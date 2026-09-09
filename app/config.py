"""Role-specific configuration for the control plane and remote worker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
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
    github_request_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    bot_login: str = "flowtool[bot]"

    trigger_label: str = "flow"
    allowlist: Annotated[list[str], NoDecode] = []
    reviewers: Annotated[list[str], NoDecode] = []
    in_review_label: str = "flow:in-review"
    done_label: str = "flow:done"

    database_url: str
    worker_tokens: dict[str, SecretStr]

    settle_seconds: float = 75.0
    submit_grace_seconds: float = 15.0
    confidence_threshold: float = Field(default=0.6, ge=0, le=1, allow_inf_nan=False)
    max_revision_cycles: int = 20

    host: str = "0.0.0.0"
    port: int = 8000
    worker_heartbeat_interval: float = Field(default=15.0, gt=0)
    worker_heartbeat_timeout: float = Field(default=45.0, gt=0)
    worker_lease_duration: float = Field(default=60.0, gt=0)
    worker_recovery_grace: float = Field(default=300.0, gt=0)
    worker_sweep_interval: float = Field(default=5.0, gt=0)
    worker_poll_interval: float = Field(default=2.0, gt=0)
    result_recovery_concurrency: int = Field(default=4, ge=1, le=64)
    result_recovery_batch_size: int = Field(default=64, ge=1, le=1000)

    @field_validator("trigger_label", "in_review_label", "done_label")
    @classmethod
    def _require_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("status and trigger labels must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _validate_status_labels(self) -> ServeSettings:
        labels = {label.casefold() for label in
                  (self.trigger_label, self.in_review_label, self.done_label)}
        if len(labels) != 3:
            raise ValueError("trigger, in_review and done labels must be distinct")
        return self

    @model_validator(mode="after")
    def _validate_recovery_timing(self) -> ServeSettings:
        if self.worker_lease_duration <= max(
            self.worker_heartbeat_interval * 3,
            self.worker_heartbeat_timeout,
        ):
            raise ValueError(
                "worker lease duration must exceed the heartbeat timeout and three intervals"
            )
        if self.worker_recovery_grace <= self.worker_lease_duration:
            raise ValueError("worker recovery grace must exceed the lease duration")
        return self

    @field_validator("allowlist", "reviewers", mode="before")
    @classmethod
    def _parse_name_list(cls, value: object) -> object:
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

    model_config = SettingsConfigDict(
        env_prefix="FLOWTOOL_",
        env_file=None,
        extra="ignore",
    )

    worker_id: str
    worker_token: SecretStr
    serve_ws_url: str
    worker_root: Path = Path("~/.flowtool/workers/{worker_id}")
    codex_run_timeout_seconds: float = 1200.0
    judge_model: str = ""
    judge_api_key: SecretStr | None = None
    judge_api_base: str | None = None
    judge_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
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
