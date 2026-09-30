import hashlib
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.protocol import PROTOCOL_VERSION
from scripts.smoke.recovery import freeze_connection, verify_bundle


def bundle(path: Path):
    path.mkdir()
    for name in ('database.dump', 'worker.tar.gz'):
        (path / name).write_bytes(b'fixture')
    manifest = {
        'format': 1, 'scope': 'isolated-fixture', 'complete': True,
        'backup_id': 'fixture', 'created_at': '2026-09-18T00:00:00+00:00',
        'revision': 'fixture-revision', 'protocol': PROTOCOL_VERSION, 'schema': 'recovery_fixture',
        'worker': {'id': 'fixture-worker', 'root_identity': ['/fixture/root', 1, 2]},
        'steps': {'stopped': True, 'dumped': True, 'archived': True},
        'files': {name: hashlib.sha256(b'fixture').hexdigest()
                  for name in ('database.dump', 'worker.tar.gz')},
    }
    (path / 'manifest.json').write_text(json.dumps(manifest))
    return manifest


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'incomplete', 'escape', 'symlink'])
def test_restore_rejects_incomplete_or_untrusted_bundle_before_using_data(tmp_path, damage):
    path = tmp_path / 'backup'
    manifest = bundle(path)
    target = path / 'database.dump'
    if damage == 'missing':
        target.unlink()
    elif damage == 'corrupt':
        target.write_bytes(b'truncated')
    elif damage == 'incomplete':
        manifest['steps']['stopped'] = False
    elif damage == 'escape':
        manifest['files']['../outside'] = manifest['files'].pop('database.dump')
    else:
        target.rename(tmp_path / 'external.dump')
        target.symlink_to(tmp_path / 'external.dump')
    (path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        verify_bundle(path)


def test_verified_bundle_retains_identity_metadata(tmp_path):
    path = tmp_path / 'backup'
    expected = bundle(path)
    assert verify_bundle(path) == expected


def test_freeze_connection_preserves_environment_port_and_password_file(postgresql_url, tmp_path, monkeypatch):
    # Deployment URLs can omit values supplied by the libpq environment.
    settings = conninfo_to_dict(postgresql_url)
    with psycopg.connect(postgresql_url) as fixture:
        port = str(fixture.info.port)
    settings.pop('port', None)
    password_file = tmp_path / 'test.pgpass'
    password_file.write_text('127.0.0.1:*:*:*:fixture-only\n')
    password_file.chmod(0o600)
    monkeypatch.setenv('PGPORT', port)
    monkeypatch.setenv('PGPASSFILE', str(password_file))
    with psycopg.connect(make_conninfo(**settings)) as conn:
        params, env = freeze_connection(conn)
    assert params['port'] == env['PGPORT'] == port
    assert params['passfile'] == env['PGPASSFILE'] == str(password_file)
    assert env['PGHOSTADDR'] in ('127.0.0.1', '::1')
    # Reopening the frozen parameters must address the same isolated schema.
    with psycopg.connect(make_conninfo(**params)) as conn:
        assert conn.execute('SELECT current_schema()').fetchone()[0].startswith('test_')


def test_freeze_connection_prefers_explicit_dsn_and_preserves_tls_paths(postgresql_url, tmp_path, monkeypatch):
    # Explicit connection settings must override inherited deployment defaults.
    settings = conninfo_to_dict(postgresql_url)
    with psycopg.connect(postgresql_url) as fixture:
        settings['port'] = str(fixture.info.port)
    monkeypatch.setenv('PGPORT', '1')
    monkeypatch.setenv('PGPASSFILE', '/wrong/default')
    settings.update(passfile=str(tmp_path / 'explicit.pgpass'), sslrootcert=str(tmp_path / 'ca.pem'), sslmode='disable')
    with psycopg.connect(make_conninfo(**settings)) as conn:
        params, env = freeze_connection(conn)
    assert env['PGPORT'] == settings['port']
    assert env['PGPASSFILE'] == settings['passfile']
    assert env['PGSSLROOTCERT'] == settings['sslrootcert']
    assert params['sslmode'] == env['PGSSLMODE'] == 'disable'


def test_freeze_connection_rejects_unrepresentable_option_instead_of_ignoring_it(postgresql_url):
    # Refuse unsupported settings before a tool can silently use different ones.
    with psycopg.connect(postgresql_url, keepalives=0) as conn, pytest.raises(ValueError, match='keepalives'):
        freeze_connection(conn)
