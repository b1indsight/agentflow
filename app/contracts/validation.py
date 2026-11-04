"""Separate recoverable payload rejection from invalid framing and handshakes.

Strict directional parsers remain the source of truth. Only a failed payload with
fully validated envelope and correlation headers may reach the rejection path.
No invalid arguments or validator diagnostics leave this boundary.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from app.contracts.protocol import (
    Message,
    ServerMessage,
    ToolCall,
    ToolResult,
    WorkerMessage,
    parse_server_message,
    parse_worker_message,
)


class PayloadHeaders(Message[ToolCall | ToolResult]):
    control: Literal[None]  # noqa: PYI061


class HeartbeatHeaders(Message[ToolCall | ToolResult]):
    control: Literal["heartbeat"]
    job_id: int
    assignment_token: str


type RejectedMessage = PayloadHeaders | HeartbeatHeaders

_HEADERS: TypeAdapter[RejectedMessage] = TypeAdapter(
    Annotated[RejectedMessage, Field(discriminator="control")]
)
_JSON = TypeAdapter(dict[str, JsonValue])


class PayloadRejected(ValueError):
    """Validated headers only; the connection owner must still authorize rejection."""

    def __init__(self, message: RejectedMessage) -> None:
        super().__init__("message payload failed validation")
        self.message = message


def _parse[Incoming: Message](raw: str, parse: Callable[[str], Incoming]) -> Incoming:
    try:
        return parse(raw)
    except ValidationError as error:
        # Envelope/model-level invariants cannot be converted into parameter failures.
        if any("payload" not in issue["loc"] for issue in error.errors(include_input=False)):
            raise
        data = _JSON.validate_json(raw)
        payload = data.get("payload")
        if not isinstance(payload, list) or not payload:
            raise
        headers: list[JsonValue] = []
        for item in payload:
            if not isinstance(item, dict) or isinstance(item.get("error"), dict):
                raise  # Never answer an error with another error, even in a mixed batch.
            headers.append(
                {
                    key: item[key]
                    for key in ("type", "method", "message_id", "reply_to")
                    if key in item
                }
            )
        data["payload"] = headers
        message = _HEADERS.validate_python(data)
        raise PayloadRejected(message) from None


def parse_worker_input(raw: str) -> WorkerMessage:
    """Established-session input; Register still uses the strict handshake parser."""
    return _parse(raw, parse_worker_message)


def parse_server_input(raw: str) -> ServerMessage:
    """Registered/recovery control failures remain fatal rather than partial handshakes."""
    return _parse(raw, parse_server_message)
