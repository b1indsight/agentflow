"""Wire format shared by the console's structured pagination cursors."""

import base64
from typing import Self

from pydantic import BaseModel, ConfigDict

from app.contracts.console import ConsoleFailure


class Cursor(BaseModel):
    """Encode strict JSON models as URL-safe Base64 tokens."""

    model_config = ConfigDict(extra="forbid", strict=True)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, token: str) -> Self:
        try:
            return cls.model_validate_json(base64.b64decode(token, altchars=b"-_", validate=True))
        except ValueError:
            raise ConsoleFailure("cursor_invalid") from None
