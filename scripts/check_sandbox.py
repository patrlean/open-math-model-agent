"""Focused DockerSandbox self-test (no LLM involved).

Verifies: the image runs Python + scientific stack, the workdir bind-mount lets
artifacts land on the host, artifact detection works, and the wall-clock timeout
fires. Run:  ./.venv/bin/python -m scripts.check_sandbox
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from mathmodel.sandbox.docker import DockerSandbox

# Make sure the docker binary is findable from this process.
os.environ["PATH"] = os.environ.get("PATH", "") + ":/usr/local/bin:/opt/homebrew/bin"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sb = DockerSandbox(Path(tmp))

        print("[1] normal run: scientific stack + write an artifact")
        r = sb.exec_python(
            "import numpy as np, json, os\n"
            "os.makedirs('results', exist_ok=True)\n"
            "x = np.arange(5)\n"
            "json.dump({'sum': int(x.sum())}, open('results/out.json','w'))\n"
            "print('numpy', np.__version__, 'sum', int(x.sum()))\n"
        )
        print("  exit_code:", r.exit_code, "ok:", r.ok)
        print("  stdout:", r.stdout.strip())
        print("  artifacts:", r.artifacts)
        assert r.ok, "normal run failed"
        assert "results/out.json" in r.artifacts, "artifact not detected"
        assert (Path(tmp) / "results" / "out.json").exists(), "artifact not on host"

        print("\n[2] error surfacing: nonzero exit + stderr")
        r2 = sb.exec_python("raise ValueError('boom')")
        print("  exit_code:", r2.exit_code, "ok:", r2.ok)
        print("  stderr tail:", r2.stderr.strip().splitlines()[-1])
        assert not r2.ok and r2.exit_code != 0

        print("\n[3] timeout enforcement (limit=3s on a 30s sleep)")
        r3 = sb.exec_python("import time; time.sleep(30)", timeout=3)
        print("  timed_out:", r3.timed_out, "duration:", r3.duration_s)
        assert r3.timed_out, "timeout did not fire"

        print("\n[4] network isolation (default network=none)")
        r4 = sb.exec_python(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
            "    print('NETWORK REACHABLE')\n"
            "except OSError as e:\n"
            "    print('network blocked:', e.__class__.__name__)\n"
        )
        print("  stdout:", r4.stdout.strip())
        assert "REACHABLE" not in r4.stdout, "network should be isolated"

    print("\nOK: DockerSandbox passes all checks.")


if __name__ == "__main__":
    main()
