"""Control configuration and its process-wide settings instance."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.github import ProjectConfig


class ServeSettings(BaseSettings):
    """Secrets and tuning used only by the control-plane process."""

    log_format: Literal["text", "json"] = "text"

    model_config = SettingsConfigDict(
        env_prefix="FLOWTOOL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    metrics_token: SecretStr | None = None
    control_api_token: SecretStr | None = None
    metrics_interval_seconds: float = Field(default=15, ge=5, le=300, allow_inf_nan=False)

    webhook_max_body_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    github_app_id: int = 0
    github_private_key_path: Path = Path("./flowtool-app.private-key.pem")
    github_webhook_secret: str = ""
    github_request_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    bot_login: str = "flowtool[bot]"

    trigger_label: str = "flow"
    allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    reviewers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    in_review_label: str = "flow:in-review"
    done_label: str = "flow:done"
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)

    @field_validator("projects")
    @classmethod
    def _project_repositories(cls, value: dict[str, ProjectConfig]) -> dict[str, ProjectConfig]:
        normalized = {repo.casefold(): config for repo, config in value.items()}
        if len(normalized) != len(value) or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) for repo in value
        ):
            raise ValueError("Projects keys must be unique owner/repository names")
        return normalized

    database_url: str
    worker_tokens: dict[str, SecretStr]

    judge_model: str = ""
    judge_api_key: SecretStr | None = None
    judge_api_base: str | None = None
    judge_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)

    settle_seconds: float = 75.0
    submit_grace_seconds: float = 15.0
    confidence_threshold: float = Field(default=0.6, ge=0, le=1, allow_inf_nan=False)
    max_revision_cycles: int = 20
    prepare_max_failures: int = Field(default=5, ge=1, le=100)
    prepare_retry_window_seconds: float = Field(default=900, gt=0, allow_inf_nan=False)

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
    reconcile_interval_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    reconcile_batch_size: int = Field(default=64, ge=1, le=1000)
    reconcile_concurrency: int = Field(default=4, ge=1, le=64)

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
