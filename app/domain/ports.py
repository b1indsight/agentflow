"""Domain review interface; callers bind concrete resources."""

from __future__ import annotations

from typing import Protocol

from app.domain.context_models import ReviewContext
from app.domain.judge_models import JudgeDecision


class Judge(Protocol):
    """Review interpretation; worker placement belongs to job assignment."""

    async def triage(self, context: ReviewContext) -> JudgeDecision: ...
