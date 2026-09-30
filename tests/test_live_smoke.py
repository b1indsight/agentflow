"""The live harness must never turn a retry into a second GitHub write."""

import json
from types import SimpleNamespace

import pytest

from app.contracts.protocol import PROTOCOL_VERSION
from app.live_smoke import GitHubProbe, SmokeError, SmokeSession


class FakeGitHub:
    def __init__(self):
        self.issue = None
        self.writes = 0
        self.fail = False

    def find_issue(self, repo, marker):
        return self.issue

    def create_issue(self, repo, marker):
        self.writes += 1
        if self.fail:
            raise SmokeError("request outcome unknown")
        self.issue = 7
        return 7


def session(tmp_path, gh):
    return SmokeSession("owner/smoke", "run-1", tmp_path, gh)


def test_default_mode_does_not_write_and_requires_explicit_repo_allowlist(tmp_path):
    gh = FakeGitHub()
    with session(tmp_path, gh) as smoke:
        assert smoke.ensure_issue() is None
        with pytest.raises(SmokeError):
            smoke.ensure_issue(allow_write=True, allowed_repo="owner/production")
    assert gh.writes == 0


def test_repeated_create_reuses_issue_and_persisted_identity(tmp_path):
    gh = FakeGitHub()
    with session(tmp_path, gh) as smoke:
        assert smoke.ensure_issue(allow_write=True, allowed_repo="owner/smoke") == 7
    with session(tmp_path, gh) as smoke:
        assert smoke.ensure_issue(allow_write=True, allowed_repo="owner/smoke") == 7
    assert gh.writes == 1


def test_unknown_create_outcome_never_blindly_retries(tmp_path):
    gh = FakeGitHub()
    gh.fail = True
    with session(tmp_path, gh) as smoke, pytest.raises(SmokeError):
        smoke.ensure_issue(allow_write=True, allowed_repo="owner/smoke")
    gh.fail = False
    with session(tmp_path, gh) as smoke, pytest.raises(SmokeError, match="unknown"):
        smoke.ensure_issue(allow_write=True, allowed_repo="owner/smoke")
    assert gh.writes == 1
    gh.issue = 7  # The next remote observation proves the original write succeeded.
    with session(tmp_path, gh) as smoke:
        assert smoke.ensure_issue() == 7


@pytest.mark.parametrize("repo,run", [("--bad", "run"), ("owner/repo", "../../escape"),
                                     ("https://token@host/a/b", "run")])
def test_invalid_identifiers_cannot_reach_tools_or_paths(tmp_path, repo, run):
    with pytest.raises(SmokeError):
        SmokeSession(repo, run, tmp_path, FakeGitHub())


def test_same_run_cannot_be_owned_by_two_processes(tmp_path):
    gh = FakeGitHub()
    with session(tmp_path, gh), pytest.raises(SmokeError, match="already"), session(tmp_path, gh):
        pass


def test_failed_gh_command_never_exposes_stderr(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout="", stderr="token-secret"))
    with pytest.raises(SmokeError) as error:
        GitHubProbe().find_issue("owner/smoke", "marker")
    assert "token-secret" not in str(error.value)


def test_truncated_issue_listing_is_not_absence(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps([{"number": n+1, "body": ""} for n in range(100)])))
    with pytest.raises(SmokeError, match="incomplete"):
        GitHubProbe().find_issue("owner/smoke", "marker")


def test_observe_reads_without_creating_work_and_does_not_claim_partial_success(store, tmp_path, monkeypatch):
    from app.domain.models import DeliveryState
    from app.live_smoke import Pull

    gh = FakeGitHub()
    gh.issue = 7
    flow = store.create_flow("owner/smoke", 7, 42)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, "flow/7")
    delivery.state, delivery.pr_number, delivery.head_sha = DeliveryState.OPEN, 8, "a"*40
    store.update_delivery(delivery)
    probe = GitHubProbe()
    remote = Pull(number=8, merged=False, head={"sha": "a"*40, "ref": "flow/7",
                  "repo": {"full_name": "owner/smoke"}}, base={"sha": "b"*40, "ref": "main",
                  "repo": {"full_name": "owner/smoke"}})
    monkeypatch.setattr(probe, "pull", lambda *args: remote)
    with session(tmp_path, gh) as smoke:
        smoke.ensure_issue()
        observed = smoke.observe(store, probe, "test-revision")
        assert observed.pr == 8 and not observed.core_passed
        assert smoke.state.observed_heads == ["a"*40]
        smoke.observe(store, probe, "test-revision")
        assert smoke.state.observed_heads == ["a"*40]
        remote.head.ref = "unexpected"
        with pytest.raises(SmokeError, match="identity"):
            smoke.observe(store, probe, "test-revision")
    assert not store.list_jobs(flow.id)
    assert "Please" not in next(tmp_path.glob("*.json")).read_text()


def test_deadline_prevents_even_read_requests(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("must not invoke gh"))
    with pytest.raises(SmokeError, match="deadline"):
        GitHubProbe(deadline=1).find_issue("owner/smoke", "marker")


def test_core_pass_requires_three_verified_heads_and_matching_merge(store, tmp_path, monkeypatch):
    from app.domain.models import DeliveryState, FlowState, JobKind
    from app.live_smoke import Pull

    gh = FakeGitHub()
    gh.issue = 7
    flow = store.create_flow("owner/smoke", 7, 42)
    flow.state = FlowState.SUCCEEDED
    attempt = store.create_attempt(flow.id)
    attempt.revision_cycles = 2
    store.update_attempt(attempt)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, "flow/7")
    delivery.state, delivery.pr_number = DeliveryState.MERGED, 8
    delivery.merge_commit_sha = "d"*40
    store.update_delivery(delivery)
    archive = store.enqueue_job(flow.id, JobKind.ARCHIVE)
    store.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)
    with store.connection() as conn:
        conn.execute("INSERT INTO flow_executions(flow_id,worker_id,repo_identity,bound_at,released_at) "
                     "VALUES (%s,'worker-1','worker-1:owner/smoke',%s,%s)", (flow.id, store.now(), store.now()))
        conn.execute("UPDATE jobs SET state='done',result_applied_at=%s WHERE id=%s", (store.now(), archive.id))
    remote = Pull(number=8, merged=True, merge_commit_sha="d"*40,
                  head={"sha": "a"*40, "ref": "flow/7", "repo": {"full_name": "owner/smoke"}},
                  base={"sha": "b"*40, "ref": "main", "repo": {"full_name": "owner/smoke"}})
    probe = GitHubProbe()
    monkeypatch.setattr(probe, "pull", lambda *args: remote)
    with session(tmp_path, gh) as smoke:
        smoke.ensure_issue()
        for letter in ["a", "b", "c"]:
            delivery.head_sha = remote.head.sha = letter*40
            store.update_delivery(delivery)
            result = smoke.observe(store, probe, "actual-revision")
            assert result.core_passed == (letter == "c")
        remote.merge_commit_sha = "e"*40
        assert not smoke.observe(store, probe, "actual-revision").core_passed


@pytest.mark.parametrize('observe,expected', [(False, 0), (True, 2)])
def test_cli_missing_issue_is_not_successful_observation(tmp_path, observe, expected):
    import os
    import subprocess
    import sys
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\nprint("[]")\n')
    gh.chmod(0o700)
    result = subprocess.run([sys.executable, '-m', 'app.live_smoke', '--repo', 'owner/smoke',
                             '--run-id', 'missing', '--state-dir', str(tmp_path / 'state'),
                             *(['--observe'] if observe else [])],
                            env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}'},
                            capture_output=True, timeout=10, check=False)
    assert result.returncode == expected


def test_cli_deadline_bounds_unresponsive_database_connection(tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time
    # A TCP listener accepts the kernel handshake but never speaks PostgreSQL.
    # The pool's normal acquisition timeout must not exceed the smoke's total budget.
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\nprint(\'[{{"number": 1, "body": "<!-- flowtool-smoke:timeout -->"}}]\')\n')
    gh.chmod(0o700)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        url = f'postgresql://fixture@127.0.0.1:{listener.getsockname()[1]}/fixture'
        started = time.monotonic()
        try:
            result = subprocess.run([
                sys.executable, '-m', 'app.live_smoke', '--repo', 'owner/smoke',
                '--run-id', 'timeout', '--state-dir', str(tmp_path / 'state'),
                '--observe', '--source-revision', 'fixture', '--wait-seconds', '1'],
                env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
                     'FLOWTOOL_DATABASE_URL': url}, capture_output=True, timeout=4, check=False)
        except subprocess.TimeoutExpired:
            pytest.fail('database exceeded total smoke deadline')
    assert result.returncode == 2
    assert time.monotonic() - started < 4


def test_parent_sigterm_reaps_observer_and_gh_and_releases_state_lock(tmp_path):
    import os
    import signal
    import socket
    import subprocess
    import sys

    # Signal only the watchdog after gh proves the observer owns the session lock.
    # Inherited stdout keeps communicate blocked if either descendant survives.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(10)
        gh = tmp_path / 'gh'
        gh.write_text(
            f'#!{sys.executable}\nimport os, socket, time\n'
            f'with socket.create_connection(("127.0.0.1", {listener.getsockname()[1]})) as s:\n'
            '    s.sendall(str(os.getpgrp()).encode())\n'
            '    time.sleep(60)\n'
        )
        gh.chmod(0o700)
        process = subprocess.Popen([
            sys.executable, '-m', 'app.live_smoke', '--repo', 'owner/smoke',
            '--run-id', 'terminated', '--state-dir', str(tmp_path / 'state'),
            '--wait-seconds', '30'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}'})
        group = None
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                group = int(connection.recv(64))
                process.terminate()
                process.communicate(timeout=5)
                assert connection.recv(1) == b''  # gh also released its live socket
            assert process.returncode == 128 + signal.SIGTERM
            with SmokeSession('owner/smoke', 'terminated', tmp_path / 'state', FakeGitHub()):
                pass
        finally:
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
