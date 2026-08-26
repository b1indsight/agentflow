"""配置：pydantic-settings，env 前缀 FLOWTOOL_。见 M0-PLAN §3。"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLOWTOOL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # GitHub App（ARCHITECTURE 附录 A）
    github_app_id: int = 0
    github_private_key_path: Path = Path("./flowtool-app.private-key.pem")
    github_webhook_secret: str = ""
    bot_login: str = "flowtool[bot]"

    # 触发
    trigger_label: str = "flow"
    allowlist: list[str] = []

    # 本地路径
    repos_root: Path = Path("~/.flowtool/repos")
    db_path: Path = Path("~/.flowtool/flowtool.sqlite3")

    # 修订循环（附录 B.7）
    settle_seconds: float = 75.0
    submit_grace_seconds: float = 15.0
    confidence_threshold: float = 0.6
    codex_run_timeout_seconds: float = 1200.0
    max_revision_cycles: int = 20

    # HTTP 服务
    host: str = "0.0.0.0"
    port: int = 8000

    @field_validator("allowlist", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # 支持逗号分隔（env 里 FLOWTOOL_ALLOWLIST=alice,bob）
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("repos_root", "db_path", "github_private_key_path", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


_settings: Settings | None = None


def get_settings() -> Settings:
    """进程内单例。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
