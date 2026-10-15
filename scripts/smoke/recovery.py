"""Opt-in local fixture drill, never a production backup/deployment command.

Run with `uv run python -m scripts.smoke.recovery --help`. It creates and restores
only its own random PostgreSQL schema and leaves evidence/resources for inspection.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    Assignment,
    AssignmentSecret,
    InitialTask,
    PushSecret,
)
from app.domain.models import JobKind
from app.persistence.store import Store
from app.worker.state.session import SessionError, WorkerSession

FILES = {'database.dump', 'worker.tar.gz'}


def freeze_connection(conn: psycopg.Connection) -> tuple[dict[str, str], dict[str, str]]:
    """Flatten libpq's resolved defaults/service/URL into one private tool environment.

    Use libpq's own environment mapping, not a hand-maintained subset. Refuse options
    that cannot be passed this way before creating fixture resources; never put secrets
    in command arguments or reports. Pin the actual loopback peer for every connection.
    """
    peer = conn.info.hostaddr
    if not ipaddress.ip_address(peer).is_loopback:
        raise ValueError('resolved database peer must be loopback')
    params: dict[str, str] = {}
    env = dict(os.environ)
    for option in psycopg.pq.Conninfo.get_defaults():
        if option.envvar:
            env.pop(option.envvar.decode(), None)
    for option in conn.pgconn.info:
        key = option.keyword.decode()
        if option.val is None or key == 'service':
            continue
        if not option.envvar:
            if option.val:
                raise ValueError(f'connection option has no safe tool environment mapping: {key}')
            continue
        value = option.val.decode()
        params[key] = value
        env[option.envvar.decode()] = value
    params['hostaddr'] = env['PGHOSTADDR'] = peer
    return params, env


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_bundle(path: Path) -> dict:
    """Reject incomplete, relocated or corrupted fixture artifacts before restore.

    Hashes detect corruption, not malicious replacement of both data and manifest.
    Trust the private backup store; never restore an untrusted PostgreSQL archive.
    """
    manifest_path = path / 'manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('manifest must be a regular file')
    try:
        manifest = json.loads(manifest_path.read_text())
        valid = (manifest['format'] == 1 and manifest['scope'] == 'isolated-fixture'
                 and manifest['complete'] is True and manifest['revision']
                 and manifest['backup_id'] and manifest['created_at']
                 and manifest['protocol'] == PROTOCOL_VERSION
                 and re.fullmatch(r'recovery_[a-z0-9_]+', manifest['schema'])
                 and manifest['worker']['id']
                 and len(manifest['worker']['root_identity']) == 3
                 and manifest['steps'] == {'stopped': True, 'dumped': True, 'archived': True}
                 and set(manifest['files']) == FILES)
        if not valid:
            raise ValueError('incomplete or incompatible manifest')
        for name, expected in manifest['files'].items():
            target = path / name
            if target.is_symlink() or not target.is_file() or digest(target) != expected:
                raise ValueError('missing or corrupt backup artifact')
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid backup bundle') from exc
    return manifest


def drill(work: Path, postgres_bin: Path, revision: str) -> dict:
    """Create a synthetic active assignment, cold-copy, restore and prove blocking."""
    base = os.environ.get('FLOWTOOL_TEST_DATABASE_URL', '')
    options = conninfo_to_dict(base)
    if (options.get('host') not in {'127.0.0.1', '::1', 'localhost'}
            or not options.get('dbname') or options.get('service') or options.get('options')
            or options.get('hostaddr')):
        raise ValueError('explicit loopback test database URL without overrides required')
    # Fail before creating data when the operator supplied unavailable/mismatched tools.
    versions = {}
    for tool in ('pg_dump', 'pg_restore'):
        versions[tool] = subprocess.check_output([str(postgres_bin / tool), '--version'], text=True).strip()
    with psycopg.connect(base) as conn:
        major = conn.info.server_version // 10000
        options, env = freeze_connection(conn)
    base = make_conninfo(**options)
    if any(f'PostgreSQL) {major}.' not in v for v in versions.values()):
        raise ValueError('dump/restore tools must match the server major version')
    os.umask(0o077)
    work.mkdir(mode=0o700, parents=False, exist_ok=False)
    schema = 'recovery_' + uuid4().hex
    (work / 'resources.json').write_text(json.dumps({'schema': schema, 'scope': 'isolated-fixture'}))
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    scoped = make_conninfo(base, options=f"{options.get('options', '')} -c search_path={schema}".strip())
    root, home, backup = work / 'live/root', work / 'live/home', work / 'backup'
    for path in (root, home, backup):
        path.mkdir(mode=0o700, parents=True)
    (home / 'fake-session').write_text('fixture-only-no-auth')
    store = Store(scoped)
    try:
        store.init_db()
        flow = store.flows.create_flow('fixture/no-network', 1, 1)
        attempt = store.flows.create_attempt(flow.id)
        flow.current_attempt_id = attempt.id
        store.flows.update_flow(flow)
        store.workers.worker_connected('fixture-worker', PROTOCOL_VERSION, 'fixture-epoch')
        job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        if not store.workers.assign_job('fixture-worker', 'fixture-token', 'fixture-epoch'):
            raise RuntimeError('fixture assignment failed')
        if not store.workers.start_assignment(job.id, attempt.id, 'fixture-worker', 'fixture-token', 'fixture-epoch'):
            raise RuntimeError('fixture start failed')
        store.webhooks.receive('fixture-delivery', 'ping', {}, 'a' * 64)
        journal = WorkerSession(root)
        journal.record_received(Assignment(
            job_id=job.id, flow_id=flow.id, assignment_token='fixture-token',
            connection_epoch='fixture-epoch', secret=AssignmentSecret(push=PushSecret(
                token=SecretStr('fixture-not-a-credential'), repository=flow.repo,
                expires_at=datetime.now(UTC))),
            task=InitialTask(flow_id=flow.id, attempt_id=attempt.id, repo=flow.repo,
                             issue_number=1, issue_title='fixture', issue_body='', branch='fixture'),
        ))
        original = journal.record_started()
    finally:
        store.close()
    # No serve, worker, executor or GitHub client is started by this fixture.

    def pg(tool: str, *args: str) -> None:
        # Do not copy database credentials to argv or relay stderr into CI logs.
        result = subprocess.run([str(postgres_bin / tool), *args], env=env,
                                capture_output=True, timeout=120, check=False)
        if result.returncode:
            raise RuntimeError(f'{tool} failed; inspect the isolated test database')

    pg('pg_dump', '--format=custom', '--no-owner', '--no-acl', '--schema', schema,
       '--file', str(backup / 'database.dump'))
    with tarfile.open(backup / 'worker.tar.gz', 'w:gz') as archive:
        archive.add(root, arcname='root')
        archive.add(home, arcname='home')
    manifest = {'format': 1, 'scope': 'isolated-fixture', 'complete': True, 'backup_id': schema,
                    'created_at': datetime.now(UTC).isoformat(), 'revision': revision,
                    'protocol': PROTOCOL_VERSION, 'schema': schema,
                    'worker': {'id': 'fixture-worker', 'root_identity': original.root_identity},
                    'steps': {'stopped': True, 'dumped': True, 'archived': True},
                    'files': {name: digest(backup / name) for name in sorted(FILES)}}
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    verify_bundle(backup)
    started = time.monotonic()
    # This identifier was minted by this process, never accepted from input/manifest.
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    pg('pg_restore', '--dbname', make_conninfo(dbname=options['dbname']), '--exit-on-error', '--single-transaction',
       '--no-owner', '--no-acl', str(backup / 'database.dump'))
    restored = Store(scoped)
    try:
        restored_job = restored.jobs.get_job(job.id)
        binding = restored.workers.get_flow_execution(flow.id)
        if (restored.flows.get_flow(flow.id) != flow or not restored_job
                or restored_job.assignment_token != 'fixture-token'
                or not binding or binding.released_at is not None
                or len(restored.webhooks.list()) != 1):
            raise RuntimeError('restored database facts differ from fixture')
    finally:
        restored.close()
    destination = work / 'restored'
    destination.mkdir(mode=0o700)
    with tarfile.open(backup / 'worker.tar.gz') as archive:
        archive.extractall(destination, filter='data')
    if (destination / 'home/fake-session').read_text() != 'fixture-only-no-auth':
        raise RuntimeError('HOME restore mismatch')
    if WorkerSession(root).load() != original:
        raise RuntimeError('original journal changed')
    try:
        WorkerSession(destination / 'root').load()
    except SessionError as exc:
        if str(exc) != 'worker journal belongs to another worker root':
            raise
    else:
        raise RuntimeError('copied root incorrectly accepted execution identity')
    report = {'scope': 'isolated-fixture', 'schema': schema, 'revision': revision,
                  'protocol': PROTOCOL_VERSION, 'tools': versions,
                  'database_restore': 'passed', 'home_restore': 'passed',
                  'original_journal': 'unchanged', 'copied_root': 'blocked_as_expected',
                  'fixture_restore_seconds': round(time.monotonic() - started, 3),
                  'active_flow_recovery': 'NOT_VALIDATED', 'github_side_effects': 'NOT_EXECUTED'}
    (work / 'report.json').write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='authorize only a newly created local fixture schema')
    parser.add_argument('--work-dir', type=Path, required=True, help='fresh private directory; parent must exist')
    parser.add_argument('--postgres-bin', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; no operation performed')
    print(json.dumps(drill(args.work_dir.resolve(), args.postgres_bin.resolve(), args.revision), indent=2))


if __name__ == '__main__':
    main()
