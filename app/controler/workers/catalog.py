"""Discover model options from ready coding sessions, without scheduling a job."""

import asyncio
from typing import get_args

from app.communication.gateway import Gateway, Registration
from app.communication.requests import ReplyRejected, request_reply
from app.contracts.catalog import CatalogGroup, CatalogModel
from app.contracts.console import AmpEffort, CodexEffort
from app.contracts.protocol import (
    CatalogQuery,
    CatalogResult,
    ServerMessage,
    ServerMessageUnion,
    WorkerMessageUnion,
    call_header,
    message_header,
)
from app.controler.workers.callbacks import ServerCallbacks


async def _query(peer: Registration[WorkerMessageUnion, ServerMessageUnion]) -> list[CatalogModel] | None:
    call = CatalogQuery(**call_header(), method="worker.model_catalog")
    try:
        reply, _ = await request_reply(peer, call, CatalogResult, ServerMessage(
            **message_header(peer.session.epoch), control=None, payload=[call],
        ), timeout=30)
    except (TimeoutError, ConnectionError, ReplyRejected):
        return None
    return reply.models


async def read_catalog(gateway: Gateway[WorkerMessageUnion, ServerMessageUnion],
                       connections: set[ServerCallbacks]) -> dict[str, CatalogGroup]:
    """Take one live snapshot; do not count a superseded session as a second worker."""
    agents = []
    queries = []
    for worker in sorted(connections, key=lambda w: w.worker_id):
        agent = worker.register.coding_agent
        peer = gateway.get(worker.worker_id)
        if (worker.registered and worker.register.execute and agent is not None
                and peer is not None and peer.session is worker.session):
            agents.append(agent)
            queries.append(_query(peer))
    replies = await asyncio.gather(*queries)
    groups = {}
    for backend, levels in (("codex", get_args(CodexEffort)), ("amp", get_args(AmpEffort))):
        entries = [reply for agent, reply in zip(agents, replies, strict=True) if agent == backend]
        successful = [entry for entry in entries if entry is not None]
        by_id: dict[str, CatalogModel] = {}
        for models in successful:
            for model in models:
                merged = by_id.setdefault(model.id, model.model_copy(update={"efforts": []}))
                merged.efforts = [level for level in levels if level in merged.efforts or level in model.efforts]
        groups[backend] = CatalogGroup(
            status="unavailable" if not successful else "partial" if len(successful) < len(entries) else "ok",
            models=[by_id[key] for key in sorted(by_id)],
        )
    return groups
