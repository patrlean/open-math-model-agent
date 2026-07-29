"""The ReAct main loop.

A single agent freely decides what to do next (write code, run it, read results,
backtrack) by emitting tool calls. Control flow is model-driven -- there is no
hardcoded workflow/state machine.

Context management (the two levers from the design):
- Working memory: plan.md + decisions.md + a results index are re-surfaced into a
  single pinned message every turn, so continuity comes from re-reading canonical
  files, not from the transcript surviving.
- Compaction: token usage is accumulated from each response; past the configured
  threshold the older transcript is summarized (by the main model) into one
  message. This is safe because the durable state lives in files.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..providers.base import Provider, Usage, model_request_context
from ..tools.base import ToolContext, ToolRegistry
from .prompts import strip_legacy_modeling_user_suffix

_WM_HEADER = "[working memory — regenerated each turn from files; do not treat as user input]"


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
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.ctx = ctx
        self.compact_threshold_tokens = compact_threshold_tokens
        self.keep_tail_messages = keep_tail_messages
        self.max_steps = max_steps
        # Fan-out cap for a single turn's tool calls (e.g. several spawn_subagent
        # emitted together run concurrently instead of one-at-a-time).
        self.max_parallel_tools = max(1, max_parallel_tools)
        self.on_event = on_event or (lambda kind, data: None)
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
            # messages[0] = system prompt, messages[1] = pinned working memory.
            self.messages = [
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": _WM_HEADER},
            ]
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

    def _emit(self, kind: str, **data: Any) -> None:
        self.on_event(kind, data)

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
                    "verification_final_delivery_pending": (
                        self._final_delivery_pending
                    ),
                    "verification_attempts_completed": (
                        self._verification_attempts_completed
                    ),
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
    def _refresh_working_memory(self) -> None:
        wd = self.ctx.workdir
        parts = [_WM_HEADER]

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

        # Nudge: plan is stale AND results have advanced since it was last written.
        if self._plan_stale_steps >= 4 and len(results_files) > self._last_results_count:
            parts.insert(1, (
                f"⚠ the plan has not changed for {self._plan_stale_steps} steps, but "
                f"results/ has grown ({len(results_files)} files). Call set_task_status "
                "now to mark finished sub-tasks done (with their key result) before "
                "continuing."
            ))
        if self._plan_stale_steps == 0:
            self._last_results_count = len(results_files)

        dec = wd / "decisions.md"
        if dec.exists():
            tail = "\n".join(dec.read_text().splitlines()[-20:])
            parts.append("## Recent decisions (decisions.md)\n" + tail)

        if results_files:
            parts.append("## Results available (results/)\n" + "\n".join(results_files))

        fdir = wd / "figures"
        if fdir.is_dir():
            figs = sorted(str(p.relative_to(wd)) for p in fdir.rglob("*") if p.is_file())
            if figs:
                parts.append("## Figures (figures/)\n" + "\n".join(figs))

        self.messages[1] = {"role": "system", "content": "\n\n".join(parts)}

    # --- main loop ----------------------------------------------------------
    def run(
        self,
        task: str,
        *,
        verify_on_completion: bool | None = None,
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
        self.messages.append({"role": "user", "content": task})
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
            self._refresh_working_memory()
            self._emit(
                "context",
                step=step,
                total_tokens=self.total_usage.total_tokens,
                context_tokens=self.context_tokens,
                num_messages=len(self.messages),
                working_memory=self.messages[1]["content"],
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
                resp = self.provider.chat(
                    self.messages,
                    tools=tool_schemas,
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
            for tc, observation in zip(resp.tool_calls, observations):
                if self.ctx.stop_event.is_set():
                    break
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
                        "run_code",
                        "write_paper",
                        "edit_paragraph",
                        "spawn_subagent",
                    }
                    and not observation.startswith(("[error]", "[render error]"))
                ):
                    verification_required = True

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
        if self.context_tokens < self.compact_threshold_tokens:
            return
        # Head to summarize is messages[2:cut]; keep system + working memory + a
        # tail of recent messages. Align cut to an assistant message so we never
        # split an assistant(tool_calls) from its tool results.
        history = self.messages[2:]
        if len(history) <= self.keep_tail_messages + 2:
            return
        cut = len(history) - self.keep_tail_messages
        while cut < len(history) and history[cut].get("role") != "assistant":
            cut += 1
        if cut >= len(history):
            return
        head, tail = history[:cut], history[cut:]

        self._emit("compact_start", context_tokens=self.context_tokens,
                   total_tokens=self.total_usage.total_tokens,
                   summarizing=len(head), keeping=len(tail))
        summary = self._summarize(head)
        self.messages = self.messages[:2] + [
            {"role": "user",
             "content": "[Earlier conversation compacted. Durable state is in "
                        "plan.md / decisions.md / results/. Summary of what happened:]\n"
                        + summary}
        ] + tail
        self._emit("compact_done", new_len=len(self.messages))

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
        with model_request_context(
            agent_role=self.agent_role,
            agent_scope=self.ctx.scope,
            phase="context_compaction",
            system_prompt_source="Context compaction system prompt",
        ):
            resp = self.provider.chat(prompt)
        self.total_usage = self.total_usage + resp.usage
        return resp.text or "(summary unavailable)"
