"""Deterministic checks for Docker ownership labels and orphan cleanup.

Run with: ./.venv/bin/python -m scripts.check_docker_orphan_cleanup
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from mathmodel.sandbox import docker


def check_workdir_identity_is_stable_and_scoped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = root / "first"
        second = root / "second"
        assert docker.workdir_fingerprint(first) == docker.workdir_fingerprint(first)
        assert docker.workdir_fingerprint(first) != docker.workdir_fingerprint(second)


def check_cleanup_uses_both_labels_and_explicit_ids() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "ps", "-aq"]:
            return subprocess.CompletedProcess(cmd, 0, "abc123\ndef456\n", "")
        assert cmd == ["docker", "rm", "-f", "abc123", "def456"]
        return subprocess.CompletedProcess(cmd, 0, "abc123\ndef456\n", "")

    with tempfile.TemporaryDirectory() as tmp, patch.object(
        docker.subprocess, "run", side_effect=fake_run
    ):
        labels = docker.managed_container_labels(tmp)
        result = docker.cleanup_managed_containers(tmp)

    assert result.matched == ("abc123", "def456")
    assert result.removed == result.matched
    assert result.error is None
    assert calls[0].count("--filter") == 2
    assert f"label=com.mathmodel.managed=true" in calls[0]
    assert (
        f"label=com.mathmodel.workdir-sha256="
        f"{labels['com.mathmodel.workdir-sha256']}"
    ) in calls[0]


def check_run_code_container_receives_ownership_labels() -> None:
    class FinishedProcess:
        pid = 12345
        returncode = 0

        def communicate(self, timeout=None):
            del timeout
            return "", ""

    with tempfile.TemporaryDirectory() as tmp, patch.object(
        docker.subprocess,
        "Popen",
        return_value=FinishedProcess(),
    ) as popen:
        sandbox = docker.DockerSandbox(tmp)
        result = sandbox.exec_python("print('ok')", timeout=5)
        labels = docker.managed_container_labels(tmp)

    assert result.ok
    command = popen.call_args.args[0]
    assert f"com.mathmodel.managed={labels['com.mathmodel.managed']}" in command
    assert (
        "com.mathmodel.workdir-sha256="
        f"{labels['com.mathmodel.workdir-sha256']}"
    ) in command


def check_no_match_never_runs_remove() -> None:
    with patch.object(
        docker.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 0, "", ""),
    ) as run:
        result = docker.cleanup_managed_containers("/tmp/no-matching-run")
    assert result.matched == ()
    assert result.removed == ()
    assert run.call_count == 1


def check_missing_docker_is_nonfatal() -> None:
    with patch.object(
        docker.subprocess,
        "run",
        side_effect=FileNotFoundError("docker unavailable"),
    ):
        result = docker.cleanup_managed_containers("/tmp/docker-unavailable")
    assert result.removed == ()
    assert "docker unavailable" in (result.error or "")


def main() -> None:
    check_workdir_identity_is_stable_and_scoped()
    check_cleanup_uses_both_labels_and_explicit_ids()
    check_run_code_container_receives_ownership_labels()
    check_no_match_never_runs_remove()
    check_missing_docker_is_nonfatal()
    print("docker orphan cleanup checks: passed")


if __name__ == "__main__":
    main()
