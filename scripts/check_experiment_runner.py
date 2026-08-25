"""Regression checks for the detached, source-frozen experiment runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mathmodel.experiment import (
    _finalize_named_paper,
    build_parser,
    discover_cases,
)


class _MockChatHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({
            "id": "mock-response",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "mock completed"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        benchmark = root / "benchmark"
        output = root / "experiments"
        for name in ("case-a", "case-b"):
            problem = benchmark / name / "problem"
            problem.mkdir(parents=True)
            (problem / "problem.txt").write_text(f"problem {name}", encoding="utf-8")
        (benchmark / "case-b" / "task.md").write_text(
            "custom frozen task", encoding="utf-8"
        )

        cases = discover_cases(benchmark)
        assert [case["name"] for case in cases] == ["case-a", "case-b"]
        assert all(case["valid"] for case in cases)

        parser = build_parser()
        args = parser.parse_args([
            "submit", "--benchmark", str(benchmark), "--output", str(output),
            "--label", "snapshot-test", "--dry-run",
        ])
        assert args.func(args) == 0

        runs = list(output.iterdir())
        assert len(runs) == 1
        run_dir = runs[0]
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["status"] == "prepared"
        assert len(manifest["cases"]) == 4
        assert manifest["settings"]["repetitions"] == 2
        assert manifest["settings"]["scheduling"] == "paired_repetitions"
        assert [
            (case["name"], case["repetition"], case["slug"])
            for case in manifest["cases"]
        ] == [
            ("case-a", 1, "case-a-run-1"),
            ("case-a", 2, "case-a-run-2"),
            ("case-b", 1, "case-b-run-1"),
            ("case-b", 2, "case-b-run-2"),
        ]
        assert len(manifest["source_sha256"]) == 64
        assert (run_dir / "source" / "mathmodel" / "experiment.py").is_file()
        assert not (run_dir / "source" / "mathmodel" / "dashboard").exists()
        assert (run_dir / "cases" / "case-b-run-1" / "task.txt").read_text() == "custom frozen task"
        assert (run_dir / "cases" / "case-b-run-2" / "task.txt").read_text() == "custom frozen task"

        paper_workspace = root / "paper-export-workspace"
        paper_dir = paper_workspace / "paper"
        paper_dir.mkdir(parents=True)
        canonical_pdf = paper_dir / "main.pdf"
        canonical_pdf.write_bytes(b"%PDF-1.4\ntest")
        exported = _finalize_named_paper(
            paper_workspace,
            label="Agent Baseline.pdf",
            repetition=2,
        )
        assert exported == "paper/Agent-Baseline-run2.pdf"
        assert not canonical_pdf.exists()
        assert (paper_workspace / exported).read_bytes() == b"%PDF-1.4\ntest"
        assert _finalize_named_paper(
            paper_workspace,
            label="ignored",
            repetition=1,
        ) is None

        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(run_dir / "source")
        subprocess.run(
            [sys.executable, "-c", "import mathmodel.agent.build, mathmodel.experiment"],
            cwd=run_dir / "source", env=environment, check=True,
        )

        frozen_input = run_dir / "cases" / "case-a-run-1" / "inputs" / "problem.txt"
        original = benchmark / "case-a" / "problem" / "problem.txt"
        original.write_text("changed after submission", encoding="utf-8")
        assert frozen_input.read_text() == "problem case-a"

        status_args = parser.parse_args([
            "status", manifest["id"], "--output", str(output),
        ])
        assert status_args.func(status_args) == 0

        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockChatHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        mock_config = root / "mock-config.yaml"
        mock_config.write_text(
            "\n".join([
                "provider: openai_compatible",
                "model: mock-model",
                f"base_url: http://127.0.0.1:{server.server_port}/v1",
                "sandbox: local",
                "verification:",
                "  enabled: false",
            ]),
            encoding="utf-8",
        )
        old_key = os.environ.get("OPENAI_COMPATIBLE_API_KEY")
        os.environ["OPENAI_COMPATIBLE_API_KEY"] = "test-only-key"
        try:
            live_args = parser.parse_args([
                "submit", "--benchmark", str(benchmark), "--output", str(output),
                "--config", str(mock_config), "--label", "lifecycle-test",
                "--max-steps", "2",
            ])
            assert live_args.func(live_args) == 0
            live_runs = [
                path for path in output.iterdir()
                if "lifecycle-test" in path.name
            ]
            assert len(live_runs) == 1
            live_run = live_runs[0]
            deadline = time.time() + 20
            while time.time() < deadline:
                live_manifest = json.loads((live_run / "manifest.json").read_text())
                if live_manifest["status"] in {"completed", "completed_with_errors", "failed"}:
                    break
                time.sleep(0.1)
            assert live_manifest["status"] == "completed", live_manifest
            for case in live_manifest["cases"]:
                case_dir = live_run / "cases" / case["slug"]
                case_status = json.loads((case_dir / "status.json").read_text())
                assert case_status["status"] == "completed", case_status
                assert (case_dir / "workspace" / "events.jsonl").is_file()
                assert (case_dir / "workspace" / "final_summary.md").read_text() == "mock completed"
        finally:
            if old_key is None:
                os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)
            else:
                os.environ["OPENAI_COMPATIBLE_API_KEY"] = old_key
            server.shutdown()
            server.server_close()

    print("experiment runner checks: passed")


if __name__ == "__main__":
    main()
