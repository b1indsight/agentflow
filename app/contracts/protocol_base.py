"""Shared strict envelope for execution and opt-in protocol extensions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

PROTOCOL_VERSION: Literal[13] = 13


class ProtocolModel(BaseModel):
    """Reject unknown fields at the transport boundary."""

    model_config = ConfigDict(extra="forbid")


class Message(ProtocolModel):
    protocol_version: Literal[13] = PROTOCOL_VERSION
