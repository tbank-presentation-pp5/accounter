from pydantic import BaseModel
from typing import Dict, List, Optional


class AccountMetrics(BaseModel):
    email: str
    neurons: int
    status: str  # available / exhausted / error / unknown
    models: Optional[Dict[str, int]] = None
    last_updated: Optional[str] = None


class MetricsSummary(BaseModel):
    total: int
    available: int
    exhausted: int
    error: int
    unknown: int
    total_neurons: int


class MetricsResponse(BaseModel):
    summary: MetricsSummary
    accounts: List[AccountMetrics]


class HistoryPoint(BaseModel):
    email: str
    neurons: int
    status: str
    checked_at: str


class HistoryResponse(BaseModel):
    history: List[HistoryPoint]
