"""Explicit current-wire frame builders for runtime peers; business items remain typed."""

from app.contracts.protocol import (
    Registered,
    ServerFrame,
    ServerPayload,
    WorkerFrame,
    WorkerPayload,
    frame_header,
)


def server_frame(*items: ServerPayload, epoch: str) -> ServerFrame:
    return ServerFrame(**frame_header(epoch), control=None, payload=list(items))


def worker_frame(*items: WorkerPayload, epoch: str) -> WorkerFrame:
    return WorkerFrame(**frame_header(epoch), control=None, payload=list(items))


def registered(*items: ServerPayload, epoch: str) -> Registered:
    return Registered(
        **frame_header(epoch),
        control="registered",
        payload=list(items),
        heartbeat_interval=60,
        recovery=None,
    )
