"""Durable flow.open_pr receipts; their JSON shapes remain compatible with protocol v9."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PreparedDelivery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["prepared"]
    head_sha: str = Field(min_length=1)
    delivery_version: int = Field(ge=0)


class OpenedDelivery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pr_number: int = Field(gt=0)
    url: str
    head_sha: str = Field(min_length=1)
    delivery_version: int = Field(ge=0)


DeliveryReceipt = PreparedDelivery | OpenedDelivery
