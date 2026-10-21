"""Static contracts for role-specific coding CLI installation.

Docker is intentionally not required for this check; the live role smoke runs on Linux CI.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]
AMP_VERSION = "0.0.1792555249-g702cd1"
AMP_SHA256 = "41a2e3680c63091d7c0293e2ad702af95772f9f35d977b74c7794fa40e45b5c9"


def test_amp_install_is_pinned_to_official_linux_x64_artifact_in_worker_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    worker = dockerfile.split("FROM runtime AS worker", 1)[1]
    serve = dockerfile.split("FROM runtime AS serve", 1)[1].split("FROM runtime AS worker", 1)[0]

    assert f"ARG AMP_VERSION={AMP_VERSION}" in worker
    assert f"ARG AMP_SHA256={AMP_SHA256}" in worker
    assert "https://static.ampcode.com/cli/${AMP_VERSION}/amp-linux-x64.gz" in worker
    assert 'echo "${AMP_SHA256}  /tmp/amp" | sha256sum -c -' in worker
    assert "/usr/local/bin/amp" in worker
    assert "amp" not in serve
    assert "cli-version.txt" not in dockerfile


def test_smoke_and_examples_keep_amp_worker_scoped() -> None:
    smoke = (ROOT / "scripts/smoke/docker.sh").read_text()
    environment = (ROOT / "deploy/docker/worker.env.example").read_text()
    worker = (ROOT / "deploy/k8s/base/worker.yaml").read_text()

    assert 'docker run --rm --entrypoint amp "$FLOWTOOL_WORKER_IMAGE" --version' in smoke
    assert 'docker run --rm --entrypoint amp "$FLOWTOOL_SERVE_IMAGE" --version' in smoke
    assert "FLOWTOOL_CODING_AGENT=codex" in environment
    assert "FLOWTOOL_AMP_RUN_TIMEOUT_SECONDS=1200" in environment
    assert "AMP_API_KEY" in environment
    assert "name: FLOWTOOL_CODING_AGENT" in worker
    assert "name: FLOWTOOL_AMP_RUN_TIMEOUT_SECONDS" in worker
