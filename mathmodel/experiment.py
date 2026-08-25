"""Detached, source-frozen benchmark experiments for the modeling agent.

This module deliberately does not import the dashboard server.  ``submit``
copies the runtime source and benchmark inputs into an experiment directory,
then starts a detached supervisor from that snapshot.  Later edits to the
working tree therefore cannot change an experiment that is already running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import PROJECT_ROOT, load_config


DEFAULT_BENCHMARK_ROOT = PROJECT_ROOT / "benchmark-v1"
DEFAULT_EXPERIMENT_ROOT = PROJECT_ROOT / "experiments"
DEFAULT_REPETITIONS = 2
CONTEXT_PROFILES: dict[str, dict[str, Any]] = {
    "control": {
        "compact_threshold_tokens": 1_000_000,
        "keep_tail_messages": 12,
        "compaction_strategy": "legacy_monolithic",
    },
    "monolithic-256k": {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": "legacy_monolithic",
    },
    "split-256k": {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": "split_user_agent_v1",
    },
    "incremental-summary-256k": {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": "incremental_summary_v1",
    },
    "externalized-results-256k": {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": "externalized_tool_results_v1",
        "tool_result_externalize_threshold_tokens": 1_000,
        "tool_result_preview_chars": 600,
    },
    "summary-preserve-thinking-256k": {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": "incremental_summary_preserve_thinking_v1",
        "tool_result_externalize_threshold_tokens": 1_000,
        "tool_result_preview_chars": 600,
    },
    # V2 ablation matrix. The existing `control` profile remains the full-history
    # baseline. These three groups isolate checkpoint summarization, deterministic
    # Tool Result pruning, and their combination without replacing older profiles.
    "checkpoint-summary-256k": {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": "checkpoint_summary_v2",
    },
    "policy-pruning-control": {
        "compact_threshold_tokens": 1_000_000,
        "keep_tail_messages": 12,
        "compaction_strategy": "policy_tool_pruning_v2",
        "tool_prune_threshold_tokens": 166_400,
        "tool_prune_aggressive_threshold_tokens": 204_800,
        "tool_prune_recent_results": 5,
    },
    "checkpoint-pruning-256k": {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": "checkpoint_tool_pruning_v2",
        "tool_prune_threshold_tokens": 166_400,
        "tool_prune_aggressive_threshold_tokens": 204_800,
        "tool_prune_recent_results": 5,
    },
}
SUPPORTED_INPUT_SUFFIXES = {
    ".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".md",
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif",
}
TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed"}
DEFAULT_TASK = """\
Run benchmark case {case_name} completely and unattended. Read problem.md and
solve every sub-problem end to end. Make and document reasonable assumptions
instead of asking the user questions. Maintain the plan and decision log, save
all computed evidence under results/, and create useful figures where
appropriate. Produce the complete competition-quality paper and compiled PDF.
If writable spreadsheet templates are present in the work directory root, fill
them while preserving their required sheets, columns, and ordering. Check the
final numerical results and deliverables before finishing.
"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _safe_name(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in value)
    return cleaned.strip("-") or "case"


def _input_files(problem_dir: Path) -> list[Path]:
    return sorted(
        p for p in problem_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_INPUT_SUFFIXES
        and p.name != "task.md"
    )


def discover_cases(benchmark_root: Path) -> list[dict[str, Any]]:
    """Return every case directory, including invalid/empty cases."""
    if not benchmark_root.is_dir():
        raise ValueError(f"benchmark directory does not exist: {benchmark_root}")
    cases: list[dict[str, Any]] = []
    for case_dir in sorted(p for p in benchmark_root.iterdir() if p.is_dir()):
        problem_dir = case_dir / "problem"
        inputs = _input_files(problem_dir) if problem_dir.is_dir() else []
        task_path = case_dir / "task.md"
        cases.append({
            "name": case_dir.name,
            "case_dir": case_dir,
            "problem_dir": problem_dir,
            "inputs": inputs,
            "task_path": task_path if task_path.is_file() else None,
            "valid": bool(inputs),
            "error": None if inputs else f"no supported input files in {problem_dir}",
        })
    if not cases:
        raise ValueError(f"no benchmark cases found in: {benchmark_root}")
    return cases


def _git_value(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _git_metadata() -> dict[str, Any]:
    status = _git_value("status", "--porcelain")
    return {
        "commit": _git_value("rev-parse", "HEAD"),
        "branch": _git_value("branch", "--show-current"),
        "dirty": bool(status),
    }


def _copy_runtime_snapshot(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)

    def ignore_mathmodel(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name == "__pycache__" or name.endswith(".pyc")}
        if Path(directory).name == "mathmodel":
            # The experiment runtime has no HTTP/UI dependency. Excluding these
            # directories keeps each snapshot around 1 MB instead of ~90 MB.
            ignored.update({"dashboard", "context_inspector"})
        return ignored

    shutil.copytree(
        PROJECT_ROOT / "mathmodel",
        destination / "mathmodel",
        ignore=ignore_mathmodel,
    )
    for directory_name in ("skills", "templates"):
        source = PROJECT_ROOT / directory_name
        if source.is_dir():
            shutil.copytree(source, destination / directory_name)
    for file_name in ("requirements.txt",):
        source = PROJECT_ROOT / file_name
        if source.is_file():
            shutil.copy2(source, destination / file_name)


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _new_run_id(label: str | None = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{stamp}-{_safe_name(label)}-{suffix}" if label else f"{stamp}-{suffix}"


def _snapshot_case(case: dict[str, Any], destination: Path) -> dict[str, Any]:
    inputs_dir = destination / "inputs"
    inputs_dir.mkdir(parents=True)
    copied: list[str] = []
    for source in case["inputs"]:
        relative = source.relative_to(case["problem_dir"])
        target = inputs_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(relative))
    task = (
        case["task_path"].read_text(encoding="utf-8", errors="replace").strip()
        if case["task_path"] else DEFAULT_TASK.format(case_name=case["name"])
    )
    (destination / "task.txt").write_text(task, encoding="utf-8")
    return {"name": case["name"], "inputs": copied, "task": task}


def submit(args: argparse.Namespace) -> int:
    benchmark_root = Path(args.benchmark).expanduser().resolve()
    experiment_root = Path(args.output).expanduser().resolve()
    cases = discover_cases(benchmark_root)
    invalid = [case for case in cases if not case["valid"]]
    if invalid:
        details = "\n".join(f"  - {case['name']}: {case['error']}" for case in invalid)
        raise ValueError(
            "benchmark preflight failed; every case must contain input files:\n" + details
        )
    slugs = [_safe_name(case["name"]) for case in cases]
    if len(set(slugs)) != len(slugs):
        raise ValueError("benchmark case names collide after filename normalization")
    if (
        args.max_steps < 1
        or args.sub_max_steps < 1
        or args.max_workers < 1
        or args.repetitions < 1
    ):
        raise ValueError(
            "max-steps, sub-max-steps, max-workers, and repetitions must all be positive"
        )

    # Besides resolving the exact configuration used by this submission,
    # load_config loads API keys from the repository .env into the inherited
    # environment. Secrets are never copied into the experiment directory.
    config_source = (
        Path(args.config).expanduser().resolve() if args.config else None
    )
    config = load_config(config_source)
    config.setdefault("verification", {})["enabled"] = bool(args.with_verification)
    context_config = config.setdefault("context", {})
    if args.context_profile:
        context_config.update(CONTEXT_PROFILES[args.context_profile])
    if args.compact_threshold_tokens is not None:
        context_config["compact_threshold_tokens"] = args.compact_threshold_tokens
    if args.keep_tail_messages is not None:
        context_config["keep_tail_messages"] = args.keep_tail_messages
    if args.compaction_strategy:
        context_config["compaction_strategy"] = args.compaction_strategy
    if args.tool_result_externalize_threshold_tokens is not None:
        context_config["tool_result_externalize_threshold_tokens"] = (
            args.tool_result_externalize_threshold_tokens
        )
    if args.tool_result_preview_chars is not None:
        context_config["tool_result_preview_chars"] = args.tool_result_preview_chars
    if args.tool_prune_threshold_tokens is not None:
        context_config["tool_prune_threshold_tokens"] = (
            args.tool_prune_threshold_tokens
        )
    if args.tool_prune_aggressive_threshold_tokens is not None:
        context_config["tool_prune_aggressive_threshold_tokens"] = (
            args.tool_prune_aggressive_threshold_tokens
        )
    if args.tool_prune_recent_results is not None:
        context_config["tool_prune_recent_results"] = args.tool_prune_recent_results
    if (
        int(context_config.get("compact_threshold_tokens", 0)) < 1
        or int(context_config.get("keep_tail_messages", 0)) < 1
        or int(context_config.get(
            "tool_result_externalize_threshold_tokens", 0
        )) < 1
        or int(context_config.get("tool_result_preview_chars", 0)) < 1
        or int(context_config.get("tool_prune_threshold_tokens", 0)) < 1
        or int(context_config.get(
            "tool_prune_aggressive_threshold_tokens", 0
        )) < int(context_config.get("tool_prune_threshold_tokens", 0))
        or int(context_config.get("tool_prune_recent_results", -1)) < 0
    ):
        raise ValueError(
            "context thresholds/tail/preview must be valid and the aggressive "
            "prune threshold must be >= the initial prune threshold"
        )
    if args.working_memory_mode:
        context_config["working_memory_mode"] = (
            args.working_memory_mode
        )
    if args.sandbox:
        config["sandbox"] = args.sandbox
    if config.get("sandbox") == "local":
        config["sandbox_python"] = sys.executable

    run_id = _new_run_id(args.label)
    run_dir = experiment_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    source_dir = run_dir / "source"
    _copy_runtime_snapshot(source_dir)
    config_path = run_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_json_safe(config), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    case_records: list[dict[str, Any]] = []
    # Each repetition gets a separate directory, workspace, process and log.
    # Case-first ordering lets the default two-worker pool start both runs of
    # one benchmark case concurrently instead of queueing its second run.
    for case in cases:
        for repetition in range(1, args.repetitions + 1):
            benchmark_slug = _safe_name(case["name"])
            case_slug = f"{benchmark_slug}-run-{repetition}"
            case_dir = run_dir / "cases" / case_slug
            record = _snapshot_case(case, case_dir)
            record.update({
                "slug": case_slug,
                "benchmark_case": case["name"],
                "repetition": repetition,
                "repetitions": args.repetitions,
                "status": "queued",
                "directory": str(Path("cases") / case_slug),
            })
            _atomic_json(case_dir / "status.json", {
                "name": case["name"],
                "repetition": repetition,
                "repetitions": args.repetitions,
                "status": "queued",
                "submitted_at": _now_iso(),
            })
            case_records.append(record)

    manifest = {
        "schema_version": 1,
        "id": run_id,
        "label": args.label,
        "status": "prepared" if args.dry_run else "queued",
        "submitted_at": _now_iso(),
        "repository": str(PROJECT_ROOT),
        "benchmark_source": str(benchmark_root),
        "config_source": str(config_source or (PROJECT_ROOT / "config.yaml")),
        "git": _git_metadata(),
        "source_sha256": _tree_sha256(source_dir),
        "python": sys.executable,
        "settings": {
            "max_steps": args.max_steps,
            "sub_max_steps": args.sub_max_steps,
            "max_workers": min(args.max_workers, len(case_records)),
            "repetitions": args.repetitions,
            "scheduling": "paired_repetitions",
            "verification_enabled": bool(args.with_verification),
            "sandbox": config.get("sandbox"),
            "provider": config.get("provider"),
            "model": config.get("model"),
            "reasoning_effort": config.get("reasoning_effort"),
            "working_memory_mode": config.get("context", {}).get(
                "working_memory_mode",
                "replace",
            ),
            "context_profile": args.context_profile,
            "compact_threshold_tokens": context_config.get(
                "compact_threshold_tokens"
            ),
            "keep_tail_messages": context_config.get("keep_tail_messages", 12),
            "compaction_strategy": context_config.get(
                "compaction_strategy",
                "legacy_monolithic",
            ),
            "tool_result_externalize_threshold_tokens": context_config.get(
                "tool_result_externalize_threshold_tokens",
                1_000,
            ),
            "tool_result_preview_chars": context_config.get(
                "tool_result_preview_chars",
                600,
            ),
            "tool_prune_threshold_tokens": context_config.get(
                "tool_prune_threshold_tokens",
            ),
            "tool_prune_aggressive_threshold_tokens": context_config.get(
                "tool_prune_aggressive_threshold_tokens",
            ),
            "tool_prune_recent_results": context_config.get(
                "tool_prune_recent_results",
                5,
            ),
        },
        "cases": [
            {key: value for key, value in record.items() if key != "task"}
            for record in case_records
        ],
    }
    _atomic_json(run_dir / "manifest.json", manifest)
    if args.dry_run:
        print(f"Prepared (not started): {run_id}")
        print(run_dir)
        return 0

    command = [
        sys.executable, "-m", "mathmodel.experiment", "_supervise",
        "--run-dir", str(run_dir),
    ]
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_dir) if not prior_pythonpath
        else os.pathsep.join((str(source_dir), prior_pythonpath))
    )
    environment["PATH"] = environment.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:/Library/TeX/texbin"
    supervisor_log = (run_dir / "supervisor.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=source_dir,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=supervisor_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        supervisor_log.close()
    # The detached child can reach "running" before Popen returns. Merge the
    # PID into its latest manifest rather than reverting that status to queued.
    latest_manifest = _read_json(run_dir / "manifest.json") or manifest
    latest_manifest["supervisor_pid"] = process.pid
    latest_manifest["process_group_id"] = process.pid
    _atomic_json(run_dir / "manifest.json", latest_manifest)
    print(f"Submitted: {run_id}")
    print(f"PID: {process.pid}")
    print(f"Results: {run_dir}")
    print(f"Status: {sys.executable} -m mathmodel.experiment status {run_id}")
    return 0


def _event_printer(kind: str, data: dict[str, Any]) -> None:
    if kind == "assistant":
        calls = data.get("tool_calls") or []
        label = f"[step {data.get('step', '?')}] tokens={data.get('total_tokens', 0)}"
        if calls:
            names = [item[0] if isinstance(item, (list, tuple)) else str(item) for item in calls]
            print(f"{label} -> {', '.join(names)}", flush=True)
        elif data.get("text"):
            print(f"{label} {str(data['text'])[:240]}", flush=True)
    elif kind == "tool_result":
        observation = str(data.get("observation", "")).replace("\n", " ")
        print(f"    <{data.get('name', 'tool')}> {observation[:300]}", flush=True)
    elif kind in {
        "compact_start", "verification_start", "verification_result", "done",
        "max_steps", "cancelled",
    }:
        print(f"[{kind}] {json.dumps(data, ensure_ascii=False, default=str)[:500]}", flush=True)


def _artifact_inventory(workdir: Path) -> list[dict[str, Any]]:
    ignored = {"events.jsonl", "context_requests.jsonl", "context_requests.index.jsonl"}
    inventory: list[dict[str, Any]] = []
    for path in sorted(p for p in workdir.rglob("*") if p.is_file()):
        relative = str(path.relative_to(workdir))
        if relative in ignored or relative.startswith("_inputs/"):
            continue
        inventory.append({"path": relative, "bytes": path.stat().st_size})
    return inventory


def _finalize_named_paper(
    workdir: Path,
    *,
    label: str | None,
    repetition: int,
) -> str | None:
    """Rename the completed canonical PDF to ``<label>-runN.pdf``.

    The modeling tools keep using paper/main.pdf while the Agent is active. Only
    the detached experiment worker calls this after Agent.run() has returned, so
    the stable internal write/compile contract is not disturbed.
    """
    source = workdir / "paper" / "main.pdf"
    if not source.is_file():
        return None
    raw_label = str(label or "experiment").strip()
    if raw_label.lower().endswith(".pdf"):
        raw_label = raw_label[:-4]
    stem = _safe_name(raw_label)
    target = source.with_name(f"{stem}-run{max(1, int(repetition))}.pdf")
    if target == source:
        return str(target.relative_to(workdir))
    os.replace(source, target)
    return str(target.relative_to(workdir))


def _prepare_workspace(case_dir: Path) -> list[Path]:
    input_dir = case_dir / "inputs"
    workdir = case_dir / "workspace"
    copied_inputs = workdir / "_inputs"
    shutil.copytree(input_dir, copied_inputs)
    inputs = sorted(p for p in copied_inputs.rglob("*") if p.is_file())
    # Excel files frequently double as required answer templates. Ingestion
    # creates normalized CSV views; this extra writable copy lets the agent
    # fill the original workbook format as well.
    for source in inputs:
        if source.suffix.lower() in {".xlsx", ".xls"}:
            target = workdir / source.name
            if not target.exists():
                shutil.copy2(source, target)
    return inputs


def run_worker(run_dir: Path, case_slug: str) -> int:
    from .agent.build import build_agent
    from .contextlog import CONTEXT_LOG_FILENAME, ContextRecorder
    from .ingest.ingest import ingest
    from .runlog import JsonlLogger, compose

    manifest = _read_json(run_dir / "manifest.json")
    case_manifest = next(
        (case for case in manifest.get("cases", []) if case.get("slug") == case_slug),
        None,
    )
    if not case_manifest:
        raise ValueError(f"unknown case: {case_slug}")
    case_dir = run_dir / "cases" / case_slug
    status_path = case_dir / "status.json"
    started = time.time()
    status = {
        "name": case_manifest["name"], "status": "running",
        "repetition": case_manifest.get("repetition", 1),
        "repetitions": case_manifest.get("repetitions", 1),
        "pid": os.getpid(), "started_at": _now_iso(),
    }
    _atomic_json(status_path, status)
    try:
        inputs = _prepare_workspace(case_dir)
        workdir = case_dir / "workspace"
        ingest(inputs, workdir)
        config = load_config(run_dir / "config.yaml")
        config["_context_request_observer"] = ContextRecorder(
            workdir / CONTEXT_LOG_FILENAME,
            f"{manifest.get('id', run_dir.name)}/{case_slug}",
        )
        logger = compose(JsonlLogger(workdir / "events.jsonl"), _event_printer)
        settings = manifest.get("settings", {})
        agent = build_agent(
            config,
            workdir,
            max_steps=int(settings.get("max_steps", 200)),
            sub_max_steps=int(settings.get("sub_max_steps", 60)),
            on_event=logger,
            resume=False,
            verification_enabled=bool(settings.get("verification_enabled", False)),
        )
        task_path = case_dir / "task.txt"
        task = task_path.read_text(encoding="utf-8")
        summary = agent.run(
            task,
            verify_on_completion=bool(settings.get("verification_enabled", False)),
        )
        (workdir / "final_summary.md").write_text(summary or "", encoding="utf-8")
        paper_pdf = _finalize_named_paper(
            workdir,
            label=manifest.get("label"),
            repetition=int(case_manifest.get("repetition", 1)),
        )
        artifacts = _artifact_inventory(workdir)
        _atomic_json(case_dir / "artifacts.json", {"artifacts": artifacts})
        status.update({
            "status": "completed" if agent.last_stop_reason == "done" else "stopped",
            "finished_at": _now_iso(),
            "duration_seconds": round(time.time() - started, 2),
            "stop_reason": agent.last_stop_reason,
            "usage": agent.total_usage.to_dict(),
            "artifact_count": len(artifacts),
            "paper_pdf": paper_pdf,
        })
        _atomic_json(status_path, status)
        return 0 if agent.last_stop_reason == "done" else 2
    except BaseException as exc:
        error_text = traceback.format_exc()
        (case_dir / "error.log").write_text(error_text, encoding="utf-8")
        status.update({
            "status": "failed",
            "finished_at": _now_iso(),
            "duration_seconds": round(time.time() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
        })
        _atomic_json(status_path, status)
        print(error_text, file=sys.stderr, flush=True)
        return 1


def _supervise(run_dir: Path) -> int:
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["status"] = "running"
    manifest["started_at"] = _now_iso()
    _atomic_json(manifest_path, manifest)
    settings = manifest.get("settings", {})
    max_workers = max(1, int(settings.get("max_workers", 1)))
    source_dir = run_dir / "source"

    def launch(case: dict[str, Any]) -> tuple[str, int]:
        slug = case["slug"]
        case_dir = run_dir / "cases" / slug
        log_path = case_dir / "console.log"
        with log_path.open("a", encoding="utf-8") as log:
            result = subprocess.run(
                [sys.executable, "-m", "mathmodel.experiment", "_worker",
                 "--run-dir", str(run_dir), "--case", slug],
                cwd=source_dir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        return slug, result.returncode

    results: dict[str, int] = {}
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(launch, dict(case)): case for case in manifest["cases"]}
            for future in as_completed(futures):
                case = futures[future]
                try:
                    slug, returncode = future.result()
                except BaseException:
                    slug, returncode = case["slug"], 1
                    with (run_dir / "supervisor.log").open("a", encoding="utf-8") as handle:
                        handle.write(traceback.format_exc())
                results[slug] = returncode
    except BaseException:
        manifest = _read_json(manifest_path)
        manifest.update({"status": "failed", "finished_at": _now_iso()})
        _atomic_json(manifest_path, manifest)
        raise

    case_statuses = [
        _read_json(run_dir / "cases" / case["slug"] / "status.json")
        for case in manifest.get("cases", [])
    ]
    failures = sum(status.get("status") != "completed" for status in case_statuses)
    manifest = _read_json(manifest_path)
    manifest.update({
        "status": "completed" if failures == 0 else "completed_with_errors",
        "finished_at": _now_iso(),
        "case_returncodes": results,
        "completed_cases": len(case_statuses) - failures,
        "failed_cases": failures,
    })
    _atomic_json(manifest_path, manifest)
    return 0 if failures == 0 else 1


def _experiment_dir(value: str, root: Path) -> Path:
    direct = Path(value).expanduser()
    if direct.is_dir():
        return direct.resolve()
    candidate = root / value
    if candidate.is_dir():
        return candidate.resolve()
    raise ValueError(f"experiment not found: {value}")


def _render_status(run_dir: Path) -> str:
    manifest = _read_json(run_dir / "manifest.json")
    lines = [
        f"Experiment: {manifest.get('id', run_dir.name)}",
        f"Status: {manifest.get('status', 'unknown')}",
        f"Source: {str(manifest.get('source_sha256', ''))[:12]}  Git: {manifest.get('git', {}).get('commit', '-')}",
    ]
    for case in manifest.get("cases", []):
        status = _read_json(run_dir / "cases" / case["slug"] / "status.json")
        duration = status.get("duration_seconds")
        suffix = f" ({duration / 60:.1f} min)" if isinstance(duration, (int, float)) else ""
        detail = f" — {status['error']}" if status.get("error") else ""
        repetition = case.get("repetition")
        repetitions = case.get("repetitions")
        run_label = (
            f" [run {repetition}/{repetitions}]"
            if isinstance(repetition, int) and isinstance(repetitions, int)
            else ""
        )
        lines.append(
            f"  {case['name']}{run_label}: "
            f"{status.get('status', 'unknown')}{suffix}{detail}"
        )
    lines.append(f"Directory: {run_dir}")
    return "\n".join(lines)


def show_status(args: argparse.Namespace) -> int:
    root = Path(args.output).expanduser().resolve()
    print(_render_status(_experiment_dir(args.experiment, root)))
    return 0


def list_experiments(args: argparse.Namespace) -> int:
    root = Path(args.output).expanduser().resolve()
    if not root.is_dir():
        print("No experiments yet.")
        return 0
    runs = sorted((p for p in root.iterdir() if (p / "manifest.json").is_file()), reverse=True)
    if not runs:
        print("No experiments yet.")
        return 0
    for run_dir in runs:
        manifest = _read_json(run_dir / "manifest.json")
        label = f" [{manifest['label']}]" if manifest.get("label") else ""
        print(f"{manifest.get('id', run_dir.name)}  {manifest.get('status', 'unknown')}{label}")
    return 0


def list_cases(args: argparse.Namespace) -> int:
    cases = discover_cases(Path(args.benchmark).expanduser().resolve())
    for case in cases:
        state = "ready" if case["valid"] else "INVALID"
        print(f"{case['name']}: {state} ({len(case['inputs'])} input files)")
        if case["error"]:
            print(f"  {case['error']}")
    return 0 if all(case["valid"] for case in cases) else 1


def show_logs(args: argparse.Namespace) -> int:
    root = Path(args.output).expanduser().resolve()
    run_dir = _experiment_dir(args.experiment, root)
    if args.case:
        slug = _safe_name(args.case)
        path = run_dir / "cases" / slug / "console.log"
        if not path.exists():
            # Accept the original case name, not only its slug.
            manifest = _read_json(run_dir / "manifest.json")
            match = next((c for c in manifest.get("cases", []) if c.get("name") == args.case), None)
            if match:
                path = run_dir / "cases" / match["slug"] / "console.log"
    else:
        path = run_dir / "supervisor.log"
    if not args.follow:
        if not path.is_file():
            raise ValueError(f"log does not exist yet: {path}")
        sys.stdout.write(path.read_text(encoding="utf-8", errors="replace"))
        return 0

    position = 0
    while True:
        if path.is_file():
            with path.open(encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                chunk = handle.read()
                position = handle.tell()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
        status = _read_json(run_dir / "manifest.json").get("status")
        if status in TERMINAL_STATUSES:
            return 0
        time.sleep(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mathmodel.experiment",
        description="Run source-frozen benchmark experiments without the dashboard.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit_parser = subparsers.add_parser("submit", help="snapshot and start an experiment")
    submit_parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK_ROOT))
    submit_parser.add_argument("--output", default=str(DEFAULT_EXPERIMENT_ROOT))
    submit_parser.add_argument("--config", help="configuration file to freeze for this run")
    submit_parser.add_argument("--label")
    submit_parser.add_argument("--max-steps", type=int, default=200)
    submit_parser.add_argument("--sub-max-steps", type=int, default=80)
    submit_parser.add_argument("--max-workers", type=int, default=4)
    submit_parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_REPETITIONS,
        help="independent runs per benchmark case (default: 2)",
    )
    submit_parser.add_argument("--sandbox", choices=("docker", "local"))
    submit_parser.add_argument(
        "--working-memory-mode",
        choices=("append_only", "replace"),
        help="override Working Memory mode for this frozen experiment",
    )
    submit_parser.add_argument(
        "--context-profile",
        choices=tuple(CONTEXT_PROFILES),
        help=(
            "preset a comparable context experiment: control, "
            "monolithic-256k, split-256k, incremental-summary-256k, "
            "externalized-results-256k, summary-preserve-thinking-256k, "
            "checkpoint-summary-256k, policy-pruning-control, or "
            "checkpoint-pruning-256k"
        ),
    )
    submit_parser.add_argument(
        "--compact-threshold-tokens",
        type=int,
        help="override the current-context token threshold for compaction",
    )
    submit_parser.add_argument(
        "--keep-tail-messages",
        type=int,
        help="minimum recent raw messages retained during compaction",
    )
    submit_parser.add_argument(
        "--compaction-strategy",
        choices=(
            "legacy_monolithic",
            "split_user_agent_v1",
            "incremental_summary_v1",
            "externalized_tool_results_v1",
            "incremental_summary_preserve_thinking_v1",
            "checkpoint_summary_v2",
            "policy_tool_pruning_v2",
            "checkpoint_tool_pruning_v2",
        ),
        help="override the compaction algorithm without changing the baseline",
    )
    submit_parser.add_argument(
        "--tool-result-externalize-threshold-tokens",
        type=int,
        help="externalize old Tool Results at or above this estimated token size",
    )
    submit_parser.add_argument(
        "--tool-result-preview-chars",
        type=int,
        help="local preview characters retained for an externalized Tool Result",
    )
    submit_parser.add_argument(
        "--tool-prune-threshold-tokens",
        type=int,
        help="start moderate tool-specific pruning at this current-context size",
    )
    submit_parser.add_argument(
        "--tool-prune-aggressive-threshold-tokens",
        type=int,
        help="switch to aggressive tool-specific pruning at this context size",
    )
    submit_parser.add_argument(
        "--tool-prune-recent-results",
        type=int,
        help="number of newest Tool Results that always remain raw",
    )
    submit_parser.add_argument(
        "--with-verification", action="store_true",
        help="run the built-in verifier (off by default for external benchmark scoring)",
    )
    submit_parser.add_argument("--dry-run", action="store_true", help="create snapshot but do not start")
    submit_parser.set_defaults(func=submit)

    case_parser = subparsers.add_parser("cases", help="validate and list benchmark cases")
    case_parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK_ROOT))
    case_parser.set_defaults(func=list_cases)

    list_parser = subparsers.add_parser("list", help="list submitted experiments")
    list_parser.add_argument("--output", default=str(DEFAULT_EXPERIMENT_ROOT))
    list_parser.set_defaults(func=list_experiments)

    status_parser = subparsers.add_parser("status", help="show one experiment")
    status_parser.add_argument("experiment")
    status_parser.add_argument("--output", default=str(DEFAULT_EXPERIMENT_ROOT))
    status_parser.set_defaults(func=show_status)

    logs_parser = subparsers.add_parser("logs", help="print or follow experiment logs")
    logs_parser.add_argument("experiment")
    logs_parser.add_argument("--case")
    logs_parser.add_argument("--follow", action="store_true")
    logs_parser.add_argument("--output", default=str(DEFAULT_EXPERIMENT_ROOT))
    logs_parser.set_defaults(func=show_logs)

    supervisor = subparsers.add_parser("_supervise", help="internal detached supervisor")
    supervisor.add_argument("--run-dir", required=True)
    supervisor.set_defaults(func=lambda ns: _supervise(Path(ns.run_dir).resolve()))

    worker = subparsers.add_parser("_worker", help="internal case worker")
    worker.add_argument("--run-dir", required=True)
    worker.add_argument("--case", required=True)
    worker.set_defaults(func=lambda ns: run_worker(Path(ns.run_dir).resolve(), ns.case))
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
