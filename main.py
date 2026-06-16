from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Union
from contextlib import asynccontextmanager
import asyncio
import json
import sqlite3
import threading
import time
import uvicorn
import os
import logging

from async_cloudflare_stats import CloudflareAIStats
from models.response import AccountSuccessResponse, AccountNoAccountsResponse
from models.account import AccountDataInput, AccountAddResponse
from models.metrics import AccountMetrics, MetricsSummary, MetricsResponse, HistoryPoint, HistoryResponse


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)
logging.getLogger("uvicorn.access").setLevel(LOG_LEVEL)
DB_PATH = os.getenv("DB_PATH", "auth.db")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8001"))
METRICS_RETENTION_DAYS = int(os.getenv("METRICS_RETENTION_DAYS", "0"))
neuron_cache: Dict[str, int] = {}
_cursor_rowid: int = 0  # rowid последнего исчерпанного аккаунта


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    prewarm_neuron_cache()
    logging.getLogger("uvicorn.access").addFilter(ExcludeHealthFilter())

    thread = threading.Thread(target=clear_old_cache, daemon=True)
    thread.start()

    if METRICS_RETENTION_DAYS > 0:
        metrics_thread = threading.Thread(target=clear_old_metrics, daemon=True)
        metrics_thread.start()

    try:
        yield
    finally:
        pass


app = FastAPI(
    title="Accounter",
    description="Бэк-сервис для проверки и получения аккаунтов Cloudflare AI",
    version="1.1.0",
    lifespan=lifespan
)


# DB


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS auth (
            email TEXT PRIMARY KEY,
            password TEXT,
            acc_token TEXT,
            account_id TEXT,
            ai_token TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS metrics_current (
            email       TEXT PRIMARY KEY,
            neurons     INTEGER NOT NULL,
            status      TEXT NOT NULL,
            models_json TEXT,
            updated_at  TEXT NOT NULL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS metrics_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT NOT NULL,
            neurons    INTEGER NOT NULL,
            status     TEXT NOT NULL,
            checked_at TEXT NOT NULL
        )
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_history_email
        ON metrics_history(email, checked_at)
        """)
        conn.commit()


def upsert_metrics_current(email: str, neurons: int, status: str, models: Optional[Dict[str, int]] = None):
    models_json = json.dumps(models) if models else None
    updated_at = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("""
                INSERT INTO metrics_current (email, neurons, status, models_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    neurons     = excluded.neurons,
                    status      = excluded.status,
                    models_json = COALESCE(excluded.models_json, metrics_current.models_json),
                    updated_at  = excluded.updated_at
            """, (email, neurons, status, models_json, updated_at))
            conn.commit()
    except Exception as e:
        logging.error(f"upsert_metrics_current {email}: {e}")


def insert_metrics_history(email: str, neurons: int, status: str):
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("""
                INSERT INTO metrics_history (email, neurons, status, checked_at)
                VALUES (?, ?, ?, ?)
            """, (email, neurons, status, checked_at))
            conn.commit()
    except Exception as e:
        logging.error(f"insert_metrics_history {email}: {e}")


def update_metrics_models(email: str, models: Dict[str, int]):
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute(
                "UPDATE metrics_current SET models_json = ? WHERE email = ?",
                (json.dumps(models), email),
            )
            conn.commit()
    except Exception as e:
        logging.error(f"update_metrics_models {email}: {e}")


# Cache


def prewarm_neuron_cache():
    today = datetime.now(timezone.utc).date().isoformat()
    today_start = f"{today}T00:00:00"  # only data after daily reset at 00:00 UTC
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("""
                SELECT email, neurons FROM metrics_current
                WHERE neurons >= 10000
                  AND updated_at >= ?
            """, (today_start,)).fetchall()
        for email, neurons in rows:
            neuron_cache[f"{email}_{today}"] = neurons
        if rows:
            logging.info(f"Prewarmed neuron cache: {len(rows)} exhausted accounts")
    except Exception as e:
        logging.error(f"prewarm_neuron_cache: {e}")


def clear_old_cache():
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=2)
        time.sleep((target - now).total_seconds())
        neuron_cache.clear()
        logging.info("Neuron cache cleared")


def clear_old_metrics():
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        try:
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                conn.execute(
                    "DELETE FROM metrics_history WHERE checked_at < datetime('now', ? || ' days')",
                    (f"-{METRICS_RETENTION_DAYS}",),
                )
                conn.commit()
            logging.info(f"Cleaned metrics_history older than {METRICS_RETENTION_DAYS} days")
        except Exception as e:
            logging.error(f"clear_old_metrics: {e}")


# Helpers


def get_all_accounts() -> List[Dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM auth")
        return [dict(row) for row in cur.fetchall()]


async def get_neurons_count(email: str, account_id: str, acc_token: str) -> tuple[int, bool]:
    """Returns (neurons, from_cache). neurons=-1 on Cloudflare error."""
    cache_key = f"{email}_{datetime.now(timezone.utc).date()}"
    if cache_key in neuron_cache:
        return neuron_cache[cache_key], True

    total_neurons = 0
    async with CloudflareAIStats(acc_token, email, account_id) as stats:
        total_neurons = await stats.get_last_24h_neurons()
        if total_neurons == -1 or total_neurons >= 10000:
            neuron_cache[cache_key] = int(total_neurons)
            return int(total_neurons), False

    return int(total_neurons), False


async def fetch_and_store_models(email: str, account_id: str, acc_token: str):
    try:
        async with CloudflareAIStats(acc_token, email, account_id) as stats:
            models = await stats.get_neurons_by_model_breakdown()
        if models:
            await asyncio.to_thread(update_metrics_models, email, models)
    except Exception as e:
        logging.error(f"fetch_and_store_models {email}: {e}")


# Endpoints


@app.get(
    "/get_acc",
    response_model=Union[AccountSuccessResponse, AccountNoAccountsResponse],
    summary="Получить доступный аккаунт",
)
async def get_account_with_low_neurons() -> Union[AccountSuccessResponse, AccountNoAccountsResponse]:
    """Получить аккаунт с количеством нейронов < 10000."""
    global _cursor_rowid

    def _count() -> int:
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute("SELECT COUNT(*) FROM auth").fetchone()[0]

    def _fetch_next(after_rowid: int) -> Optional[Dict]:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT email, acc_token, account_id, ai_token, rowid"
                " FROM auth WHERE rowid > ? ORDER BY rowid LIMIT 1",
                (after_rowid,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT email, acc_token, account_id, ai_token, rowid"
                    " FROM auth ORDER BY rowid LIMIT 1"
                ).fetchone()
            if not row:
                return None
            return {"email": row[0], "acc_token": row[1], "account_id": row[2],
                    "ai_token": row[3], "rowid": row[4]}

    n = await asyncio.to_thread(_count)
    if not n:
        return AccountNoAccountsResponse(status="no_accounts", message="No accounts in database")

    for _ in range(n):
        account = await asyncio.to_thread(_fetch_next, _cursor_rowid)
        if not account:
            break

        email = account["email"]
        neurons, from_cache = await get_neurons_count(email, account["account_id"], account["acc_token"])

        if neurons == -1:
            status, stored = "error", 0
        elif neurons >= 10000:
            status, stored = "exhausted", neurons
        else:
            status, stored = "available", neurons

        if not from_cache:
            asyncio.create_task(asyncio.to_thread(upsert_metrics_current, email, stored, status))
            asyncio.create_task(asyncio.to_thread(insert_metrics_history, email, stored, status))
            if neurons >= 10000:
                asyncio.create_task(fetch_and_store_models(email, account["account_id"], account["acc_token"]))

        if 0 <= neurons <= 9999:
            if neurons >= 1:
                asyncio.create_task(fetch_and_store_models(email, account["account_id"], account["acc_token"]))
            return AccountSuccessResponse(
                status="success",
                account_id=account["account_id"],
                ai_token=account["ai_token"],
                neurons_count=neurons,
                email=email,
            )

        # exhausted или error — двигаем курсор на следующий
        _cursor_rowid = account["rowid"]

    return AccountNoAccountsResponse(
        status="no_accounts",
        message="No accounts with neurons < 10000 found",
    )


@app.post(
    "/add_acc",
    response_model=AccountAddResponse,
    summary="Добавить аккаунт",
)
async def add_account(account_data: AccountDataInput) -> AccountAddResponse:
    def _insert(account_dict: dict):
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("""
            INSERT OR REPLACE INTO auth (email, password, acc_token, account_id, ai_token)
            VALUES (:email, :password, :acc_token, :account_id, :ai_token)
            """, account_dict)
            conn.commit()

    try:
        await asyncio.to_thread(_insert, account_data.model_dump())
        return AccountAddResponse(status="success", message="Account added/updated")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="Текущие метрики по аккаунтам",
)
async def get_metrics() -> MetricsResponse:
    def _query():
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM metrics_current ORDER BY neurons DESC"
            ).fetchall()

    try:
        metric_rows = await asyncio.to_thread(_query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    accounts: List[AccountMetrics] = []
    summary: Dict[str, int] = {
        "total": 0, "available": 0, "exhausted": 0,
        "error": 0, "unknown": 0, "total_neurons": 0,
    }

    for row in metric_rows:
        models = json.loads(row["models_json"]) if row["models_json"] else None
        accounts.append(AccountMetrics(
            email=row["email"],
            neurons=row["neurons"],
            status=row["status"],
            models=models,
            last_updated=row["updated_at"],
        ))
        summary["total"] += 1
        s = row["status"]
        if s in summary:
            summary[s] += 1
        summary["total_neurons"] += row["neurons"]

    return MetricsResponse(
        summary=MetricsSummary(**summary),
        accounts=accounts,
    )


@app.get(
    "/metrics/history",
    response_model=HistoryResponse,
    summary="История метрик",
)
async def get_metrics_history(hours: int = 24) -> HistoryResponse:
    def _query():
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if hours == 0:
                return conn.execute("""
                    SELECT email, neurons, status, checked_at
                    FROM metrics_history
                    ORDER BY checked_at ASC
                """).fetchall()
            return conn.execute("""
                SELECT email, neurons, status, checked_at
                FROM metrics_history
                WHERE checked_at >= datetime('now', ? || ' hours')
                ORDER BY checked_at ASC
            """, (f"-{hours}",)).fetchall()

    try:
        rows = await asyncio.to_thread(_query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return HistoryResponse(history=[
        HistoryPoint(
            email=row["email"],
            neurons=row["neurons"],
            status=row["status"],
            checked_at=row["checked_at"],
        )
        for row in rows
    ])


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse("static/index.html")


@app.get("/health", include_in_schema=False)
async def health():
    return Response(status_code=204)


# ---


class ExcludeHealthFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        ws=None,
        loop="uvloop",
    )
