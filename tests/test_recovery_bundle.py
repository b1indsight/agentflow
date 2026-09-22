import hashlib
import json
from pathlib import Path

import pytest

from scripts.smoke.recovery import verify_bundle


def bundle(path: Path):
    path.mkdir()
    for name in ('database.dump', 'worker.tar.gz'):
        (path / name).write_bytes(b'fixture')
    manifest = {
        'format': 1, 'scope': 'isolated-fixture', 'complete': True,
        'backup_id': 'fixture', 'created_at': '2026-09-18T00:00:00+00:00',
        'revision': 'fixture-revision', 'protocol': 9, 'schema': 'recovery_fixture',
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
