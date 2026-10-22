"""Worker configuration and its process-wide settings instance."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Execution-only settings; deliberately excludes database and GitHub credentials."""

    log_format: Literal["text", "json"] = "text"

    model_config = SettingsConfigDict(
        env_prefix="FLOWTOOL_",
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    worker_id: str
    worker_token: SecretStr
    serve_ws_url: str
    worker_status_path: Path | None = None
    worker_root: Path = Path("~/.flowtool/workers/{worker_id}")
    codex_run_timeout_seconds: float = 1200.0
    trusted_host_execution: bool = False
    execution_isolation: Literal["dedicated-account", "container", "vm"] | None = None
    allow_insecure_worker_ws: bool = False
    reconnect_min_backoff: float = Field(default=1.0, gt=0)
    reconnect_max_backoff: float = Field(default=30.0, gt=0)
    reconnect_jitter: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def _require_full_access_isolation(self) -> WorkerSettings:
        if self.trusted_host_execution and self.execution_isolation is None:
            raise ValueError(
                "trusted-host execution requires external execution isolation "
                "(dedicated-account, container, or vm)"
            )
        return self

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
            worker_id = info.data.get("worker_id")
            if worker_id is None:
                return value
            value = Path(f"~/.flowtool/workers/{worker_id}")
        return value.expanduser()

    @model_validator(mode="after")
    def _require_secure_websocket(self) -> WorkerSettings:
        if self.reconnect_max_backoff < self.reconnect_min_backoff:
            raise ValueError("worker reconnect backoff range or jitter is invalid")
        if self.serve_ws_url.startswith("wss://"):
            return self
        if self.allow_insecure_worker_ws and self.serve_ws_url.startswith("ws://"):
            return self
        raise ValueError(
            "FLOWTOOL_SERVE_WS_URL must use wss://; set "
            "FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true only for development"
        )

if TYPE_CHECKING:
    settings: WorkerSettings


def __getattr__(name: str) -> WorkerSettings:
    """Create the process-wide settings on first access, keeping type imports inert."""
    if name != "settings":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = WorkerSettings()  # type: ignore[call-arg]
    globals()["settings"] = value
    return value
