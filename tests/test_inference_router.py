"""One logical inference owns routing, retries, identity and its total budget."""

import asyncio
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.controler.config import ServerSettings
from app.controler.runtime import ServerRuntime
from app.controler.workers.inference import InferencePool
from app.domain.inference_models import (
    InferenceCallFailed,
    InferenceError,
    InferenceFailure,
    InferenceMessage,
    InferenceOutput,
    InferenceTask,
)
from app.inference.bub import BubInference
from app.inference.router import InferenceRouter


def task(**changes):
    return InferenceTask(messages=[InferenceMessage(role="user", content="Summarize this text")],
                         timeout_seconds=10, **changes)


class Backend:
    def __init__(self, *failures):
        self.failures = list(failures)
        self.calls = []

    async def infer(self, request, **selection):
        self.calls.append((request, selection))
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise InferenceError(InferenceFailure(code=failure), worker_id="worker-1",
                                     backend="codex/v1")
        return InferenceOutput(request_id=request.request_id, request_digest=request.digest,
                               text="A summary", adapter_version="codex/v1")


@pytest.mark.parametrize("model,key,base,remote", [
    ("", None, None, True), (" ", " ", " ", True),
    ("openai:test", None, None, False), ("", "key", None, False),
    ("", None, "https://provider.invalid", False),
    ("invalid-model", "key", None, False), ("openai:test", "key", None, False),
])
async def test_server_routes_only_wholly_unconfigured_bub_to_workers(monkeypatch, model, key, base, remote):
    # An API-base-only deployment is misconfigured, not permission to spend worker credentials.
    backend = Backend()
    monkeypatch.setattr("app.controler.runtime.Database", Mock())
    monkeypatch.setattr("app.controler.runtime.GitHubApp", Mock())
    monkeypatch.setattr("app.controler.runtime.configure_logging", Mock())
    monkeypatch.setattr(InferencePool, "infer", backend.infer)
    runtime = ServerRuntime(ServerSettings.model_construct(
        database_url="postgresql://unused/test", worker_tokens={}, judge_model=model,
        judge_api_key=SecretStr(key) if key is not None else None, judge_api_base=base,
    ))
    try:
        if remote:
            assert (await runtime.inference.infer(task())).text == "A summary"
            assert len(backend.calls) == 1
        else:
            local = runtime.inference.local
            assert isinstance(local, BubInference)
            if local.configuration_failure():
                with pytest.raises(InferenceCallFailed, match="configuration"):
                    await runtime.inference.infer(task())
            assert not backend.calls
    finally:
        await runtime.access.close()


async def test_unknown_remote_outcome_retries_with_fresh_identity_and_frozen_input():
    # A partition may lose a sent request's reply; a new worker may recompute the answer.
    backend = Backend("connection_closed", "execution_timeout")
    original = task()
    result = await InferenceRouter(local=None, workers=backend).infer(original)
    calls = [call for call, _ in backend.calls]
    assert result.text == "A summary" and len(calls) == 3
    assert len({call.request_id for call in calls} | {original.request_id}) == 4
    assert all(call.messages == original.messages for call in calls)
    assert calls[0].timeout_seconds >= calls[1].timeout_seconds >= calls[2].timeout_seconds
    assert backend.calls[1][1]["excluded"] == frozenset({"worker-1"})


@pytest.mark.parametrize("last", ["execution_timeout", "authentication"])
async def test_three_failed_attempts_produce_a_safe_stable_summary(last):
    backend = Backend("execution_timeout", "execution_timeout", last)
    with pytest.raises(InferenceCallFailed) as caught:
        await InferenceRouter(local=None, workers=backend).infer(task())
    summary = caught.value.summary
    assert summary.attempts == summary.max_attempts == 3
    assert summary.notify and summary.last_failure_code == last
    assert summary.last_worker_id == "worker-1" and summary.backend == "codex/v1"
    assert summary.stop_reason == ("non_retryable" if last == "authentication" else "max_attempts")


@pytest.mark.parametrize("failure", ["authentication", "invalid_response", "output_limit", "backend_error"])
async def test_permanent_failures_are_not_retried(failure):
    backend = Backend(failure)
    with pytest.raises(InferenceCallFailed) as caught:
        await InferenceRouter(local=None, workers=backend).infer(task())
    assert len(backend.calls) == 1 and not caught.value.summary.notify


async def test_local_failures_never_fall_back_to_workers():
    local, workers = Backend("execution_timeout"), Backend()
    assert (await InferenceRouter(local=local, workers=workers).infer(task())).text == "A summary"
    assert len(local.calls) == 2 and not workers.calls


async def test_no_candidate_is_zero_attempts_without_a_false_notice():
    backend = Backend("unavailable")
    with pytest.raises(InferenceCallFailed) as caught:
        await InferenceRouter(local=None, workers=backend).infer(task())
    assert caught.value.summary.attempts == 0 and not caught.value.summary.notify


async def test_retry_after_beyond_budget_stops_without_another_attempt():
    class RateLimited(Backend):
        async def infer(self, request, **selection):
            self.calls.append((request, selection))
            raise InferenceError(InferenceFailure(code="rate_limited", retry_after_seconds=30))

    backend = RateLimited()
    with pytest.raises(InferenceCallFailed) as caught:
        await InferenceRouter(local=backend, workers=Backend()).infer(task())
    assert len(backend.calls) == 1
    assert caught.value.summary.stop_reason == "deadline" and caught.value.summary.notify


async def test_cancellation_never_starts_another_attempt():
    class Cancelled(Backend):
        async def infer(self, request, **selection):
            raise asyncio.CancelledError

    workers = Backend()
    with pytest.raises(asyncio.CancelledError):
        await InferenceRouter(local=Cancelled(), workers=workers).infer(task())
    assert not workers.calls


async def test_late_success_after_execution_budget_is_not_accepted():
    # Process cleanup can finish after the answer deadline, but must not extend acceptance.
    class Late(Backend):
        async def infer(self, request, **selection):
            await asyncio.sleep(0.02)
            return await super().infer(request, **selection)

    backend = Late()
    request = task().model_copy(update={"timeout_seconds": 0.005})
    with pytest.raises(InferenceCallFailed) as caught:
        await InferenceRouter(local=backend, workers=Backend()).infer(request)
    assert caught.value.summary.stop_reason == "deadline"
    assert caught.value.summary.attempts == 1
