"""Role-specific configuration for the control plane and remote worker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class FlowToolSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLOWTOOL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

class ServeSettings(FlowToolSettings):
    """Secrets and tuning used only by the control-plane process."""

    github_app_id: int = 0
    github_private_key_path: Path = Path("./flowtool-app.private-key.pem")
    github_webhook_secret: str = ""
    bot_login: str = "flowtool[bot]"

    trigger_label: str = "flow"
    allowlist: Annotated[list[str], NoDecode] = []

    database_url: str
    worker_tokens: dict[str, SecretStr]

    settle_seconds: float = 75.0
    submit_grace_seconds: float = 15.0
    confidence_threshold: float = 0.6
    max_revision_cycles: int = 20

    host: str = "0.0.0.0"
    port: int = 8000
    worker_heartbeat_interval: float = 15.0
    worker_heartbeat_timeout: float = 45.0
    worker_poll_interval: float = 2.0

    @field_validator("allowlist", mode="before")
    @classmethod
    def _parse_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _require_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("FLOWTOOL_DATABASE_URL is required")
        return value

    @field_validator("worker_tokens")
    @classmethod
    def _validate_worker_tokens(
        cls, value: dict[str, SecretStr]
    ) -> dict[str, SecretStr]:
        if not value:
            raise ValueError("FLOWTOOL_WORKER_TOKENS must configure at least one worker")
        tokens: list[str] = []
        for worker_id, token in value.items():
            if not worker_id.strip():
                raise ValueError("worker ids in FLOWTOOL_WORKER_TOKENS cannot be empty")
            secret = token.get_secret_value()
            if not secret.strip():
                raise ValueError("worker tokens in FLOWTOOL_WORKER_TOKENS cannot be empty")
            tokens.append(secret)
        if len(tokens) != len(set(tokens)):
            raise ValueError("worker tokens in FLOWTOOL_WORKER_TOKENS must be unique")
        return value

    @field_validator("github_private_key_path", mode="after")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        return value.expanduser()


class WorkerSettings(FlowToolSettings):
    """Execution-only settings; deliberately excludes database and GitHub credentials."""

    worker_id: str
    worker_token: SecretStr
    serve_ws_url: str
    worker_root: Path = Path("~/.flowtool/workers/{worker_id}")
    codex_run_timeout_seconds: float = 1200.0
    allow_insecure_worker_ws: bool = False

    @field_validator("worker_id")
    @classmethod
    def _require_worker_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("FLOWTOOL_WORKER_ID is required")
        return value

    @field_validator("worker_token")
    @classmethod
    def _require_worker_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("FLOWTOOL_WORKER_TOKEN is required")
        return value

    @field_validator("worker_root", mode="after")
    @classmethod
    def _expand_worker_root(cls, value: Path, info: ValidationInfo) -> Path:
        if value == Path("~/.flowtool/workers/{worker_id}"):
            value = Path(f"~/.flowtool/workers/{info.data['worker_id']}")
        return value.expanduser()

    @model_validator(mode="after")
    def _require_secure_websocket(self) -> WorkerSettings:
        if self.serve_ws_url.startswith("wss://"):
            return self
        if self.allow_insecure_worker_ws and self.serve_ws_url.startswith("ws://"):
            return self
        raise ValueError(
            "FLOWTOOL_SERVE_WS_URL must use wss://; set "
            "FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true only for development"
        )


_serve_settings: ServeSettings | None = None
_worker_settings: WorkerSettings | None = None


def get_serve_settings() -> ServeSettings:
    global _serve_settings
    if _serve_settings is None:
        _serve_settings = ServeSettings()  # type: ignore[call-arg]
    return _serve_settings


def get_worker_settings() -> WorkerSettings:
    global _worker_settings
    if _worker_settings is None:
        _worker_settings = WorkerSettings()  # type: ignore[call-arg]
    return _worker_settings
