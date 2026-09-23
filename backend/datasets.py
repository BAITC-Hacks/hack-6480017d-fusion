"""Bounded local imports. Never extract archive paths or replace original data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid
import zipfile

from fastapi import HTTPException, UploadFile
import pyarrow.parquet as pq

from backend.analysis_service import AnalysisService
from backend.audit import EXPECTED_TYPES, audit_data, assert_valid_audit

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ROWS = {'nodes': 10_000, 'edges': 100_000, 'transactions': 200_000}
FILENAMES = frozenset(f'{name}.parquet' for name in MAX_ROWS)
KEEP_PREVIOUS = ' Предыдущий датасет сохранён.'


class DatasetError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message + KEEP_PREVIOUS)
        self.status_code = status_code


class UploadSizeLimitMiddleware:
    """Limit multipart bytes before parsing, including requests without a length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') != '/api/datasets' or scope.get('method') != 'POST':
            return await self.app(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get('body', b''))
            if total > MAX_UPLOAD_BYTES + 64 * 1024:
                raise HTTPException(413, 'Общий размер загрузки превышает 50 МиБ.' + KEEP_PREVIOUS)
            return message

        await self.app(scope, bounded_receive, send)


@dataclass
class PreparedDataset:
    metadata: dict
    analysis: AnalysisService
    report: dict
    directory: Path


def metadata_for(identifier: str, name: str, is_default: bool, report: dict, loaded_at: str | None = None) -> dict:
    summary = report['summary']
    return {'id': identifier, 'name': name, 'is_default': is_default,
            'nodes': summary['node_count'], 'edges': summary['edge_count'],
            'transactions': summary['transaction_count'], 'period': summary['period'],
            'loaded_at': loaded_at or datetime.now(timezone.utc).isoformat()}


def _check_metadata(directory: Path) -> None:
    uncompressed = 0
    for name, expected in EXPECTED_TYPES.items():
        parquet = pq.ParquetFile(directory / f'{name}.parquet',
                                 thrift_string_size_limit=1_048_576, thrift_container_size_limit=100_000)
        try:
            schema = parquet.schema_arrow
            if len(schema) != len(expected) or set(schema.names) != set(expected) or any(
                schema.field(column).type != dtype for column, dtype in expected.items()
            ):
                raise DatasetError(f'{name}.parquet: неверные колонки или типы данных.')
            metadata = parquet.metadata
            if metadata.num_rows > MAX_ROWS[name]:
                raise DatasetError(f'{name}.parquet: превышен лимит {MAX_ROWS[name]} строк.', 413)
            for group in range(metadata.num_row_groups):
                row_group = metadata.row_group(group)
                uncompressed += sum(row_group.column(column).total_uncompressed_size for column in range(row_group.num_columns))
            if uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise DatasetError('Распакованные данные превышают 200 МиБ.', 413)
        finally:
            parquet.close()


def _copy(source, target: Path, remaining: int) -> int:
    written = 0
    with target.open('wb') as output:
        while chunk := source.read(min(1024 * 1024, remaining - written + 1)):
            written += len(chunk)
            if written > remaining:
                raise DatasetError('Распакованные данные превышают 200 МиБ.', 413)
            output.write(chunk)
    return written


def _unpack(source, directory: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > 1000 or sum(item.file_size for item in infos) > MAX_UNCOMPRESSED_BYTES:
            raise DatasetError('Архив превышает лимит: 1000 файлов или 200 МиБ после распаковки.', 413)
        selected = {}
        paths = set()
        for item in infos:
            raw = item.filename
            path = PurePosixPath(raw)
            mode = item.external_attr >> 16
            if ('\\' in raw or '\x00' in raw or path.is_absolute() or '..' in path.parts
                    or (path.parts and ':' in path.parts[0]) or stat.S_ISLNK(mode)):
                raise DatasetError('Архив содержит небезопасный путь или символическую ссылку.')
            if raw in paths:
                raise DatasetError('Архив содержит повторяющиеся имена файлов.')
            paths.add(raw)
            if item.flag_bits & 1:
                raise DatasetError('Зашифрованные архивы не поддерживаются.')
            if item.is_dir():
                continue
            # macOS adds resource-fork files named ._nodes.parquet when archiving
            # folders. They are metadata, not a second copy of the dataset.
            if '__MACOSX' in path.parts or path.name.startswith('._') or path.name == '.DS_Store':
                continue
            if path.suffix.lower() == '.parquet':
                if path.name not in FILENAMES or path.name in selected:
                    raise DatasetError('В ZIP должен быть ровно один файл каждого имени: nodes.parquet, edges.parquet, transactions.parquet.')
                selected[path.name] = item
            # All other auxiliary members are ignored after path/size checks.
            # Never extract or execute starter scripts, nested archives or docs.
        if set(selected) != FILENAMES:
            raise DatasetError('В ZIP нужны nodes.parquet, edges.parquet и transactions.parquet.')
        written = 0
        for name, item in selected.items():
            with archive.open(item) as source_file:
                written += _copy(source_file, directory / name, MAX_UNCOMPRESSED_BYTES - written)


class DatasetStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.directory = self.root / 'datasets'
        self.pointer = self.root / 'active_dataset.json'

    def prepare(self, uploads: list[UploadFile]) -> PreparedDataset:
        identifier = uuid.uuid4().hex
        directory = self.directory / identifier
        try:
            if len(uploads) not in (1, 3):
                raise DatasetError('Выберите три parquet-файла или один ZIP-архив.')
            total = 0
            for upload in uploads:
                upload.file.seek(0, 2)
                total += upload.file.tell()
                upload.file.seek(0)
            if total > MAX_UPLOAD_BYTES:
                raise DatasetError('Общий размер загрузки превышает 50 МиБ.', 413)
            directory.mkdir(parents=True)
            if len(uploads) == 1:
                name = uploads[0].filename or ''
                if not name.lower().endswith('.zip'):
                    raise DatasetError('Один файл должен быть ZIP-архивом с тремя parquet.')
                _unpack(uploads[0].file, directory)
                friendly_name = PurePosixPath(name.replace('\\', '/')).name[:160]
            else:
                if {item.filename for item in uploads} != FILENAMES:
                    raise DatasetError('Нужны ровно nodes.parquet, edges.parquet и transactions.parquet без повторений.')
                for upload in uploads:
                    _copy(upload.file, directory / upload.filename, MAX_UPLOAD_BYTES)
                friendly_name = 'Загруженный датасет'
            return self.load(identifier, friendly_name)
        except DatasetError:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise DatasetError('Не удалось прочитать ZIP/Parquet или выполнить расчёт. Проверьте формат и целостность файлов.') from None

    def load(self, identifier: str, name: str, loaded_at: str | None = None) -> PreparedDataset:
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise DatasetError('Сохранённый датасет имеет некорректный идентификатор.')
        directory = self.directory / identifier
        _check_metadata(directory)
        report = audit_data(directory, enforce_case_period=False)
        try:
            assert_valid_audit(report)
        except ValueError as error:
            raise DatasetError(str(error)) from None
        analysis = AnalysisService(directory, report)
        return PreparedDataset(metadata_for(identifier, name, False, report, loaded_at), analysis, report, directory)

    def restore(self) -> PreparedDataset | None:
        if not self.pointer.exists():
            return None
        metadata = json.loads(self.pointer.read_text(encoding='utf-8'))
        if metadata.get('is_default'):
            return None
        return self.load(metadata['id'], metadata['name'], metadata.get('loaded_at'))

    def activate(self, metadata: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f'.active-{uuid.uuid4().hex}.json'
        try:
            temporary.write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, self.pointer)
        finally:
            temporary.unlink(missing_ok=True)
