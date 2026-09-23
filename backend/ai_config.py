"""Server-only AI settings. Secret values never appear in repr or public status."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


DEFAULT_ENV_PATH = Path(__file__).resolve().parent / '.env'
NVIDIA_BASE_URL = 'https://integrate.api.nvidia.com/v1'
OPENAI_MODEL = 'gpt-5.4-mini'
NVIDIA_MODEL = 'nvidia/nemotron-nano-3-30b-a3b'
RETIRED_NVIDIA_MODEL = 'nvidia/nemotron-3-nano-30b-a3b'


@dataclass(frozen=True)
class AISettings:
    openai_api_key: str = field(default='', repr=False)
    nvidia_api_key: str = field(default='', repr=False)
    openai_model: str = OPENAI_MODEL
    nvidia_model: str = NVIDIA_MODEL
    nvidia_base_url: str = NVIDIA_BASE_URL
    nvidia_enabled: bool = False
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError('Таймаут AI должен быть больше нуля и не больше 30 секунд')

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key.strip() and self.openai_model.strip())

    @property
    def nvidia_configured(self) -> bool:
        return bool(self.nvidia_api_key.strip() and self.nvidia_model.strip())


def load_settings(
    env_path: Path | str | None = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> AISettings:
    """Read at server startup, without changing os.environ or exposing values.

    Environment variables take precedence. An empty key disables that provider;
    empty model/address values fall back to the documented non-secret defaults.
    Tests pass an isolated temporary path or None; never the real project file.
    """
    values = dotenv_values(env_path, interpolate=False) if env_path is not None else {}
    values.update(os.environ if environ is None else environ)

    def value(name: str, default: str = '') -> str:
        return (values.get(name) or default).strip()

    nvidia_model = value('NVIDIA_MODEL', NVIDIA_MODEL)
    # The old documented Nano identifier returns HTTP 410; the live official
    # catalog supplies this exact replacement. Resolve without rewriting .env.
    if nvidia_model == RETIRED_NVIDIA_MODEL:
        nvidia_model = NVIDIA_MODEL

    return AISettings(
        openai_api_key=value('OPENAI_API_KEY'),
        nvidia_api_key=value('NVIDIA_API_KEY'),
        openai_model=value('OPENAI_MODEL', OPENAI_MODEL),
        nvidia_model=nvidia_model,
        nvidia_base_url=value('NVIDIA_BASE_URL', NVIDIA_BASE_URL).rstrip('/'),
        nvidia_enabled=value('NVIDIA_ENABLED').lower() == 'true',
    )
