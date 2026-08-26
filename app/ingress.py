"""FastAPI webhook 入口 + 组合根。ARCHITECTURE §4.1。

/webhook：HMAC 验签 → 忽略 bot / 去重（router）→ 秒回 202。
lifespan 里装配所有组件、起 worker / reconciler 后台任务。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request, Response
from loguru import logger

from app.codex_driver import CodexAgent
from app.config import get_settings
from app.engine import FlowEngine
from app.github_client import GitHubApp
from app.judge import BubJudge
from app.reconcile import Reconciler
from app.router import EventRouter
from app.store import Store
from app.worker import Worker


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """校验 X-Hub-Signature-256（sha256=<hexdigest>）。"""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


class App:
    """组合根：持有所有单例组件。"""

    def __init__(self) -> None:
        s = get_settings()
        self.settings = s
        self.store = Store(s.db_path)
        self.engine = FlowEngine(self.store, CodexAgent(), BubJudge(), GitHubApp())
        self.router = EventRouter(self.store, self.engine)
        self.worker = Worker(self.store, self.engine)
        self.reconciler = Reconciler(self.store, self.engine)
        self._tasks: list[asyncio.Task] = []
        self._handlers: set[asyncio.Task] = set()

    def spawn_handler(self, coro) -> None:
        """后台跑一个 webhook 处理协程：存引用防 GC 半路回收 + 完成时记异常。"""
        task = asyncio.create_task(coro)
        self._handlers.add(task)
        task.add_done_callback(self._on_handler_done)

    def _on_handler_done(self, task: asyncio.Task) -> None:
        self._handlers.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.opt(exception=exc).error("webhook handler crashed")

    def startup(self) -> None:
        self.store.init_db()
        self._tasks = [
            asyncio.create_task(self.worker.run()),
            asyncio.create_task(self.reconciler.run()),
        ]

    async def shutdown(self) -> None:
        self.worker.stop()
        self.reconciler.stop()
        for t in (*self._tasks, *self._handlers):
            t.cancel()
        self.store.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = App()
    app.state.ctx = ctx
    ctx.startup()
    logger.info("FlowTool up on {}:{}", ctx.settings.host, ctx.settings.port)
    try:
        yield
    finally:
        await ctx.shutdown()


app = FastAPI(title="FlowTool", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    ctx: App = request.app.state.ctx
    body = await request.body()
    if not verify_signature(ctx.settings.github_webhook_secret, body, x_hub_signature_256):
        return Response(status_code=401, content="bad signature")

    payload = await request.json()
    # 长活不能卡在这里（GitHub 10s 超时）：交后台，秒回 202。
    ctx.spawn_handler(ctx.router.handle(x_github_event, x_github_delivery, payload))
    return Response(status_code=202)
