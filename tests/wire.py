"""Explicit current-wire message builders for runtime peers; business items remain typed."""

from app.contracts.protocol import (
    Registered,
    ServerMessage,
    ServerPayload,
    WorkerMessage,
    WorkerPayload,
    message_header,
)


def server_message(*items: ServerPayload, epoch: str) -> ServerMessage:
    return ServerMessage(**message_header(epoch), control=None, payload=list(items))


def worker_message(*items: WorkerPayload, epoch: str) -> WorkerMessage:
    return WorkerMessage(**message_header(epoch), control=None, payload=list(items))


def registered(*, epoch: str) -> Registered:
    return Registered(
        **message_header(epoch),
        control="registered",
        payload=[],
        heartbeat_interval=60,
        recoveries=[],
        ready=True,
    )
