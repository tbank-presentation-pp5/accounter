from fastapi import FastAPI, HTTPException, Response
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Union
from contextlib import asynccontextmanager
import sqlite3
import threading
import time
import uvicorn
import os
import logging

from async_cloudflare_stats import CloudflareAIStats
from models.response import AccountSuccessResponse, AccountNoAccountsResponse
from models.account import AccountDataInput, AccountAddResponse


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)
logging.getLogger("uvicorn.access").setLevel(LOG_LEVEL)
DB_PATH = os.getenv("DB_PATH", "auth.db")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8001"))
neuron_cache = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # убераем health ручку из логов
    logging.getLogger("uvicorn.access").addFilter(ExcludeHealthFilter())

    # запуск фонового потока очистки кэша
    thread = threading.Thread(target=clear_old_cache, daemon=True)
    thread.start()
    try:
        yield
    finally:
        pass


app = FastAPI(
    title="Accounter",
    description="Бэк-сервис для проверки и получения аккаунтов Cloudflare AI",
    version="1.0.0",
    lifespan=lifespan
)


def clear_old_cache():
    """Очистка кэша в 00:00 по UTC"""
    while True:
        now = datetime.now(timezone.utc)
        
        # Ждем до 00:00 UTC
        target_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target_time:
            target_time = target_time + timedelta(days=1)
        
        sleep_time = (target_time - now).total_seconds()
        time.sleep(sleep_time)
        
        # Очищаем кэш
        neuron_cache.clear()
        print("Neuron cache cleared at 00:00 UTC")


def init_db():
    """Инициализация базы данных"""
    with sqlite3.connect(DB_PATH) as conn:
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
        conn.commit()


def get_all_accounts() -> List[Dict]:
    """Получить все аккаунты из базы данных"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM auth")
        rows = cur.fetchall()
        return [dict(row) for row in rows]


async def get_neurons_count(email: str, account_id: str, acc_token: str) -> int:
    """Асинхронно получить количество нейронов для аккаунта"""
    # Проверяем кэш
    cache_key = f"{email}_{datetime.now(timezone.utc).date()}"
    if cache_key in neuron_cache:
        return neuron_cache[cache_key]

    total_neurons = 0
    async with CloudflareAIStats(acc_token, email, account_id) as stats:
        total_neurons = await stats.get_today_total_neurons()
        if total_neurons == -1:
            neuron_cache[cache_key] = total_neurons

        if total_neurons >= 10000:
            neuron_cache[cache_key] = total_neurons
            return total_neurons

        total_neurons_alt = await stats.get_today_neurons_by_models()
        if total_neurons_alt:
            if total_neurons_alt != total_neurons:
                if total_neurons_alt == -1:
                    neuron_cache[cache_key] = total_neurons_alt

                if total_neurons_alt >= 10000:
                    neuron_cache[cache_key] = total_neurons_alt
                
                return total_neurons_alt
    
    return total_neurons


@app.get(
    "/get_acc", 
    response_model=Union[AccountSuccessResponse, AccountNoAccountsResponse],
    summary="Получить доступный аккаунт"
)
async def get_account_with_low_neurons() -> Union[AccountSuccessResponse, AccountNoAccountsResponse]:
    """
    Получить аккаунт с количеством нейронов < 10000
    Возвращает первый найденный подходящий аккаунт
    """
    accounts = get_all_accounts()
    
    for account in accounts:
        email = account["email"]
        account_id = account["account_id"]
        acc_token = account["acc_token"]
        ai_token = account["ai_token"]
        
        neurons = await get_neurons_count(email, account_id, acc_token)
        
        if neurons <= 9999:
            return AccountSuccessResponse(
                status="success",
                account_id=account_id,
                ai_token=ai_token,
                neurons_count=neurons,
                email=email
            )

    return AccountNoAccountsResponse(
        status="no_accounts",
        message="No accounts with neurons < 10000 found"
    )


@app.post(
    "/add_account",
    response_model=AccountAddResponse,
    summary="Добавить аккаунт")
async def add_account(account_data: AccountDataInput) -> AccountAddResponse:
    """Добавить аккаунт в базу данных"""
    try:
        account_dict = account_data.model_dump()
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("""
            INSERT OR REPLACE INTO auth (email, password, acc_token, account_id, ai_token)
            VALUES (:email, :password, :acc_token, :account_id, :ai_token)
            """, account_dict)
            conn.commit()
        
        return AccountAddResponse(status="success", message="Account added/updated")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/health", include_in_schema=False)
async def health():
    return Response(status_code=204)


class ExcludeHealthFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
