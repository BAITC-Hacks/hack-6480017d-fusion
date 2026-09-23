"""Run from project root: .venv/bin/python -m uvicorn backend.app:app."""

import logging
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.responses import Response
from starlette.concurrency import run_in_threadpool

from backend.analysis_service import AnalysisService, EXPORT_FILENAMES
from backend.ai_config import AISettings, load_settings
from backend.ai_providers import AIProviders
from backend.ai_service import AIService
from backend.audit import DEFAULT_DATA_DIR, assert_valid_audit, audit_data
from backend.serialization import Gid, SafeJSONResponse
from backend.datasets import DatasetStore, DatasetError, UploadSizeLimitMiddleware, metadata_for, KEEP_PREVIOUS

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


class AIRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    gid: Gid
    refresh: bool = False

    @field_validator('gid', mode='before')
    @classmethod
    def string_identifier(cls, value):
        if not isinstance(value, str):
            raise ValueError('Передайте gid строкой, чтобы не потерять точность идентификатора.')
        return value


def create_app(data_dir: Path = DEFAULT_DATA_DIR, *, ai_settings: AISettings | None = None,
               ai_providers: AIProviders | None = None,
               dataset_store_dir: Path | None = None) -> FastAPI:
    data_dir = Path(data_dir)

    def install(application, snapshot):
        # Endpoints read a single snapshot pointer; compatibility aliases support
        # existing local integrations without mutating any old snapshot.
        application.state.active_dataset = snapshot
        application.state.summary = snapshot.summary
        application.state.analysis = snapshot.analysis
        application.state.ai = snapshot.ai

    async def retire(service, *, shutdown=False):
        if service is None:
            return
        try:
            if ai_providers is None or shutdown:
                await service.aclose()
            else:
                # An injected provider belongs to all snapshots. Cancel old jobs
                # without closing its shared HTTP client on a dataset switch.
                tasks = list(service.tasks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            # A cleanup failure must not misreport an already committed switch.
            logger.warning('Предыдущий AI-сеанс не удалось полностью закрыть')

    def default_snapshot():
        report = audit_data(data_dir)
        assert_valid_audit(report)
        metadata = metadata_for('default', 'Датасет HackAlem', True, report)
        data = report['summary']
        summary = Summary(nodes=data['node_count'], edges=data['edge_count'],
                          transactions=data['transaction_count'], seed_nodes=data['seed_count'], period=data['period'])
        return SimpleNamespace(metadata=metadata, summary=summary, analysis=None, ai=None), report

    def prepared_snapshot(prepared):
        data = prepared.report['summary']
        return SimpleNamespace(metadata=prepared.metadata, analysis=prepared.analysis, ai=None,
            summary=Summary(nodes=data['node_count'], edges=data['edge_count'],
                            transactions=data['transaction_count'], seed_nodes=data['seed_count'], period=data['period']))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        temporary = None
        if dataset_store_dir is not None:
            store_root = Path(dataset_store_dir)
        elif data_dir.resolve() == DEFAULT_DATA_DIR.resolve():
            store_root = DEFAULT_DATA_DIR.parent / '.fusion'
        else:
            temporary = TemporaryDirectory(prefix='fusion-datasets-')
            store_root = Path(temporary.name)
        application.state.dataset_store = DatasetStore(store_root)
        application.state.dataset_lock = asyncio.Lock()
        install(application, SimpleNamespace(metadata=None, summary=None, analysis=None, ai=None))
        application.state.ai_settings = None
        try:
            restored = None
            try:
                restored = await run_in_threadpool(application.state.dataset_store.restore)
            except Exception:
                logger.warning('Сохранённый датасет недоступен; загружаем исходные данные')
            if restored is not None:
                install(application, prepared_snapshot(restored))
            else:
                snapshot, report = await run_in_threadpool(default_snapshot)
                install(application, snapshot)
                try:
                    snapshot.analysis = await run_in_threadpool(AnalysisService, data_dir, report)
                    install(application, snapshot)
                except Exception:
                    logger.exception("Не удалось рассчитать роли и граф; исходная сводка доступна")
        except Exception:
            logger.exception("Не удалось загрузить или проверить локальные parquet")
        try:
            settings = ai_settings if ai_settings is not None else load_settings()
            application.state.ai_settings = settings
            if application.state.analysis is not None:
                snapshot = application.state.active_dataset
                snapshot.ai = AIService(snapshot.analysis, settings, ai_providers)
                install(application, snapshot)
        except Exception:
            logger.error('AI-настройки недоступны; локальные расчёты продолжают работать')
        try:
            yield
        finally:
            await retire(application.state.active_dataset.ai, shutdown=True)
            if temporary is not None:
                temporary.cleanup()

    application = FastAPI(
        title="Fusion", version="0.4.0", lifespan=lifespan,
        default_response_class=SafeJSONResponse,
    )
    application.add_middleware(UploadSizeLimitMiddleware)

    @application.get('/api/datasets/current')
    def current_dataset(request: Request):
        metadata = request.app.state.active_dataset.metadata
        if metadata is None:
            raise HTTPException(503, 'Данные недоступны. Загрузите три parquet-файла или ZIP.')
        return SafeJSONResponse(metadata)

    def check_local_origin(request: Request):
        origin = request.headers.get('origin')
        allowed = {f'http://{host}:{port}' for host in ('localhost', '127.0.0.1', '[::1]')
                   for port in (5173, 4173, 8000)}
        if origin is not None and origin not in allowed:
            raise HTTPException(403, 'Загрузка разрешена только из локального интерфейса Fusion.' + KEEP_PREVIOUS)

    async def activate(request: Request, snapshot):
        previous = request.app.state.active_dataset
        settings = request.app.state.ai_settings
        if settings is not None:
            snapshot.ai = AIService(snapshot.analysis, settings, ai_providers)
        try:
            await run_in_threadpool(request.app.state.dataset_store.activate, snapshot.metadata)
        except Exception:
            await retire(snapshot.ai)
            raise DatasetError('Не удалось сохранить выбранный датасет.') from None
        install(request.app, snapshot)
        try:
            await retire(previous.ai)
        except Exception:
            # The new snapshot is already committed; cleanup must not report
            # a failed import or claim that the previous dataset is still active.
            logger.warning('Новый датасет активен; завершение прежнего AI-сервиса потребовало внимания')
        return SafeJSONResponse(snapshot.metadata)

    @application.post('/api/datasets')
    async def upload_dataset(request: Request, files: list[UploadFile] = File(default=[])):
        check_local_origin(request)
        lock = request.app.state.dataset_lock
        if lock.locked():
            raise HTTPException(409, 'Другой датасет уже загружается. Дождитесь завершения.' + KEEP_PREVIOUS)
        async with lock:
            try:
                prepared = await run_in_threadpool(request.app.state.dataset_store.prepare, files)
                return await activate(request, prepared_snapshot(prepared))
            except DatasetError as error:
                raise HTTPException(error.status_code, str(error)) from None
            except Exception:
                raise HTTPException(422, 'Не удалось проверить или загрузить датасет.' + KEEP_PREVIOUS) from None
            finally:
                for upload in files:
                    await upload.close()

    @application.post('/api/datasets/reset')
    async def reset_dataset(request: Request):
        check_local_origin(request)
        lock = request.app.state.dataset_lock
        if lock.locked():
            raise HTTPException(409, 'Другой датасет уже загружается. Дождитесь завершения.' + KEEP_PREVIOUS)
        async with lock:
            try:
                def prepare_default():
                    snapshot, report = default_snapshot()
                    snapshot.analysis = AnalysisService(data_dir, report)
                    return snapshot
                snapshot = await run_in_threadpool(prepare_default)
                return await activate(request, snapshot)
            except DatasetError as error:
                raise HTTPException(error.status_code, str(error)) from None
            except Exception:
                raise HTTPException(422, 'Исходные данные недоступны или не прошли проверку.' + KEEP_PREVIOUS) from None

    @application.get("/api/health", response_model=Health)
    def health() -> dict:
        """Liveness only; summary reports data readiness separately."""
        return {"status": "ok", "service": "Fusion"}

    @application.get("/api/summary", response_model=Summary)
    def summary(request: Request) -> Summary:
        snapshot = request.app.state.active_dataset
        if snapshot.summary is None:
            raise HTTPException(
                status_code=503,
                detail="Данные недоступны или не прошли проверку. Запустите аудит данных.",
            )
        return snapshot.summary

    def analysis_for(request: Request) -> AnalysisService:
        service = request.app.state.active_dataset.analysis
        if service is None:
            raise HTTPException(
                status_code=503,
                detail="Расчёт недоступен. Проверьте данные и перезапустите сервер.",
            )
        return service

    def ai_for(request: Request) -> AIService:
        service = request.app.state.active_dataset.ai
        if service is None:
            raise HTTPException(status_code=503, detail='AI-настройки или данные недоступны. Проверьте конфигурацию сервера.')
        return service

    @application.get('/api/ai/status')
    def ai_status(request: Request):
        return SafeJSONResponse(ai_for(request).configuration())

    @application.post('/api/ai/analyses', status_code=202)
    async def start_ai(payload: AIRequest, request: Request):
        service = ai_for(request)
        if payload.gid not in service.analysis.nodes:
            raise HTTPException(status_code=404, detail='Участник не найден в выгрузке.')
        try:
            return SafeJSONResponse(service.start(payload.gid, payload.refresh), status_code=202)
        except RuntimeError as error:
            raise HTTPException(status_code=429, detail=str(error)) from None

    @application.get('/api/ai/analyses/{job_id}')
    def ai_result(job_id: str, request: Request):
        try:
            return SafeJSONResponse(ai_for(request).get(job_id))
        except KeyError:
            raise HTTPException(status_code=404, detail='Разбор не найден или сервер был перезапущен.') from None

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
