"""Private generic bub 0.4.1 stdin/stdout adapter: no tools, tapes or SDK retries."""

import asyncio
import contextlib
import json
import math
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from loguru import logger

from app.domain.inference_models import (
    MAX_INPUT_BYTES,
    InferenceFailure,
    InferenceOutput,
    InferenceTask,
)
from app.domain.json import strict_json
from app.inference.bub import ADAPTER_VERSION

# Keep all SDK imports and diagnostics off the result channel.
with contextlib.redirect_stdout(sys.stderr):
    import anthropic
    import openai
    from any_llm.exceptions import MissingApiKeyError, UnsupportedProviderError
    from bub.builtin.model_runner import ModelRunner
    from bub.builtin.settings import AgentSettings


def classify(error: Exception) -> InferenceFailure:
    """Use typed SDK facts, never stderr or exception-message pattern matching."""
    if isinstance(error, (openai.APITimeoutError, anthropic.APITimeoutError)):
        return InferenceFailure(code="execution_timeout")
    if isinstance(error, (openai.APIConnectionError, anthropic.APIConnectionError)):
        return InferenceFailure(code="backend_unavailable")
    if isinstance(error, (MissingApiKeyError, UnsupportedProviderError)):
        return InferenceFailure(code="configuration")
    if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        if error.status_code in {401, 403}:
            return InferenceFailure(code="authentication")
        body = error.body
        detail = body.get("error", body) if isinstance(body, dict) else None
        code = detail.get("code") or detail.get("type") if isinstance(detail, dict) else None
        if code in ("insufficient_quota", "billing_hard_limit_reached", "credit_balance_too_low"):
            return InferenceFailure(code="configuration")
        # An opaque 429 could be permanent quota exhaustion. Only known transient
        # provider codes justify another paid attempt.
        limited = error.status_code == 429 and code in ("rate_limit_exceeded", "rate_limit_error")
        if limited or 500 <= error.status_code < 600:
            delay = None
            value = error.response.headers.get("retry-after")
            if value:
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        pass
                if delay is not None:
                    delay = max(0, delay) if math.isfinite(delay) else None
            return InferenceFailure(code="rate_limited" if limited else "backend_unavailable",
                                    retry_after_seconds=delay)
    return InferenceFailure(code="backend_error")


async def run(task: InferenceTask) -> InferenceOutput | InferenceFailure:
    """Complete arbitrary messages once; callers own domain schemas and semantic actions."""
    class ExplicitSettings(AgentSettings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                       dotenv_settings, file_secret_settings):
            return (init_settings,)

    model = os.environ["FLOWTOOL_JUDGE_MODEL"]
    messages = [message.model_dump() for message in task.messages]
    if task.output_schema is not None:
        schema = json.dumps(task.output_schema, ensure_ascii=False, separators=(",", ":"))
        messages.append({"role": "system", "content":
            "Return exactly one JSON value matching this schema, without Markdown:\n" + schema})
    try:
        settings = ExplicitSettings(
            model=model, api_key=os.environ["FLOWTOOL_JUDGE_API_KEY"],
            api_base=os.environ.get("FLOWTOOL_JUDGE_API_BASE"), fallback_models=[],
            max_tokens=16_000, client_args={"max_retries": 0},
        )
        response = await ModelRunner(settings, hooks=None).completion_response(
            model=model, tools=[], messages=messages,
        )
        raw = ""
        if isinstance(response, AsyncIterator):
            finished = False
            async for chunk in response:
                if len(chunk.choices) > 1:
                    return InferenceFailure(code="invalid_response")
                for choice in chunk.choices:
                    if choice.delta.tool_calls or choice.index != 0:
                        return InferenceFailure(code="invalid_response")
                    if choice.finish_reason is not None:
                        if choice.finish_reason != "stop":
                            return InferenceFailure(code="invalid_response")
                        finished = True
                    raw += choice.delta.content or ""
                    if len(raw.encode()) > task.max_output_bytes:
                        return InferenceFailure(code="output_limit")
            if not finished:
                return InferenceFailure(code="invalid_response")
        else:
            if (len(response.choices) != 1 or response.choices[0].message.tool_calls
                    or response.choices[0].finish_reason != "stop"):
                return InferenceFailure(code="invalid_response")
            raw = response.choices[0].message.content or ""
            if len(raw.encode()) > task.max_output_bytes:
                return InferenceFailure(code="output_limit")
    except Exception as error:  # noqa: BLE001 — sanitized process boundary, never serialize errors.
        return classify(error)
    return InferenceOutput(request_id=task.request_id, request_digest=task.digest,
                           text=raw, model=model, adapter_version=ADAPTER_VERSION)


def main() -> None:
    logger.remove()
    task = InferenceTask.model_validate(strict_json(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)))
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(run(task))
    sys.stdout.write(result.model_dump_json())


if __name__ == "__main__":
    main()
