"""Run from project root: .venv/bin/python -m uvicorn backend.app:app."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from backend.audit import DEFAULT_DATA_DIR, assert_valid_audit, audit_data
from backend.serialization import SafeJSONResponse

logger = logging.getLogger(__name__)


class Period(BaseModel):
    start: str
    end: str


class Summary(BaseModel):
    nodes: int
    edges: int
    transactions: int
    seed_nodes: int
    period: Period


class Health(BaseModel):
    status: str
    service: str


def create_app(data_dir: Path = DEFAULT_DATA_DIR) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.summary = None
        try:
            report = audit_data(data_dir)
            assert_valid_audit(report)
            data = report["summary"]
            application.state.summary = Summary(
                nodes=data["node_count"], edges=data["edge_count"],
                transactions=data["transaction_count"], seed_nodes=data["seed_count"],
                period=data["period"],
            )
        except Exception:
            logger.exception("Не удалось загрузить или проверить локальные parquet")
        yield

    application = FastAPI(
        title="Fusion", version="0.1.0", lifespan=lifespan,
        default_response_class=SafeJSONResponse,
    )

    @application.get("/api/health", response_model=Health)
    def health() -> dict:
        """Liveness only; summary reports data readiness separately."""
        return {"status": "ok", "service": "Fusion"}

    @application.get("/api/summary", response_model=Summary)
    def summary(request: Request) -> Summary:
        if request.app.state.summary is None:
            raise HTTPException(
                status_code=503,
                detail="Данные недоступны или не прошли проверку. Запустите аудит данных.",
            )
        return request.app.state.summary

    return application


app = create_app()
