"""Run from project root: .venv/bin/python -m uvicorn backend.app:app."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.responses import Response

from backend.analysis_service import AnalysisService, EXPORT_FILENAMES
from backend.audit import DEFAULT_DATA_DIR, assert_valid_audit, audit_data
from backend.serialization import Gid, SafeJSONResponse

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
        application.state.analysis = None
        try:
            report = audit_data(data_dir)
            assert_valid_audit(report)
            data = report["summary"]
            application.state.summary = Summary(
                nodes=data["node_count"], edges=data["edge_count"],
                transactions=data["transaction_count"], seed_nodes=data["seed_count"],
                period=data["period"],
            )
            try:
                application.state.analysis = AnalysisService(Path(data_dir), report)
            except Exception:
                logger.exception("Не удалось рассчитать роли и граф; исходная сводка доступна")
        except Exception:
            logger.exception("Не удалось загрузить или проверить локальные parquet")
        yield

    application = FastAPI(
        title="Fusion", version="0.3.0", lifespan=lifespan,
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

    def analysis_for(request: Request) -> AnalysisService:
        service = request.app.state.analysis
        if service is None:
            raise HTTPException(
                status_code=503,
                detail="Расчёт недоступен. Проверьте данные и перезапустите сервер.",
            )
        return service

    @application.get("/api/analysis")
    def analysis(request: Request):
        return SafeJSONResponse(analysis_for(request).summary)

    @application.get("/api/nodes/{gid}")
    def node(gid: Gid, request: Request):
        service = analysis_for(request)
        if gid not in service.nodes:
            raise HTTPException(status_code=404, detail="Узел не найден в выгрузке.")
        return SafeJSONResponse(service.node(gid))

    @application.get("/api/graph")
    def graph(request: Request, cluster_id: int | None = Query(default=None, ge=0),
              gid: Gid | None = None, limit: int = Query(default=250, ge=1, le=500)):
        if cluster_id is not None and gid is not None:
            raise HTTPException(status_code=422, detail="Выберите кластер или узел, а не оба одновременно.")
        service = analysis_for(request)
        if gid is not None and gid not in service.nodes:
            raise HTTPException(status_code=404, detail="Узел не найден в выгрузке.")
        if cluster_id is not None and cluster_id not in service.cluster_ids:
            raise HTTPException(status_code=404, detail="Кластер не найден в выгрузке.")
        return SafeJSONResponse(service.graph(cluster_id=cluster_id, gid=gid, limit=limit))

    @application.get("/api/exports/{filename}")
    def export(filename: str, request: Request):
        if filename not in EXPORT_FILENAMES:
            raise HTTPException(status_code=404, detail="Выгрузка не найдена.")
        return Response(
            content=analysis_for(request).exports[filename], media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return application


app = create_app()
