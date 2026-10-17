"""Thin owner-only HTTP reads; all blocking database units run outside the event loop."""

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from app.contracts.console import (
    ActivityPage,
    ContentPage,
    FlowDetail,
    FlowPage,
    JobPage,
    Phase,
    RepositoryPage,
)
from app.controler.runtime import ServerRuntime
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore

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
        await runtime.console.db(partial(ConsoleStore(runtime.database).request_sync, flow_id))
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
