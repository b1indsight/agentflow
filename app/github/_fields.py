"""Pydantic declarations shared by immutable resource observations."""

from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined


def unobserved_field() -> FieldInfo:
    """Omit unknown facts from serialization while preserving an observed None."""
    return FieldInfo.from_field(default_factory=lambda: PydanticUndefined,
                                exclude_if=lambda value: value is PydanticUndefined)
