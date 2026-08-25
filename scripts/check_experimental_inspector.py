"""Regression checks for the read-only Experimental Inspector backend."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.contextlog import ContextRecorder
from mathmodel.experimental_inspector import server


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    original_root = server.EXPERIMENTS_ROOT
    original_process_alive = server._process_alive
    original_process_group_alive = server._process_group_alive

    server._process_alive = lambda pid: False
    server._process_group_alive = lambda process_group_id: True
    try:
        process_manifest = {
            "status": "running",
            "supervisor_pid": 123456,
            "process_group_id": 123456,
        }
        assert server._effective_experiment_status(process_manifest) == "orphaned"
        server._process_group_alive = lambda process_group_id: False
        assert server._effective_experiment_status(process_manifest) == "killed"
        assert server._effective_case_status({"status": "running", "pid": 123457}, "killed") == "killed"
        assert server._effective_case_status({"status": "queued"}, "killed") == "killed"
        assert server._effective_case_status({"status": "completed"}, "killed") == "completed"
        assert server._effective_experiment_status({"status": "completed"}) == "completed"
    finally:
        server._process_alive = original_process_alive
        server._process_group_alive = original_process_group_alive

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "experiments"
        run = root / "run-001"
        case = run / "cases" / "case-a"
        workspace = case / "workspace"
        workspace.mkdir(parents=True)
        write_json(run / "manifest.json", {
            "id": "run-001",
            "label": "baseline",
            "status": "running",
            "submitted_at": "2026-08-02T12:00:00+08:00",
            "source_sha256": "abc123",
            "git": {"commit": "deadbeef", "dirty": True},
            "settings": {
                "provider": "mock",
                "model": "mock-v1",
                "context_profile": "split-256k",
                "compact_threshold_tokens": 256_000,
                "keep_tail_messages": 10,
                "compaction_strategy": "split_user_agent_v1",
            },
            "cases": [{"name": "Case A", "slug": "case-a", "status": "queued"}],
        })
        write_json(case / "status.json", {
            "name": "Case A", "status": "running", "started_at": "2026-08-02T12:00:01+08:00",
        })
        (case / "task.txt").write_text("Solve case A", encoding="utf-8")
        (case / "console.log").write_text("worker started\n", encoding="utf-8")
        (workspace / "events.jsonl").write_text(
            json.dumps({"kind": "task", "t": 0, "task": "Solve"}) + "\n"
            + json.dumps({"kind": "assistant", "t": 1, "step": 1, "text": "Working"}) + "\n"
            + json.dumps({
                "kind": "compact_done",
                "t": 2,
                "strategy": "split_user_agent_v1",
                "compaction_index": 1,
                "context_chars_before": 800_000,
                "context_chars_after": 80_000,
                "compression_ratio": 0.1,
                "summary_calls": 1,
                "summary_usage": {"total_tokens": 2_500},
            }) + "\n",
            encoding="utf-8",
        )
        write_json(workspace / "session_state.json", {
            "total_usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })
        (workspace / "plan.md").write_text("# Plan\n", encoding="utf-8")
        paper = workspace / "paper" / "main.pdf"
        paper.parent.mkdir()
        paper.write_bytes(b"%PDF-test")

        recorder = ContextRecorder(workspace / "context_requests.jsonl", "run-001/case-a")
        recorder("request", {
            "request_id": "req-1", "ts": 100.0, "provider": "MockProvider", "model": "mock-v1",
            "context": {"agent_role": "Main Agent", "phase": "agent_step", "step": 1},
            "params": {"messages": [{"role": "system", "content": "policy"}, {"role": "user", "content": "solve"}], "tools": []},
        })
        recorder("response", {
            "request_id": "req-1", "ts": 101.0, "status": "completed",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })
        recorder("request", {
            "request_id": "req-2", "ts": 102.0, "provider": "MockProvider", "model": "mock-v1",
            "context": {"agent_role": "Subagent 1", "agent_scope": "sub1_", "phase": "agent_step", "step": 1},
            "params": {"messages": [{"role": "system", "content": "sub policy"}, {"role": "user", "content": "research"}], "tools": []},
        })
        recorder("response", {
            "request_id": "req-2", "ts": 103.0, "status": "completed",
            "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        })

        server.EXPERIMENTS_ROOT = root.resolve()
        try:
            experiments = server.list_experiments()
            assert len(experiments) == 1
            assert experiments[0]["cases"][0]["status"] == "running"
            assert experiments[0]["cases"][0]["request_count"] == 2

            detail = server.get_experiment("run-001")
            assert detail["label"] == "baseline"
            assert detail["git"]["dirty"] is True
            assert detail["settings"]["context_profile"] == "split-256k"

            case_detail = server.get_case("run-001", "case-a")
            assert len(case_detail["events"]) == 3
            assert case_detail["events"][-1]["compression_ratio"] == 0.1
            assert case_detail["events_cursor"] > 0
            assert case_detail["usage"]["total_tokens"] == 12
            assert case_detail["artifacts"][0]["path"] in {"paper/main.pdf", "plan.md"}
            assert any(item["path"] == "paper/main.pdf" for item in case_detail["artifacts"])

            incremental = server.get_case(
                "run-001", "case-a", case_detail["events_cursor"],
            )
            assert incremental["events"] == []

            requests = server.list_context_requests("run-001", "case-a")
            assert requests[0]["request_id"] == "req-2"
            agent_contexts = server.list_agent_contexts("run-001", "case-a")
            assert [group["agent_role"] for group in agent_contexts] == ["Main Agent", "Subagent 1"]
            assert agent_contexts[0]["request_count"] == 1
            assert agent_contexts[0]["total_tokens"] == 12
            assert agent_contexts[1]["agent_scope"] == "sub1_"
            assert agent_contexts[1]["requests"][0]["request_id"] == "req-2"
            request = server.get_context_request("run-001", "case-a", "req-1")
            assert request["items"][0]["category"] == "system_prompt"

            assert server.resolve_artifact("run-001", "case-a", "paper/main.pdf") == paper.resolve()
            try:
                server.resolve_artifact("run-001", "case-a", "../../manifest.json")
            except ValueError:
                pass
            else:
                raise AssertionError("artifact path traversal was not rejected")
        finally:
            server.EXPERIMENTS_ROOT = original_root

    assert server.STATIC_INDEX.is_file(), "build the frontend before running this check"
    print("experimental inspector checks: passed")


if __name__ == "__main__":
    main()
