"""Model metadata exposed by coding workers and the owner-only settings page."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class CatalogModel(BaseModel, extra="forbid"):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$", max_length=256)
    displayName: str = Field(min_length=1, max_length=256)
    efforts: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]] = Field(max_length=32)


class CatalogGroup(BaseModel):
    status: Literal["ok", "partial", "unavailable"]
    models: list[CatalogModel]
