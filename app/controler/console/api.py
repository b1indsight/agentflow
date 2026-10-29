"""Owner-only console HTTP; all blocking database units run outside the event loop."""

import asyncio
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from app.contracts.console import (
    ActivityPage,
    ArchiveRequest,
    ConsoleFailure,
    ContentPage,
    FlowDetail,
    FlowPage,
    JobPage,
    Phase,
    RepositoryPage,
    RetryRequest,
)
from app.controler.runtime import ServerRuntime
from app.controler.workflow.service import ControlService, RetryRejected
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore
from app.persistence.flows import FlowStore

router = APIRouter(prefix="/api")
FlowId = Annotated[int, Path(gt=0)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=4096)]


@router.get("/flows", response_model=FlowPage)
async def flows(
    request: Request,
    q: str = Query(default="", max_length=2000),
    phase: Phase | None = None,
    cursor: Cursor = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> FlowPage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(ConsoleStore(runtime.database).flows, q=q, phase=phase, cursor=cursor, limit=limit)
    )


@router.get("/repos", response_model=RepositoryPage)
async def repos(
    request: Request, cursor: Cursor = None, limit: int = Query(default=100, ge=1, le=100)
) -> RepositoryPage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(ConsoleStore(runtime.database).repos, cursor=cursor, limit=limit)
    )


@router.get("/flows/{flow_id}", response_model=FlowDetail)
async def detail(request: Request, flow_id: FlowId) -> FlowDetail:
    runtime: ServerRuntime = request.app.state.runtime
    store = ConsoleStore(runtime.database)
    await runtime.console.db(partial(store.request_sync, flow_id))
    return await runtime.console.db(partial(store.detail, flow_id))


def _require_same_origin_json(request: Request) -> None:
    """Protect both console commands, including behind TLS and Host-rewriting proxies."""
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    # Browser-controlled Fetch Metadata survives proxy TLS termination and Host rewriting.
    # Fall back to Origin comparison when there is no explicit same-origin signal.
    if fetch_site not in (None, "same-origin", "none") or (
        fetch_site != "same-origin"
        and origin is not None
        and origin != str(request.base_url).rstrip("/")
    ):
        raise ConsoleFailure("cross_origin_request", 403)
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ConsoleFailure("json_required", 415)


@router.post("/flows/{flow_id}/archive", response_model=FlowDetail)
async def archive(request: Request, flow_id: FlowId, body: ArchiveRequest) -> FlowDetail:
    """Archive only local display state; JSON and origin checks prevent form-based CSRF."""
    _require_same_origin_json(request)
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(partial(ConsoleStore(runtime.database).archive, flow_id))


@router.post("/flows/{flow_id}/retry", response_model=FlowDetail)
async def retry(request: Request, flow_id: FlowId, body: RetryRequest) -> FlowDetail:
    """Request canonical retry, reporting refusal or no change without claiming execution."""
    _require_same_origin_json(request)
    runtime: ServerRuntime = request.app.state.runtime
    loop = asyncio.get_running_loop()

    def notify_jobs() -> None:
        # Schedule on the ASGI loop after commit, including after client disconnect.
        loop.call_soon_threadsafe(runtime.worker_handler.notify_jobs)

    def command() -> FlowDetail:
        flow = FlowStore(runtime.database).get_flow(flow_id)
        if flow is None:
            raise ConsoleFailure("flow_not_found", 404)
        service = ControlService(
            database=runtime.database,
            jobs_available=notify_jobs,
        )
        try:
            accepted = service.retry(flow)
        except RetryRejected as error:
            raise ConsoleFailure(
                error.code, 404 if error.code == "flow_not_found" else 409
            ) from error
        if not accepted:
            raise ConsoleFailure("retry_no_change")
        return ConsoleStore(runtime.database).detail(flow_id)

    return await runtime.console.db(command)


@router.get("/flows/{flow_id}/jobs", response_model=JobPage)
async def jobs(
    request: Request,
    flow_id: FlowId,
    cursor: Cursor = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> JobPage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(ConsoleStore(runtime.database).jobs, flow_id, cursor=cursor, limit=limit)
    )


@router.get("/flows/{flow_id}/activity", response_model=ActivityPage)
async def activity(
    request: Request,
    flow_id: FlowId,
    cursor: Cursor = None,
    snapshot: int | None = Query(default=None, ge=0),
    since: int | None = Query(default=None, ge=0),
    refresh: bool = False,
    parent: str | None = Query(default=None, min_length=1, max_length=512),
    limit: int = Query(default=20, ge=1, le=50),
) -> ActivityPage:
    runtime: ServerRuntime = request.app.state.runtime
    if refresh:
        await runtime.console.db(
            partial(ConsoleStore(runtime.database).request_sync, flow_id, active_only=False)
        )
    return await runtime.console.db(
        partial(
            ActivityStore(runtime.database).page,
            flow_id,
            cursor=cursor,
            snapshot=snapshot,
            since=since,
            parent=parent,
            limit=limit,
        )
    )


@router.get("/flows/{flow_id}/activity/versions", response_model=ActivityPage)
async def versions(
    request: Request,
    flow_id: FlowId,
    key: str = Query(min_length=1, max_length=512),
    cursor: Cursor = None,
    snapshot: int | None = Query(default=None, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
) -> ActivityPage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(
            ActivityStore(runtime.database).versions,
            flow_id,
            key,
            cursor=cursor,
            snapshot=snapshot,
            limit=limit,
        )
    )


@router.get("/flows/{flow_id}/activity/content", response_model=ContentPage)
async def content(
    request: Request,
    flow_id: FlowId,
    key: str = Query(min_length=1, max_length=512),
    version: int = Query(gt=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=16384, ge=1, le=16384),
    compare: bool = False,
) -> ContentPage:
    """Page archived body text, or return a complete patch with compare=true (offset must be zero)."""
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(
            ActivityStore(runtime.database).content,
            flow_id,
            key,
            version,
            offset,
            limit,
            compare=compare,
        )
    )
