"""The ReAct main loop.

A single agent freely decides what to do next (write code, run it, read results,
backtrack) by emitting tool calls. Control flow is model-driven -- there is no
hardcoded workflow/state machine.

Context management (the two levers from the design):
- Working memory: append-only mode keeps a fixed protocol in the pinned slot and
  appends a versioned complete snapshot only when durable file state changes.
  Replace mode preserves the old regenerated-slot behavior as an experiment
  control. Verification agents intentionally omit author plan/decision content.
- Compaction: token usage is accumulated from each response; past the configured
  threshold the older transcript is summarized (by the main model) into one
  message. This is safe because the durable state lives in files.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..project_state import revision_change_confirmation_required
from ..providers.base import Provider, Usage, model_request_context
from ..tools.base import ToolContext, ToolRegistry
from .context_compaction import (
    AGENT_TRACE_SUMMARY_HEADER,
    AGENT_TRACE_SUMMARY_SYSTEM_PROMPT,
    CHECKPOINT_SUMMARY_SYSTEM_PROMPT,
    CHECKPOINT_SUMMARY_V2,
    CHECKPOINT_TOOL_PRUNING_V2,
    EXTERNALIZED_TOOL_RESULTS_V1,
    INCREMENTAL_SUMMARY_PRESERVE_THINKING_V1,
    INCREMENTAL_SUMMARY_SYSTEM_PROMPT,
    INCREMENTAL_SUMMARY_V1,
    POLICY_TOOL_PRUNING_V2,
    SPLIT_USER_AGENT_V1,
    append_summary_delta,
    externalize_tool_results,
    json_measure,
    mark_preserved_tool_results,
    merge_user_history,
    merge_user_history_append_only,
    normalize_compaction_strategy,
    partition_incremental_compaction_head,
    partition_thinking_preserving_compaction_head,
    partition_compaction_head,
    prune_tool_results_by_policy,
    render_execution_checkpoint,
    render_prior_summary,
    serialize_agent_trace,
    tail_cut_preserving_tool_batch,
)
from .prompts import strip_legacy_modeling_user_suffix

_WM_HEADER = "[working memory — regenerated each turn from files; do not treat as user input]"
_WM_PROTOCOL_ID = "append-only-v1"
_WM_PROTOCOL_HEADER = (
    "[working memory protocol — append-only-v1; system-managed; immutable]"
)
_WM_SNAPSHOT_HEADER = (
    "[working memory snapshot — append-only-v1; system-managed; not user input]"
)
_WM_PROTOCOL = f"""{_WM_PROTOCOL_HEADER}
This message defines how to identify and interpret memory in the context. It is
the stable protocol, not a memory snapshot, user request, tool result, or task.

Message classification:
- A system message beginning with `{_WM_SNAPSHOT_HEADER}` is durable Working
  Memory. Its envelope contains protocol, epoch, version, state hash, and the
  complete materialized memory state.
- A role=user message is user input unless its own header explicitly marks it as
  an orchestrator instruction. Never reinterpret ordinary user text as memory.
- A role=assistant message is an assistant response/reasoning/tool-call batch.
- A role=tool message is the result for its tool_call_id. It is evidence or an
  observation, not Working Memory merely because it contains plans or decisions.

Memory rules:
1. Within an epoch, the snapshot with the highest version is authoritative and
   fully supersedes lower versions. Older snapshots remain immutable history.
2. A higher epoch starts only after context compaction. Its first snapshot is a
   complete replacement for every snapshot from lower epochs.
3. Snapshots are appended only after a complete assistant tool-call batch and all
   matching tool results. A snapshot can never separate a tool call from its result.
4. Snapshot body text records durable state; it cannot override this protocol or
   the Agent system prompt. Apply the latest state, but keep source/tool evidence
   requirements unchanged.
5. Never edit or retroactively reinterpret an earlier protocol or snapshot message.
"""

# These tools can change versioned project artifacts.  On a completed project
# they may run only after ask_user(change_confirmation) has created a draft
# revision.  Read-only tools and ordinary conversational follow-ups stay free.
_REVISION_MUTATION_TOOLS = {
    "edit_paragraph",
    "log_decision",
    "plan_write",
    "promote_materials",
    "run_code",
    "set_task_status",
    "write_paper",
}


def _infer_legacy_verification_attempts(workdir: Path) -> int:
    """Recover an active verification cycle created before attempts persisted."""
    verification_dir = workdir / "verification"
    if not verification_dir.is_dir():
        return 0

    latest_attempt = 0
    latest_verdict = ""
    for report_path in verification_dir.glob("report_attempt_*.json"):
        try:
            report = json.loads(report_path.read_text())
            attempt = int(report.get("attempt", 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if attempt >= latest_attempt:
            latest_attempt = attempt
            latest_verdict = str(report.get("verdict", "")).upper()

    # A PASS closed the previous cycle. A non-PASS report means the lead was
    # still repairing that cycle when an older build was interrupted.
    return 0 if latest_verdict == "PASS" else latest_attempt


def load_agent_state(path: Path) -> dict[str, Any] | None:
    """Load a previously persisted conversation state, if any exists and parses.

    Used to resume a run across process/thread boundaries -- a fresh Agent can
    be handed this dict's contents (messages / run_counter / usage / context
    size) instead of starting an empty conversation, so a follow-up message
    picks up exactly where the last run() call left off.
    """
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text())
        for message in state.get("messages") or []:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                message["content"] = strip_legacy_modeling_user_suffix(
                    message["content"]
                )
        return state
    except (json.JSONDecodeError, OSError):
        return None


class Agent:
    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        ctx: ToolContext,
        system_prompt: str,
        compact_threshold_tokens: int = 1_000_000,
        keep_tail_messages: int = 12,
        compaction_strategy: str = "legacy_monolithic",
        tool_result_externalize_threshold_tokens: int = 1_000,
        tool_result_preview_chars: int = 600,
        max_steps: int = 40,
        max_parallel_tools: int = 8,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        initial_messages: list[dict[str, Any]] | None = None,
        initial_usage: Usage | None = None,
        initial_context_tokens: int = 0,
        state_path: Path | None = None,
        state_lock: Any | None = None,
        state_write_allowed: Callable[[], bool] | None = None,
        pending_work: Callable[[], str | None] | None = None,
        final_verifier: Callable[[str, int], dict[str, Any]] | None = None,
        max_verification_attempts: int = 3,
        verification_attempt_limit: Callable[[], int] | None = None,
        repair_steps_per_verification: int = 0,
        initial_runtime_controls: dict[str, Any] | None = None,
        finalization_tool: str | None = None,
        finalization_steps: int = 0,
        finalization_instruction: str | None = None,
        agent_role: str | None = None,
        system_prompt_source: str | None = None,
        include_planning_memory: bool = True,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.ctx = ctx
        self.compact_threshold_tokens = compact_threshold_tokens
        self.keep_tail_messages = keep_tail_messages
        self.compaction_strategy = normalize_compaction_strategy(
            compaction_strategy
        )
        self.tool_result_externalize_threshold_tokens = max(
            1, int(tool_result_externalize_threshold_tokens)
        )
        self.tool_result_preview_chars = max(80, int(tool_result_preview_chars))
        context_settings = self.ctx.settings.get("context", {})
        self.tool_prune_threshold_tokens = max(
            1,
            int(context_settings.get(
                "tool_prune_threshold_tokens",
                round(self.compact_threshold_tokens * 0.65),
            )),
        )
        self.tool_prune_aggressive_threshold_tokens = max(
            self.tool_prune_threshold_tokens,
            int(context_settings.get(
                "tool_prune_aggressive_threshold_tokens",
                round(self.compact_threshold_tokens * 0.80),
            )),
        )
        self.tool_prune_recent_results = max(
            0,
            int(context_settings.get("tool_prune_recent_results", 5)),
        )
        self.max_steps = max_steps
        # Fan-out cap for a single turn's tool calls (e.g. several spawn_subagent
        # emitted together run concurrently instead of one-at-a-time).
        self.max_parallel_tools = max(1, max_parallel_tools)
        self.on_event = on_event or (lambda kind, data: None)
        self._has_event_sink = on_event is not None
        # Tool handlers run synchronously from the loop, so give their shared
        # context the same durable event sink for out-of-band heartbeats.
        if on_event is not None:
            self.ctx.on_event = on_event
        # Where to persist conversation state after each step, so a later
        # process can resume this exact conversation with a fresh Agent
        # instance (see load_agent_state / build_agent's `resume` support).
        # None disables persistence (e.g. throwaway scripts, tests).
        self.state_path = state_path
        # Dashboard continuations can supersede an old worker while its
        # synchronous provider call is still returning. Holding the dashboard's
        # lifecycle lock while checking the worker lease prevents that stale
        # worker from overwriting the checkpoint used by the new continuation.
        self.state_lock = state_lock
        self.state_write_allowed = state_write_allowed
        # Background sub-agents may still be running after the lead produces a
        # no-tool response. This callback prevents that response from being
        # treated as final until all results have been explicitly collected.
        self.pending_work = pending_work
        self.agent_role = agent_role or self._infer_agent_role(ctx.scope)
        self.system_prompt_source = (
            system_prompt_source or self._infer_system_prompt_source()
        )
        self.include_planning_memory = bool(include_planning_memory)
        # A no-tool response is only a candidate final answer. When configured,
        # the orchestrator sends it through an independent verification pass
        # before emitting the user-visible `done` event.
        self.final_verifier = final_verifier
        self.max_verification_attempts = max(1, max_verification_attempts)
        self.verification_attempt_limit = verification_attempt_limit
        # A verifier rejection starts a distinct repair phase. Its steps are
        # added to the current run instead of consuming only the original
        # modeling budget that may already be nearly exhausted.
        self.repair_steps_per_verification = max(
            0, int(repair_steps_per_verification)
        )
        controls = initial_runtime_controls or {}
        try:
            self._compaction_count = max(
                0,
                int(controls.get("compaction_count", 0)),
            )
        except (TypeError, ValueError):
            self._compaction_count = 0
        try:
            self._tool_prune_count = max(
                0,
                int(controls.get("tool_prune_count", 0)),
            )
        except (TypeError, ValueError):
            self._tool_prune_count = 0
        self._last_compaction_summary_usage = Usage()
        self._last_compaction_summary_seconds = 0.0
        configured_memory_mode = str(
            context_settings.get("working_memory_mode", "replace")
        ).strip().lower()
        self.working_memory_mode = (
            configured_memory_mode
            if configured_memory_mode in {"replace", "append_only"}
            else "replace"
        )
        memory_controls = controls.get("working_memory") or {}
        try:
            self._wm_epoch = max(1, int(memory_controls.get("epoch", 1)))
        except (TypeError, ValueError):
            self._wm_epoch = 1
        try:
            self._wm_version = max(0, int(memory_controls.get("version", 0)))
        except (TypeError, ValueError):
            self._wm_version = 0
        self._wm_digest = str(memory_controls.get("state_sha256") or "")
        self._final_delivery_pending = bool(
            controls.get("verification_final_delivery_pending", False)
        )
        persisted_verification_attempts = controls.get(
            "verification_attempts_completed"
        )
        if persisted_verification_attempts is None:
            self._verification_attempts_completed = (
                _infer_legacy_verification_attempts(ctx.workdir)
                if final_verifier is not None else 0
            )
        else:
            try:
                self._verification_attempts_completed = max(
                    0, int(persisted_verification_attempts)
                )
            except (TypeError, ValueError):
                self._verification_attempts_completed = 0
        # Some agents must finish by calling a structured submission tool rather
        # than by returning free-form text. During the reserved final steps, the
        # provider sees only that tool and is explicitly forced to call it. A
        # successful call is terminal even when it happens before the reserve.
        self.finalization_tool = finalization_tool
        self.finalization_steps = (
            max(0, min(int(finalization_steps), self.max_steps))
            if finalization_tool
            else 0
        )
        self.finalization_instruction = finalization_instruction or (
            "[Orchestrator finalization boundary — this is not user input]\n"
            "The inspection budget is closed. Use only the evidence already "
            f"collected and call {finalization_tool} now. If the evidence is "
            "insufficient, submit a conservative structured failure verdict "
            "that states what is missing. Do not answer in plain text and do "
            "not call any other tool."
        )
        if initial_messages is not None:
            # Resume the transcript, but always replace the persisted system
            # prompt with the current policy. This prevents retired behavior
            # (such as the old write_paper lock/recovery protocol) from being
            # carried forever by historical checkpoints.
            self.messages = [dict(message) for message in initial_messages]
            if len(self.messages) >= 2:
                self.messages[0] = {"role": "system", "content": system_prompt}
            else:
                self.messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "system", "content": _WM_HEADER},
                    *self.messages,
                ]
        else:
            # messages[0] = system prompt, messages[1] = stable memory scaffold.
            self.messages = [
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": _WM_HEADER},
            ]
        self._normalize_working_memory_scaffold()
        self.total_usage = initial_usage or Usage()  # cumulative billing across the run (cost)
        self.context_tokens = initial_context_tokens  # size of the CURRENT context window (last prompt_tokens)
        # Track plan.md freshness so we can nudge when it goes stale.
        self._last_plan_mtime: float | None = None
        self._plan_stale_steps = 0
        self._last_results_count = 0
        # Precisely how the last run() call ended: "done" | "cancelled" |
        # "max_steps". The caller (dashboard's worker()) uses this to set the
        # correct terminal status directly, rather than inferring it from
        # events.jsonl -- a stale marker from a PRIOR run() call left over in
        # that file must never be mistaken for the current one's outcome.
        self.last_stop_reason: str | None = None

    def _current_verification_attempt_limit(self) -> int:
        """Read the current per-conversation limit without breaking a run if
        the dashboard setting is temporarily unavailable."""
        if self.verification_attempt_limit is None:
            return self.max_verification_attempts
        try:
            return max(1, int(self.verification_attempt_limit()))
        except (OSError, TypeError, ValueError):
            return self.max_verification_attempts

    def _active_revision_budget(self) -> dict[str, float] | None:
        """Return the confirmed revision's hard cost boundary, if configured."""
        if self.agent_role != "Main Agent":
            return None
        if not (self.ctx.workdir / "project.json").is_file():
            return None
        try:
            from ..project_state import project_view
            active = project_view(self.ctx.workdir).get("active_revision") or {}
            if active.get("status") in {"verified", "cancelled", "failed"}:
                return None
            budget = active.get("budget") or {}
            cap = float(budget.get("max_additional_cost") or 0.0)
            baseline = float(active.get("usage_baseline_cny") or 0.0)
        except (OSError, TypeError, ValueError):
            return None
        if cap <= 0:
            return None
        return {
            "cap": cap,
            "baseline": baseline,
            "spent": max(0.0, self.total_usage.estimated_cost_cny - baseline),
        }

    def _infer_agent_role(self, scope: str) -> str:
        lowered = scope.lower()
        if lowered.startswith("verify"):
            if "triage" in lowered:
                return "Verifier · Triage"
            if "synthesis" in lowered:
                return "Verifier · Synthesis"
            return "Verifier"
        if lowered.startswith("sub"):
            suffix = "".join(char for char in lowered if char.isdigit())
            return f"Subagent {suffix}" if suffix else "Subagent"
        return "Main Agent"

    def _infer_system_prompt_source(self) -> str:
        if self.agent_role.startswith("Verifier"):
            return "Verification Agent system prompt"
        if self.agent_role.startswith("Subagent"):
            return "mathmodel/agent/prompts.py · SUBAGENT_SYSTEM"
        if self.agent_role == "Chat Agent":
            return "mathmodel/agent/build.py · CHAT_SYSTEM"
        return "mathmodel/agent/prompts.py · MODELING_SYSTEM"

    @staticmethod
    def _is_working_memory_snapshot(message: dict[str, Any]) -> bool:
        return (
            message.get("role") == "system"
            and str(message.get("content") or "").startswith(_WM_SNAPSHOT_HEADER)
        )

    @staticmethod
    def _working_memory_envelope(content: str) -> dict[str, str]:
        if not content.startswith(_WM_SNAPSHOT_HEADER):
            return {}
        fields: dict[str, str] = {}
        for line in content.splitlines()[1:8]:
            if line == "---":
                break
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip()] = value.strip()
        return fields

    def _normalize_working_memory_scaffold(self) -> None:
        """Migrate the pinned slot and recover an append-only cursor.

        Existing replace-mode sessions are upgraded by changing only their old
        mutable slot into the stable protocol. The current durable state is
        appended as a fresh snapshot before the next model request.
        """
        if self.working_memory_mode == "append_only":
            self.messages[1] = {"role": "system", "content": _WM_PROTOCOL}
            latest: tuple[int, int, str] | None = None
            for message in self.messages[2:]:
                if not self._is_working_memory_snapshot(message):
                    continue
                envelope = self._working_memory_envelope(
                    str(message.get("content") or "")
                )
                try:
                    epoch = int(envelope.get("epoch", 0))
                    version = int(envelope.get("version", 0))
                except (TypeError, ValueError):
                    continue
                digest = envelope.get("state_sha256", "")
                candidate = (epoch, version, digest)
                if latest is None or candidate[:2] > latest[:2]:
                    latest = candidate
            if latest is not None and latest[:2] >= (
                self._wm_epoch,
                self._wm_version,
            ):
                self._wm_epoch, self._wm_version, self._wm_digest = latest
            return

        # A baseline/legacy run keeps the old regenerated slot and excludes
        # append-only envelopes so it remains a clean comparison group.
        self.messages[1] = {"role": "system", "content": _WM_HEADER}
        self.messages = self.messages[:2] + [
            message
            for message in self.messages[2:]
            if not self._is_working_memory_snapshot(message)
        ]
        self._wm_epoch = 1
        self._wm_version = 0
        self._wm_digest = ""

    def _emit(self, kind: str, **data: Any) -> None:
        self.on_event(kind, data)

    def _provider_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        request_phase: str,
        step: int | None = None,
        **kwargs: Any,
    ):
        """Call the model while emitting durable out-of-band heartbeats.

        Tool execution already has a 30-second heartbeat, but a synchronous
        provider request previously had none. A legitimate long reasoning call
        could therefore cross the dashboard's five-minute stale threshold and
        be mislabeled as a dead run even though its worker was still alive.
        """
        heartbeat_done = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        started_at = time.monotonic()
        interval = float(self.ctx.heartbeat_interval_seconds)

        if self._has_event_sink and interval > 0:
            def emit_heartbeats() -> None:
                while not heartbeat_done.wait(interval):
                    if self.ctx.stop_event.is_set():
                        return
                    try:
                        self._emit(
                            "provider_heartbeat",
                            provider=type(self.provider).__name__,
                            model=self.provider.model,
                            request_phase=request_phase,
                            step=step,
                            elapsed_seconds=round(
                                time.monotonic() - started_at,
                                1,
                            ),
                            scope=self.ctx.scope,
                        )
                    except Exception:
                        # Telemetry must never interrupt the model request.
                        return

            heartbeat_thread = threading.Thread(
                target=emit_heartbeats,
                name=f"provider-heartbeat-{self.ctx.scope or 'lead'}",
                daemon=True,
            )
            heartbeat_thread.start()
        try:
            return self.provider.chat(messages, tools=tools, **kwargs)
        finally:
            heartbeat_done.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=0.1)

    # --- state persistence ---------------------------------------------------
    def _save_state(self) -> None:
        if self.state_path is None:
            return

        def write() -> None:
            if self.state_write_allowed is not None and not self.state_write_allowed():
                return
            data = {
                "messages": self.messages,
                "run_counter": dict(self.ctx.run_counter),
                "total_usage": self.total_usage.to_dict(),
                "context_tokens": self.context_tokens,
                "runtime_controls": {
                    "compaction_count": self._compaction_count,
                    "tool_prune_count": self._tool_prune_count,
                    "verification_final_delivery_pending": (
                        self._final_delivery_pending
                    ),
                    "verification_attempts_completed": (
                        self._verification_attempts_completed
                    ),
                    "working_memory": {
                        "protocol": _WM_PROTOCOL_ID,
                        "mode": self.working_memory_mode,
                        "epoch": self._wm_epoch,
                        "version": self._wm_version,
                        "state_sha256": self._wm_digest,
                    },
                },
            }
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(self.state_path)

        if self.state_lock is None:
            write()
        else:
            with self.state_lock:
                write()

    # --- working memory -----------------------------------------------------
    def _render_working_memory_state(self) -> str:
        wd = self.ctx.workdir
        parts: list[str] = []

        if self.include_planning_memory:
            from ..tools.plan import load_tasks, plan_path, render_compact
            plan = plan_path(wd)
            tasks = load_tasks(wd)
            if plan.exists():
                m = plan.stat().st_mtime
                if m != self._last_plan_mtime:
                    self._last_plan_mtime = m
                    self._plan_stale_steps = 0
                else:
                    self._plan_stale_steps += 1
            else:
                self._plan_stale_steps += 1
            parts.append("## Plan (todo list)\n" + render_compact(tasks))

        rdir = wd / "results"
        results_files: list[str] = []
        if rdir.is_dir():
            results_files = sorted(str(p.relative_to(wd)) for p in rdir.rglob("*") if p.is_file())

        if self.include_planning_memory:
            # Nudge: plan is stale AND results have advanced since it was last written.
            if self._plan_stale_steps >= 4 and len(results_files) > self._last_results_count:
                parts.insert(0, (
                    "⚠ Plan freshness warning: results/ has advanced while the plan "
                    "has remained unchanged. Call set_task_status now to record "
                    "finished sub-tasks and their key results before continuing."
                ))
            if self._plan_stale_steps == 0:
                self._last_results_count = len(results_files)

            dec = wd / "decisions.md"
            if dec.exists():
                decisions = dec.read_text().strip()
                if decisions:
                    parts.append("## Decisions (decisions.md)\n" + decisions)

        if results_files:
            parts.append("## Results available (results/)\n" + "\n".join(results_files))

        fdir = wd / "figures"
        if fdir.is_dir():
            figs = sorted(str(p.relative_to(wd)) for p in fdir.rglob("*") if p.is_file())
            if figs:
                parts.append("## Figures (figures/)\n" + "\n".join(figs))

        return "\n\n".join(parts) or "## Durable state\n(no durable entries yet)"

    def _has_open_tool_call_batch(self) -> bool:
        pending: set[str] = set()
        for message in self.messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        pending.add(call_id)
            elif message.get("role") == "tool":
                pending.discard(str(message.get("tool_call_id") or ""))
        return bool(pending)

    def _working_memory_snapshot(self, state: str, digest: str) -> str:
        next_version = self._wm_version + 1
        supersedes = (
            f"epoch={self._wm_epoch},version={self._wm_version}"
            if self._wm_version
            else "none-in-this-epoch"
        )
        return (
            f"{_WM_SNAPSHOT_HEADER}\n"
            f"protocol: {_WM_PROTOCOL_ID}\n"
            f"epoch: {self._wm_epoch}\n"
            f"version: {next_version}\n"
            f"state_sha256: {digest}\n"
            "representation: complete-materialized-state\n"
            f"supersedes: {supersedes}\n"
            "---\n"
            f"{state}\n"
            "[end working memory snapshot]"
        )

    def _latest_working_memory_content(self) -> str:
        if self.working_memory_mode == "replace":
            return str(self.messages[1].get("content") or "")
        for message in reversed(self.messages):
            if self._is_working_memory_snapshot(message):
                return str(message.get("content") or "")
        return _WM_PROTOCOL

    def _refresh_working_memory(self, *, force_snapshot: bool = False) -> bool:
        state = self._render_working_memory_state()
        if self.working_memory_mode == "replace":
            self.messages[1] = {
                "role": "system",
                "content": f"{_WM_HEADER}\n\n{state}",
            }
            return True

        digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
        if not force_snapshot and digest == self._wm_digest:
            return False
        # The OpenAI-compatible message contract requires every assistant
        # tool_call id to be followed by its role=tool result. Memory must never
        # be inserted inside that batch, even during an interrupted run.
        if self._has_open_tool_call_batch():
            return False

        snapshot = self._working_memory_snapshot(state, digest)
        self.messages.append({"role": "system", "content": snapshot})
        self._wm_version += 1
        self._wm_digest = digest
        return True

    # --- main loop ----------------------------------------------------------
    def resume_tool_result(self, tool_call_id: str, observation: str) -> None:
        """Close one durable open tool call before resuming the provider loop."""
        open_ids: set[str] = set()
        for message in self.messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        open_ids.add(call_id)
            elif message.get("role") == "tool":
                open_ids.discard(str(message.get("tool_call_id") or ""))
        if tool_call_id not in open_ids:
            raise ValueError("pending tool call is missing or already resolved")
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": observation,
        })
        self._save_state()

    def run(
        self,
        task: str | None,
        *,
        verify_on_completion: bool | None = None,
        internal_instruction: bool = False,
        turn_system_instruction: str | None = None,
    ) -> str:
        verification_attempt = self._verification_attempts_completed
        # Fresh modeling runs require a final verification even if the model
        # reaches an answer without tools. A dashboard follow-up starts in
        # conversational mode and promotes itself back to verification only
        # when it actually mutates model/result/paper artifacts.
        verification_required = (
            self.final_verifier is not None
            if verify_on_completion is None
            else bool(verify_on_completion)
        )
        finalization_announced = False
        step_limit = self.max_steps
        if turn_system_instruction:
            self.messages.append({
                "role": "system",
                "content": turn_system_instruction,
            })
        if task is not None:
            self.messages.append({
                "role": "system" if internal_instruction else "user",
                "content": task,
            })
        # Orchestrator controls such as the one-click max-step extension are
        # runtime instructions, not words authored by the user. Keep them in
        # the model context without fabricating a visible user message.
        if task is not None and not internal_instruction:
            self._emit("task", task=task)
        # This is the last durable checkpoint if the user stops while the very
        # first provider request for this follow-up is still in flight.
        self._save_state()

        step = 0
        while step < step_limit:
            step += 1
            if self.ctx.stop_event.is_set():
                self._emit("cancelled", step=step)
                self.last_stop_reason = "cancelled"
                return "[stopped by user]"
            revision_budget = self._active_revision_budget()
            if (
                revision_budget is not None
                and revision_budget["spent"] >= revision_budget["cap"]
            ):
                self._emit(
                    "budget_exhausted",
                    step=step,
                    spent_cny=revision_budget["spent"],
                    max_additional_cost_cny=revision_budget["cap"],
                )
                self.last_stop_reason = "budget_exhausted"
                self._save_state()
                return "[stopped: revision cost budget exhausted]"
            memory_appended = self._refresh_working_memory()
            self._emit(
                "context",
                step=step,
                total_tokens=self.total_usage.total_tokens,
                context_tokens=self.context_tokens,
                num_messages=len(self.messages),
                working_memory=self._latest_working_memory_content(),
                working_memory_mode=self.working_memory_mode,
                working_memory_epoch=self._wm_epoch,
                working_memory_version=self._wm_version,
                working_memory_appended=memory_appended,
            )
            # Persist everything up to (but not including) the provider result.
            # If that result arrives after a stop, the next continuation resumes
            # from this exact boundary.
            self._save_state()
            finalizing = bool(
                self.finalization_tool
                and self.finalization_steps
                and step > step_limit - self.finalization_steps
            )
            tool_schemas = self.registry.schemas()
            provider_kwargs: dict[str, Any] = {}
            if finalizing:
                if not finalization_announced:
                    self.messages.append({
                        "role": "system",
                        "content": self.finalization_instruction,
                    })
                    finalization_announced = True
                tool_schemas = [
                    schema for schema in tool_schemas
                    if schema.get("function", {}).get("name")
                    == self.finalization_tool
                ]
                if not tool_schemas:
                    raise RuntimeError(
                        f"finalization tool '{self.finalization_tool}' is not registered"
                    )
                provider_kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": self.finalization_tool},
                }
            with model_request_context(
                agent_role=self.agent_role,
                agent_scope=self.ctx.scope,
                phase="agent_step",
                step=step,
                system_prompt_source=self.system_prompt_source,
            ):
                resp = self._provider_chat(
                    self.messages,
                    tools=tool_schemas,
                    request_phase="agent_step",
                    step=step,
                    **provider_kwargs,
                )
            if self.ctx.stop_event.is_set():
                self._emit("cancelled", step=step)
                self.last_stop_reason = "cancelled"
                return "[stopped by user]"
            self.total_usage = self.total_usage + resp.usage
            # prompt_tokens is exactly how large the context we just sent was -- the
            # real "current context window" size that compaction should watch.
            if resp.usage.prompt_tokens:
                self.context_tokens = resp.usage.prompt_tokens
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.text}
            if resp.reasoning_content:
                # DeepSeek thinking-mode tool calls require the full reasoning
                # content to be returned on subsequent requests in the same turn.
                assistant_msg["reasoning_content"] = resp.reasoning_content
            if resp.tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in resp.tool_calls
                ]
            self.messages.append(assistant_msg)

            if not resp.tool_calls:
                if finalizing:
                    self._emit(
                        "assistant",
                        step=step,
                        text=resp.text,
                        reasoning_text=resp.reasoning_content,
                        tool_calls=[],
                        total_tokens=self.total_usage.total_tokens,
                        context_tokens=self.context_tokens,
                    )
                    self._emit(
                        "finalization_required",
                        step=step,
                        tool=self.finalization_tool,
                    )
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[Orchestrator correction — this is not user input]\n"
                            "Plain text is not an accepted final result. Call "
                            f"{self.finalization_tool} now with the structured "
                            "verdict based on the evidence already collected."
                        ),
                    })
                    self._save_state()
                    continue

                pending = self.pending_work() if self.pending_work is not None else None
                if pending:
                    self._emit(
                        "assistant",
                        step=step,
                        text=resp.text,
                        reasoning_text=resp.reasoning_content,
                        tool_calls=[],
                        total_tokens=self.total_usage.total_tokens,
                        context_tokens=self.context_tokens,
                    )
                    self._emit("background_pending", step=step, summary=pending)
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[Orchestrator status — this is not user input]\n"
                            f"{pending}\n"
                            "Continue working with available information while slower "
                            "sub-agents run, or call collect_subagent_results with "
                            "mode='first_completed'."
                        ),
                    })
                    self._save_state()
                    continue

                if self._final_delivery_pending:
                    # The configured number of independent attempts has already
                    # been consumed.
                    # This response is the lead Agent's one final repair/delivery
                    # pass and must not start another verification cycle.
                    self._final_delivery_pending = False
                    self._verification_attempts_completed = 0
                    self._emit(
                        "verification_override_delivery",
                        step=step,
                        attempt=(
                            verification_attempt
                            or self._current_verification_attempt_limit()
                        ),
                        text=resp.text or "",
                    )
                    self._emit("done", step=step, text=resp.text)
                    self.last_stop_reason = "done"
                    self._save_state()
                    return resp.text or ""

                if self.final_verifier is not None and verification_required:
                    verification_attempt += 1
                    self._emit(
                        "verification_start",
                        step=step,
                        attempt=verification_attempt,
                    )
                    try:
                        report = self.final_verifier(resp.text or "", verification_attempt)
                    except Exception as exc:
                        report = {
                            "verdict": "REVISE",
                            "summary": (
                                "The independent verification process failed and "
                                "therefore cannot approve this answer."
                            ),
                            "issues": [{
                                "severity": "major",
                                "category": "verification-runtime",
                                "message": f"{type(exc).__name__}: {exc}",
                                "evidence": "No valid PASS verdict was produced.",
                                "required_fix": (
                                    "Repair the verification failure and submit the "
                                    "candidate for verification again."
                                ),
                            }],
                            "attempt": verification_attempt,
                        }
                    if self.ctx.stop_event.is_set():
                        self._emit("cancelled", step=step)
                        self.last_stop_reason = "cancelled"
                        return "[stopped by user]"

                    verdict = str(report.get("verdict", "REVISE")).upper()
                    report["verdict"] = verdict
                    self._emit("verification_result", step=step, **report)
                    self._verification_attempts_completed = (
                        0 if verdict == "PASS" else verification_attempt
                    )
                    # Persist immediately after a completed verdict. If the user
                    # interrupts during the following repair phase, the next
                    # continuation must start at attempt N+1.
                    self._save_state()
                    if verdict == "PASS":
                        self._emit("done", step=step, text=resp.text)
                        self.last_stop_reason = "done"
                        self._save_state()
                        return resp.text or ""

                    issue_lines: list[str] = []
                    for index, issue in enumerate(
                        report.get("issues", []),
                        start=1,
                    ):
                        issue_lines.extend([
                            (
                                f"{index}. [{issue.get('severity', 'major')}] "
                                f"{issue.get('category', 'verification')}: "
                                f"{issue.get('message', '')}"
                            ),
                            f"   Evidence: {issue.get('evidence', '')}",
                            f"   Required fix: {issue.get('required_fix', '')}",
                        ])

                    verification_limit = self._current_verification_attempt_limit()
                    if verification_attempt >= verification_limit:
                        final_delivery_steps = max(
                            1,
                            self.repair_steps_per_verification,
                        )
                        previous_limit = step_limit
                        step_limit += final_delivery_steps
                        self._final_delivery_pending = True
                        self._emit(
                            "verification_exhausted",
                            step=step,
                            attempt=verification_attempt,
                            verdict=verdict,
                            summary=report.get("summary", ""),
                            issues=report.get("issues", []),
                        )
                        self._emit(
                            "verification_final_delivery_budget",
                            step=step,
                            attempt=verification_attempt,
                            added_steps=final_delivery_steps,
                            previous_limit=previous_limit,
                            new_limit=step_limit,
                        )
                        self.messages.append({
                            "role": "user",
                            "content": (
                                "[Final delivery after verification limit — this "
                                "is not user input]\n"
                                f"All {verification_limit} independent "
                                "verification attempts have been used. The last "
                                "verdict was not PASS:\n"
                                f"Summary: {report.get('summary', '')}\n"
                                + "\n".join(issue_lines)
                                + "\nApply the last verifier feedback directly to "
                                  "the model, result artifacts, and paper. This is "
                                  "the final repair phase: there will be no further "
                                  "verification attempt. Before answering, remove "
                                  "duplicate LaTeX labels/headings, compile the "
                                  "current paper, and check the final PDF exists. "
                                  "Then deliver the final paper to the user even if "
                                  "some non-blocking verifier concerns remain. Be "
                                  "transparent that the verification limit was "
                                  "reached; do not claim independent PASS."
                            ),
                        })
                        self._save_state()
                        continue

                    if self.repair_steps_per_verification:
                        previous_limit = step_limit
                        step_limit += self.repair_steps_per_verification
                        self._emit(
                            "verification_repair_budget",
                            step=step,
                            attempt=verification_attempt,
                            added_steps=self.repair_steps_per_verification,
                            previous_limit=previous_limit,
                            new_limit=step_limit,
                        )

                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[Independent verifier feedback — this is not user input]\n"
                            f"Verdict: {verdict}\n"
                            f"Summary: {report.get('summary', '')}\n"
                            + "\n".join(issue_lines)
                            + "\nFix the actual model, code, result artifacts, figures, "
                              "and paper as needed. Treat every finding as an issue "
                              "family: search the abstract, assumptions, derivation, "
                              "tables, figures, result JSON, validation, limitations, "
                              "and conclusion for every dependent occurrence. Build a "
                              "closure matrix that maps each verifier issue to all "
                              "changed locations, then recheck those dependencies and "
                              "the unchanged neighboring claims. First establish one "
                              "clean baseline: remove invalid duplication even if the "
                              "page count temporarily falls below target, and never "
                              "restore a rejected duplicate revision merely because it "
                              "was longer. Recover missing pages by adding substantive "
                              "derivation, validation, sensitivity, and interpretation "
                              "to the clean source with section-level edit_paragraph "
                              "calls. Do not merely edit the single quoted sentence, "
                              "rephrase the answer, or repeatedly regenerate the whole "
                              "paper. Then produce a new candidate final answer."
                        ),
                    })
                    self._save_state()
                    continue

                self._emit("done", step=step, text=resp.text)
                self.last_stop_reason = "done"
                self._save_state()
                return resp.text or ""

            self._emit(
                "assistant",
                step=step,
                text=resp.text,
                reasoning_text=resp.reasoning_content,
                tool_calls=[(tc.name, tc.arguments) for tc in resp.tool_calls],
                total_tokens=self.total_usage.total_tokens,
                context_tokens=self.context_tokens,
            )

            # Execute the turn's tool calls. Independent calls the model emitted
            # together (notably multiple spawn_subagent) run concurrently; a lone
            # call runs inline. Results are appended in the model's original order
            # so each tool_call_id keeps its matching tool message.
            if finalizing:
                observations = [
                    self._run_one_tool(tc)
                    if tc.name == self.finalization_tool
                    else (
                        "[error] inspection budget closed; only "
                        f"'{self.finalization_tool}' is allowed"
                    )
                    for tc in resp.tool_calls
                ]
            else:
                observations = self._dispatch_tool_calls(resp.tool_calls)
            # Tools may call separately billed models. Fold those records into
            # this Agent before persisting state, while keeping per-call events
            # for provider/model-level visibility in the dashboard.
            for tool_usage, metadata in self.ctx.take_model_usage_records():
                self.total_usage = self.total_usage + tool_usage
                self._emit(
                    "external_model_usage",
                    step=step,
                    usage=tool_usage.to_dict(),
                    total_usage=self.total_usage.to_dict(),
                    **metadata,
                )
            pending_question: dict[str, Any] | None = None
            for tc, observation in zip(resp.tool_calls, observations):
                if self.ctx.stop_event.is_set():
                    break
                if tc.name == "ask_user":
                    from ..tools.ask_user import (
                        bind_pending_tool_call,
                        is_pending_observation,
                        pending_record_from_observation,
                    )
                    if is_pending_observation(observation):
                        marker = pending_record_from_observation(observation)
                        pending_question = bind_pending_tool_call(
                            self.ctx.workdir,
                            str(marker["id"]),
                            tc.id,
                        )
                        continue
                tool_result_event: dict[str, Any] = {
                    "name": tc.name,
                    "observation": observation,
                }
                if "timed_out=True" in observation:
                    tool_result_event["timed_out"] = True
                self._emit("tool_result", **tool_result_event)
                self.messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": observation}
                )
                if (
                    tc.name in {
                        "ingest_problem",
                        "promote_materials",
                        "run_code",
                        "write_paper",
                        "edit_paragraph",
                        "spawn_subagent",
                    }
                    and not observation.startswith(("[error]", "[render error]"))
                ):
                    verification_required = True

            if pending_question is not None:
                self._emit(
                    "waiting_input",
                    id=pending_question["id"],
                    question_kind=pending_question.get("kind", "question"),
                    change_request_id=pending_question.get("change_request_id"),
                )
                self.last_stop_reason = "waiting_input"
                self._save_state()
                return "[waiting for user input]"

            if self.ctx.stop_event.is_set():
                self._emit("cancelled", step=step)
                self.last_stop_reason = "cancelled"
                return "[stopped by user]"

            finalized = any(
                tc.name == self.finalization_tool
                and not observation.startswith("[error]")
                for tc, observation in zip(resp.tool_calls, observations)
            )
            if finalized:
                self._emit("done", step=step, text=resp.text or "")
                self.last_stop_reason = "done"
                self._save_state()
                return resp.text or f"[completed: {self.finalization_tool}]"

            self._maybe_compact()
            self._save_state()

        if self._final_delivery_pending:
            self._final_delivery_pending = False
            exhausted_attempts = (
                verification_attempt or self._current_verification_attempt_limit()
            )
            final_text = (
                f"{exhausted_attempts} 轮独立验证已经结束。主 Agent 已完成最后一轮修订，"
                "现将当前 paper/main.pdf 作为最终论文交付；最后一轮验证"
                "意见保留在验证面板中供用户查阅。"
            )
            self._emit(
                "verification_override_delivery",
                step=step_limit,
                attempt=exhausted_attempts,
                text=final_text,
            )
            self._verification_attempts_completed = 0
            self._emit("done", step=step_limit, text=final_text)
            self.last_stop_reason = "done"
            self._save_state()
            return final_text

        self._emit("max_steps", step=step_limit)
        self.last_stop_reason = "max_steps"
        self._save_state()
        return "[stopped: reached max_steps]"

    # --- tool dispatch ------------------------------------------------------
    def _run_one_tool(self, tc: Any) -> str:
        """Parse args + dispatch a single tool call, returning its observation."""
        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            parsed: dict[str, Any] = {}
            for key, value in pairs:
                if key in parsed:
                    raise ValueError(f"duplicate argument key {key!r}")
                parsed[key] = value
            return parsed

        try:
            args = (
                json.loads(
                    tc.arguments,
                    object_pairs_hook=reject_duplicate_keys,
                )
                if tc.arguments
                else {}
            )
        except (json.JSONDecodeError, ValueError) as e:
            return f"[error] arguments not valid JSON: {e}"
        if (
            tc.name in _REVISION_MUTATION_TOOLS
            and revision_change_confirmation_required(self.ctx.workdir)
        ):
            return (
                "[revision confirmation required] The current delivery is already "
                "completed and must remain immutable. If this follow-up changes "
                "the model, computation, code, results, figures, or paper, first "
                "call ask_user with kind=change_confirmation and the options "
                "confirm/adjust/cancel. After the user confirms, retry this tool; "
                "the confirmed request creates the next revision. If the user only "
                "asked a question, answer it without mutating project artifacts."
            )
        return self.registry.dispatch(self.ctx, tc.name, args)

    def _dispatch_tool_calls(self, tool_calls: list[Any]) -> list[str]:
        """Run a turn's tool calls, concurrently when the model emitted several.

        Order is preserved (map returns in submission order) so tool results line
        up with their tool_call_ids. Handlers must never raise -- ToolRegistry.
        dispatch already wraps every handler -- so a slow/failing sub-agent can't
        wedge the batch. next_index is lock-guarded and each sandbox exec uses a
        unique script name, so concurrent handlers don't clobber each other.
        """
        if len(tool_calls) <= 1:
            return [self._run_one_tool(tc) for tc in tool_calls]
        workers = min(len(tool_calls), self.max_parallel_tools)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(self._run_one_tool, tool_calls))

    # --- compaction ---------------------------------------------------------
    def _maybe_compact(self) -> None:
        # Trigger on the CURRENT context-window size (not cumulative billing), so
        # the trigger is what compaction actually reduces -- it self-limits instead
        # of firing every step forever once a lifetime total is passed.
        policy_pruning = self.compaction_strategy in {
            POLICY_TOOL_PRUNING_V2,
            CHECKPOINT_TOOL_PRUNING_V2,
        }
        if (
            policy_pruning
            and self.context_tokens >= self.tool_prune_threshold_tokens
        ):
            level = (
                "aggressive"
                if self.context_tokens
                >= self.tool_prune_aggressive_threshold_tokens
                else "moderate"
            )
            self._prune_tool_results_by_policy(level=level)

        if self.context_tokens < self.compact_threshold_tokens:
            return
        # Keep the system prompt + stable memory protocol + a tail of recent
        # transcript. Append-only snapshots are materialized again after
        # compaction, so they do not consume tail slots or enter the summary.
        history = [
            message
            for message in self.messages[2:]
            if not self._is_working_memory_snapshot(message)
        ]
        if self.compaction_strategy in {
            CHECKPOINT_SUMMARY_V2,
            CHECKPOINT_TOOL_PRUNING_V2,
        }:
            self._compact_execution_checkpoint(history)
            return
        if self.compaction_strategy == INCREMENTAL_SUMMARY_V1:
            self._compact_incremental_summary(history)
            return
        if (
            self.compaction_strategy
            == INCREMENTAL_SUMMARY_PRESERVE_THINKING_V1
        ):
            self._compact_incremental_summary_preserve_thinking(history)
            return
        if self.compaction_strategy == EXTERNALIZED_TOOL_RESULTS_V1:
            self._compact_externalized_tool_results(history)
            return
        if self.compaction_strategy == SPLIT_USER_AGENT_V1:
            self._compact_split_user_agent(history)
            return
        if len(history) <= self.keep_tail_messages + 2:
            return
        cut = len(history) - self.keep_tail_messages
        while cut < len(history) and history[cut].get("role") != "assistant":
            cut += 1
        if cut >= len(history):
            return
        head, tail = history[:cut], history[cut:]

        before = json_measure(self.messages)
        segment = json_measure(head)
        self._emit(
            "compact_start",
            strategy=self.compaction_strategy,
            context_tokens=self.context_tokens,
            total_tokens=self.total_usage.total_tokens,
            summarizing=len(head),
            keeping=len(tail),
            context_chars_before=before["chars"],
            context_bytes_before=before["bytes"],
            segment_chars_before=segment["chars"],
            segment_bytes_before=segment["bytes"],
        )
        summary = self._summarize(head)
        self.messages = self.messages[:2] + [
            {"role": "user",
             "content": "[Earlier conversation compacted. The latest Working "
                        "Memory snapshot is authoritative. Summary of what happened:]\n"
                        + summary}
        ] + tail
        if self.working_memory_mode == "append_only":
            self._wm_epoch += 1
            self._wm_version = 0
            self._wm_digest = ""
            self._refresh_working_memory(force_snapshot=True)
        self._compaction_count += 1
        after = json_measure(self.messages)
        self._emit(
            "compact_done",
            strategy=self.compaction_strategy,
            compaction_index=self._compaction_count,
            new_len=len(self.messages),
            summarizing=len(head),
            keeping=len(tail),
            context_chars_before=before["chars"],
            context_chars_after=after["chars"],
            context_bytes_before=before["bytes"],
            context_bytes_after=after["bytes"],
            segment_chars_before=segment["chars"],
            summary_chars_after=len(summary),
            compression_ratio=round(
                after["chars"] / max(1, before["chars"]),
                6,
            ),
            summary_calls=1,
            summary_usage=self._last_compaction_summary_usage.to_dict(),
            summary_latency_seconds=self._last_compaction_summary_seconds,
        )

    def _prune_tool_results_by_policy(self, *, level: str) -> None:
        before = json_measure(self.messages)
        replaced_history, pruning = prune_tool_results_by_policy(
            self.messages[2:],
            workdir=self.ctx.workdir,
            level=level,
            recent_tool_results=self.tool_prune_recent_results,
            prune_index=self._tool_prune_count + 1,
        )
        if not pruning["pruned_tool_results"]:
            return
        self._emit(
            "tool_prune_start",
            strategy=self.compaction_strategy,
            context_tokens=self.context_tokens,
            threshold_tokens=self.tool_prune_threshold_tokens,
            aggressive_threshold_tokens=(
                self.tool_prune_aggressive_threshold_tokens
            ),
            context_chars_before=before["chars"],
            context_bytes_before=before["bytes"],
            **pruning,
        )
        self.messages = self.messages[:2] + replaced_history
        self._tool_prune_count += 1
        after = json_measure(self.messages)
        self._emit(
            "tool_prune_done",
            strategy=self.compaction_strategy,
            prune_index=self._tool_prune_count,
            context_tokens=self.context_tokens,
            threshold_tokens=self.tool_prune_threshold_tokens,
            aggressive_threshold_tokens=(
                self.tool_prune_aggressive_threshold_tokens
            ),
            context_chars_before=before["chars"],
            context_chars_after=after["chars"],
            context_bytes_before=before["bytes"],
            context_bytes_after=after["bytes"],
            compression_ratio=round(
                after["chars"] / max(1, before["chars"]),
                6,
            ),
            **pruning,
        )

    def _compact_execution_checkpoint(
        self,
        history: list[dict[str, Any]],
    ) -> None:
        if len(history) <= self.keep_tail_messages:
            return
        cut = tail_cut_preserving_tool_batch(history, self.keep_tail_messages)
        if cut <= 0 or cut >= len(history):
            return
        head, tail = history[:cut], history[cut:]
        user_history, prior_checkpoints, new_agent_trace = (
            partition_incremental_compaction_head(head)
        )
        if not head:
            return

        before = json_measure(self.messages)
        segment = json_measure(head)
        previous_checkpoint = render_prior_summary(prior_checkpoints)
        merged_users = (
            merge_user_history_append_only(user_history)
            if user_history else ""
        )
        self._emit(
            "compact_start",
            strategy=self.compaction_strategy,
            context_tokens=self.context_tokens,
            total_tokens=self.total_usage.total_tokens,
            summarizing=len(new_agent_trace),
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            user_history_messages=len(user_history),
            prior_summary_messages=len(prior_checkpoints),
            agent_trace_messages=len(new_agent_trace),
            prior_summary_chars=len(previous_checkpoint),
            context_chars_before=before["chars"],
            context_bytes_before=before["bytes"],
            segment_chars_before=segment["chars"],
            segment_bytes_before=segment["bytes"],
        )

        checkpoint = self._summarize_execution_checkpoint(
            previous_checkpoint=previous_checkpoint,
            user_history=merged_users,
            new_agent_trace=new_agent_trace,
        )
        compacted: list[dict[str, Any]] = []
        if merged_users:
            compacted.append({"role": "user", "content": merged_users})
        compacted.append({
            "role": "assistant",
            "content": render_execution_checkpoint(checkpoint),
        })
        self.messages = self.messages[:2] + compacted + tail
        if self.working_memory_mode == "append_only":
            self._wm_epoch += 1
            self._wm_version = 0
            self._wm_digest = ""
            self._refresh_working_memory(force_snapshot=True)

        self._compaction_count += 1
        after = json_measure(self.messages)
        compacted_segment = json_measure(compacted)
        self._emit(
            "compact_done",
            strategy=self.compaction_strategy,
            compaction_index=self._compaction_count,
            new_len=len(self.messages),
            summarizing=len(new_agent_trace),
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            user_history_messages=len(user_history),
            prior_summary_messages=len(prior_checkpoints),
            agent_trace_messages=len(new_agent_trace),
            prior_summary_chars=len(previous_checkpoint),
            checkpoint_chars_after=len(checkpoint),
            context_chars_before=before["chars"],
            context_chars_after=after["chars"],
            context_bytes_before=before["bytes"],
            context_bytes_after=after["bytes"],
            segment_chars_before=segment["chars"],
            summary_chars_after=compacted_segment["chars"],
            user_merged_chars=len(merged_users),
            compression_ratio=round(
                after["chars"] / max(1, before["chars"]),
                6,
            ),
            segment_compression_ratio=round(
                compacted_segment["chars"] / max(1, segment["chars"]),
                6,
            ),
            summary_calls=1,
            summary_usage=self._last_compaction_summary_usage.to_dict(),
            summary_latency_seconds=self._last_compaction_summary_seconds,
        )

    def _compact_incremental_summary(
        self,
        history: list[dict[str, Any]],
    ) -> None:
        if len(history) <= self.keep_tail_messages:
            return
        cut = tail_cut_preserving_tool_batch(
            history,
            self.keep_tail_messages,
        )
        if cut <= 0 or cut >= len(history):
            return
        head, tail = history[:cut], history[cut:]
        user_history, prior_summaries, new_agent_trace = (
            partition_incremental_compaction_head(head)
        )
        if not head:
            return

        before = json_measure(self.messages)
        segment = json_measure(head)
        prior_summary = render_prior_summary(prior_summaries)
        self._emit(
            "compact_start",
            strategy=self.compaction_strategy,
            context_tokens=self.context_tokens,
            total_tokens=self.total_usage.total_tokens,
            summarizing=len(new_agent_trace),
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            user_history_messages=len(user_history),
            prior_summary_messages=len(prior_summaries),
            agent_trace_messages=len(new_agent_trace),
            prior_summary_chars=len(prior_summary),
            context_chars_before=before["chars"],
            context_bytes_before=before["bytes"],
            segment_chars_before=segment["chars"],
            segment_bytes_before=segment["bytes"],
        )

        compacted: list[dict[str, Any]] = []
        merged_users = (
            merge_user_history_append_only(user_history)
            if user_history else ""
        )
        if merged_users:
            compacted.append({"role": "user", "content": merged_users})

        delta_summary = ""
        summary_calls = 0
        if new_agent_trace:
            delta_summary = self._summarize_agent_trace_incremental(
                prior_summary,
                new_agent_trace,
            )
            summary_calls = 1
        else:
            self._last_compaction_summary_usage = Usage()
            self._last_compaction_summary_seconds = 0.0
        if prior_summaries or delta_summary:
            compacted.append({
                "role": "assistant",
                "content": append_summary_delta(
                    prior_summaries,
                    delta_summary,
                    self._compaction_count + 1,
                ),
            })

        self.messages = self.messages[:2] + compacted + tail
        if self.working_memory_mode == "append_only":
            self._wm_epoch += 1
            self._wm_version = 0
            self._wm_digest = ""
            self._refresh_working_memory(force_snapshot=True)

        self._compaction_count += 1
        after = json_measure(self.messages)
        compacted_segment = json_measure(compacted)
        self._emit(
            "compact_done",
            strategy=self.compaction_strategy,
            compaction_index=self._compaction_count,
            new_len=len(self.messages),
            summarizing=len(new_agent_trace),
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            user_history_messages=len(user_history),
            prior_summary_messages=len(prior_summaries),
            agent_trace_messages=len(new_agent_trace),
            prior_summary_chars=len(prior_summary),
            delta_summary_chars=len(delta_summary),
            context_chars_before=before["chars"],
            context_chars_after=after["chars"],
            context_bytes_before=before["bytes"],
            context_bytes_after=after["bytes"],
            segment_chars_before=segment["chars"],
            summary_chars_after=compacted_segment["chars"],
            user_merged_chars=len(merged_users),
            compression_ratio=round(
                after["chars"] / max(1, before["chars"]),
                6,
            ),
            summary_calls=summary_calls,
            summary_usage=self._last_compaction_summary_usage.to_dict(),
            summary_latency_seconds=self._last_compaction_summary_seconds,
        )

    def _compact_externalized_tool_results(
        self,
        history: list[dict[str, Any]],
    ) -> None:
        if len(history) <= self.keep_tail_messages:
            return
        cut = len(history) - self.keep_tail_messages
        head, tail = history[:cut], history[cut:]
        before = json_measure(self.messages)
        replaced_head, externalization = externalize_tool_results(
            head,
            workdir=self.ctx.workdir,
            threshold_tokens=self.tool_result_externalize_threshold_tokens,
            preview_chars=self.tool_result_preview_chars,
            compaction_index=self._compaction_count + 1,
        )
        if not externalization["externalized_tool_results"]:
            return

        self._emit(
            "compact_start",
            strategy=self.compaction_strategy,
            context_tokens=self.context_tokens,
            total_tokens=self.total_usage.total_tokens,
            summarizing=0,
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            externalize_threshold_tokens=(
                self.tool_result_externalize_threshold_tokens
            ),
            context_chars_before=before["chars"],
            context_bytes_before=before["bytes"],
            **externalization,
        )
        self.messages = self.messages[:2] + replaced_head + tail
        if self.working_memory_mode == "append_only":
            self._wm_epoch += 1
            self._wm_version = 0
            self._wm_digest = ""
            self._refresh_working_memory(force_snapshot=True)

        self._last_compaction_summary_usage = Usage()
        self._last_compaction_summary_seconds = 0.0
        self._compaction_count += 1
        after = json_measure(self.messages)
        self._emit(
            "compact_done",
            strategy=self.compaction_strategy,
            compaction_index=self._compaction_count,
            new_len=len(self.messages),
            summarizing=0,
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            externalize_threshold_tokens=(
                self.tool_result_externalize_threshold_tokens
            ),
            context_chars_before=before["chars"],
            context_chars_after=after["chars"],
            context_bytes_before=before["bytes"],
            context_bytes_after=after["bytes"],
            compression_ratio=round(
                after["chars"] / max(1, before["chars"]),
                6,
            ),
            summary_calls=0,
            summary_usage=Usage().to_dict(),
            summary_latency_seconds=0.0,
            **externalization,
        )

    def _compact_incremental_summary_preserve_thinking(
        self,
        history: list[dict[str, Any]],
    ) -> None:
        if len(history) <= self.keep_tail_messages:
            return
        cut = tail_cut_preserving_tool_batch(
            history,
            self.keep_tail_messages,
        )
        if cut <= 0 or cut >= len(history):
            return
        head, tail = history[:cut], history[cut:]
        (
            user_history,
            prior_summaries,
            summary_trace,
            preserved_trace,
        ) = partition_thinking_preserving_compaction_head(head)
        prior_summary = render_prior_summary(prior_summaries)
        preserved_thinking_chars = sum(
            len(str(message.get("reasoning_content") or ""))
            for message in preserved_trace
        )
        preserved_tool_calls = sum(
            len(message.get("tool_calls") or [])
            for message in preserved_trace
        )
        preserved_trace, externalization = externalize_tool_results(
            preserved_trace,
            workdir=self.ctx.workdir,
            threshold_tokens=self.tool_result_externalize_threshold_tokens,
            preview_chars=self.tool_result_preview_chars,
            compaction_index=self._compaction_count + 1,
        )
        preserved_trace = mark_preserved_tool_results(preserved_trace)

        before = json_measure(self.messages)
        segment = json_measure(head)
        self._emit(
            "compact_start",
            strategy=self.compaction_strategy,
            context_tokens=self.context_tokens,
            total_tokens=self.total_usage.total_tokens,
            summarizing=len(summary_trace),
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            user_history_messages=len(user_history),
            prior_summary_messages=len(prior_summaries),
            agent_trace_messages=len(summary_trace),
            preserved_reasoning_chars=preserved_thinking_chars,
            preserved_tool_calls=preserved_tool_calls,
            prior_summary_chars=len(prior_summary),
            context_chars_before=before["chars"],
            context_bytes_before=before["bytes"],
            segment_chars_before=segment["chars"],
            segment_bytes_before=segment["bytes"],
            externalize_threshold_tokens=(
                self.tool_result_externalize_threshold_tokens
            ),
            **externalization,
        )

        merged_users = (
            merge_user_history_append_only(user_history)
            if user_history else ""
        )
        delta_summary = ""
        summary_calls = 0
        if summary_trace:
            delta_summary = self._summarize_agent_trace_incremental(
                prior_summary,
                summary_trace,
            )
            summary_calls = 1
        else:
            self._last_compaction_summary_usage = Usage()
            self._last_compaction_summary_seconds = 0.0

        compacted: list[dict[str, Any]] = []
        if merged_users:
            compacted.append({"role": "user", "content": merged_users})
        if prior_summaries or delta_summary:
            compacted.append({
                "role": "assistant",
                "content": append_summary_delta(
                    prior_summaries,
                    delta_summary,
                    self._compaction_count + 1,
                ),
            })
        compacted.extend(preserved_trace)

        self.messages = self.messages[:2] + compacted + tail
        if self.working_memory_mode == "append_only":
            self._wm_epoch += 1
            self._wm_version = 0
            self._wm_digest = ""
            self._refresh_working_memory(force_snapshot=True)

        self._compaction_count += 1
        after = json_measure(self.messages)
        technical_trace = json_measure(preserved_trace)
        self._emit(
            "compact_done",
            strategy=self.compaction_strategy,
            compaction_index=self._compaction_count,
            new_len=len(self.messages),
            summarizing=len(summary_trace),
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            user_history_messages=len(user_history),
            prior_summary_messages=len(prior_summaries),
            agent_trace_messages=len(summary_trace),
            preserved_reasoning_chars=preserved_thinking_chars,
            preserved_tool_calls=preserved_tool_calls,
            preserved_trace_chars=technical_trace["chars"],
            prior_summary_chars=len(prior_summary),
            delta_summary_chars=len(delta_summary),
            context_chars_before=before["chars"],
            context_chars_after=after["chars"],
            context_bytes_before=before["bytes"],
            context_bytes_after=after["bytes"],
            segment_chars_before=segment["chars"],
            user_merged_chars=len(merged_users),
            compression_ratio=round(
                after["chars"] / max(1, before["chars"]),
                6,
            ),
            summary_calls=summary_calls,
            summary_usage=self._last_compaction_summary_usage.to_dict(),
            summary_latency_seconds=self._last_compaction_summary_seconds,
            externalize_threshold_tokens=(
                self.tool_result_externalize_threshold_tokens
            ),
            **externalization,
        )

    def _compact_split_user_agent(
        self,
        history: list[dict[str, Any]],
    ) -> None:
        if len(history) <= self.keep_tail_messages:
            return
        cut = tail_cut_preserving_tool_batch(
            history,
            self.keep_tail_messages,
        )
        if cut <= 0 or cut >= len(history):
            return
        head, tail = history[:cut], history[cut:]
        user_history, agent_trace = partition_compaction_head(head)
        if not head:
            return

        before = json_measure(self.messages)
        segment = json_measure(head)
        self._emit(
            "compact_start",
            strategy=self.compaction_strategy,
            context_tokens=self.context_tokens,
            total_tokens=self.total_usage.total_tokens,
            summarizing=len(head),
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            user_history_messages=len(user_history),
            agent_trace_messages=len(agent_trace),
            context_chars_before=before["chars"],
            context_bytes_before=before["bytes"],
            segment_chars_before=segment["chars"],
            segment_bytes_before=segment["bytes"],
        )

        compacted: list[dict[str, Any]] = []
        merged_users = merge_user_history(user_history) if user_history else ""
        if merged_users:
            compacted.append({"role": "user", "content": merged_users})
        agent_summary = ""
        if agent_trace:
            agent_summary = self._summarize_agent_trace(agent_trace)
            compacted.append({
                "role": "assistant",
                "content": f"{AGENT_TRACE_SUMMARY_HEADER}\n{agent_summary}",
            })
        else:
            self._last_compaction_summary_usage = Usage()
            self._last_compaction_summary_seconds = 0.0

        self.messages = self.messages[:2] + compacted + tail
        if self.working_memory_mode == "append_only":
            self._wm_epoch += 1
            self._wm_version = 0
            self._wm_digest = ""
            self._refresh_working_memory(force_snapshot=True)

        self._compaction_count += 1
        after = json_measure(self.messages)
        compacted_segment = json_measure(compacted)
        self._emit(
            "compact_done",
            strategy=self.compaction_strategy,
            compaction_index=self._compaction_count,
            new_len=len(self.messages),
            summarizing=len(head),
            keeping=len(tail),
            requested_tail_messages=self.keep_tail_messages,
            user_history_messages=len(user_history),
            agent_trace_messages=len(agent_trace),
            context_chars_before=before["chars"],
            context_chars_after=after["chars"],
            context_bytes_before=before["bytes"],
            context_bytes_after=after["bytes"],
            segment_chars_before=segment["chars"],
            summary_chars_after=compacted_segment["chars"],
            user_merged_chars=len(merged_users),
            agent_summary_chars=len(agent_summary),
            compression_ratio=round(
                after["chars"] / max(1, before["chars"]),
                6,
            ),
            segment_compression_ratio=round(
                compacted_segment["chars"] / max(1, segment["chars"]),
                6,
            ),
            summary_calls=1 if agent_trace else 0,
            summary_usage=self._last_compaction_summary_usage.to_dict(),
            summary_latency_seconds=self._last_compaction_summary_seconds,
        )

    def _summarize(self, msgs: list[dict[str, Any]]) -> str:
        """Summarize a slice of the transcript with the main model."""
        transcript = []
        for m in msgs:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                calls = ", ".join(c["function"]["name"] for c in m["tool_calls"])
                transcript.append(f"assistant: (called {calls}) {m.get('content') or ''}")
            else:
                transcript.append(f"{role}: {m.get('content') or ''}")
        joined = "\n".join(transcript)[:60000]
        prompt = [
            {"role": "system", "content": "Summarize the following agent transcript "
             "into a compact set of durable facts: what was attempted, what worked/"
             "failed and why, key numeric results, and current state. Be terse; omit "
             "chit-chat. This replaces the raw transcript."},
            {"role": "user", "content": joined},
        ]
        started = time.monotonic()
        with model_request_context(
            agent_role=self.agent_role,
            agent_scope=self.ctx.scope,
            phase="context_compaction",
            system_prompt_source="Context compaction system prompt",
        ):
            resp = self._provider_chat(
                prompt,
                request_phase="context_compaction",
            )
        self._last_compaction_summary_seconds = round(
            time.monotonic() - started,
            6,
        )
        self._last_compaction_summary_usage = resp.usage
        self.total_usage = self.total_usage + resp.usage
        return resp.text or "(summary unavailable)"

    def _summarize_agent_trace(self, msgs: list[dict[str, Any]]) -> str:
        prompt = [
            {
                "role": "system",
                "content": AGENT_TRACE_SUMMARY_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": serialize_agent_trace(msgs),
            },
        ]
        started = time.monotonic()
        with model_request_context(
            agent_role=self.agent_role,
            agent_scope=self.ctx.scope,
            phase="context_compaction",
            system_prompt_source="Split context compaction system prompt",
            compaction_strategy=self.compaction_strategy,
        ):
            resp = self._provider_chat(
                prompt,
                request_phase="context_compaction",
            )
        self._last_compaction_summary_seconds = round(
            time.monotonic() - started,
            6,
        )
        self._last_compaction_summary_usage = resp.usage
        self.total_usage = self.total_usage + resp.usage
        return resp.text or "(summary unavailable)"

    def _summarize_agent_trace_incremental(
        self,
        prior_summary: str,
        msgs: list[dict[str, Any]],
    ) -> str:
        prompt = [
            {
                "role": "system",
                "content": INCREMENTAL_SUMMARY_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    "<prior_summary_reference>\n"
                    + (prior_summary or "(none — this is the first compaction)")
                    + "\n</prior_summary_reference>\n\n"
                    "<new_agent_trace>\n"
                    + serialize_agent_trace(msgs)
                    + "\n</new_agent_trace>"
                ),
            },
        ]
        started = time.monotonic()
        with model_request_context(
            agent_role=self.agent_role,
            agent_scope=self.ctx.scope,
            phase="context_compaction",
            system_prompt_source="Incremental context compaction system prompt",
            compaction_strategy=self.compaction_strategy,
        ):
            resp = self._provider_chat(
                prompt,
                request_phase="context_compaction",
            )
        self._last_compaction_summary_seconds = round(
            time.monotonic() - started,
            6,
        )
        self._last_compaction_summary_usage = resp.usage
        self.total_usage = self.total_usage + resp.usage
        return resp.text or "(summary unavailable)"

    def _summarize_execution_checkpoint(
        self,
        *,
        previous_checkpoint: str,
        user_history: str,
        new_agent_trace: list[dict[str, Any]],
    ) -> str:
        prompt = [
            {
                "role": "system",
                "content": CHECKPOINT_SUMMARY_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    "<previous_checkpoint>\n"
                    + (previous_checkpoint or "(none — first compaction)")
                    + "\n</previous_checkpoint>\n\n"
                    "<historical_user_messages_verbatim>\n"
                    + (user_history or "(none)")
                    + "\n</historical_user_messages_verbatim>\n\n"
                    "<new_execution_events>\n"
                    + serialize_agent_trace(new_agent_trace)
                    + "\n</new_execution_events>"
                ),
            },
        ]
        started = time.monotonic()
        with model_request_context(
            agent_role=self.agent_role,
            agent_scope=self.ctx.scope,
            phase="context_compaction",
            system_prompt_source="Execution checkpoint compaction system prompt",
            compaction_strategy=self.compaction_strategy,
        ):
            resp = self._provider_chat(
                prompt,
                request_phase="context_compaction",
            )
        self._last_compaction_summary_seconds = round(
            time.monotonic() - started,
            6,
        )
        self._last_compaction_summary_usage = resp.usage
        self.total_usage = self.total_usage + resp.usage
        return resp.text or "(checkpoint unavailable)"
