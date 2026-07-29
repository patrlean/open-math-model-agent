"""spawn_subagent tool: run a bounded sub-task in an isolated context.

This is the context-isolation lever. A sub-task that generates lots of one-off
tokens (iterative coding/solving/exploration) runs in a fresh Agent with its own
conversation history; the parent only sees the sub-agent's final summary. The
sub-agent shares the run workdir + sandbox, so anything it writes to results/ /
figures/ is visible to the parent -- but its debugging churn never enters the
parent's context.

Only one level deep: sub-agents do NOT get the spawn tool (see build_sub_registry).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..providers.base import Provider
from .base import Tool, ToolContext, ToolRegistry

_DEFAULT_COLLECT_TIMEOUT_SECONDS = 1800
_MAX_COLLECT_TIMEOUT_SECONDS = 1800

_PARAMS = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Self-contained sub-task with a clear, small deliverable "
            "(e.g. 'solve the sub-problem in data/q2.csv within a 20-min budget; "
            "write objective + solution to results/q2.json and report them').",
        },
        "context": {
            "type": "string",
            "description": "Any facts the sub-agent needs (paths, assumptions, budget). "
            "It does NOT see the parent conversation.",
        },
    },
    "required": ["task"],
}

_COLLECT_PARAMS = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["first_completed", "all_completed", "available"],
            "description": (
                "first_completed waits until at least one new result is ready; "
                "all_completed waits for every running sub-agent; available returns "
                "the current snapshot immediately."
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "minimum": 0,
            "maximum": _MAX_COLLECT_TIMEOUT_SECONDS,
            "description": "Maximum wait time. Defaults to 1800 seconds (30 minutes).",
        },
    },
}


@dataclass
class _BackgroundTask:
    id: int
    task: str
    status: str = "running"
    result: str = ""
    tokens: int = 0
    delivered: bool = False


class BackgroundSubagentManager:
    """Own background sub-agents for one lead Agent generation.

    Starting a task is non-blocking. Results are deliberately pulled through
    collect_subagent_results so the lead can reason after the first completion
    while slower siblings continue running.
    """

    def __init__(
        self,
        provider: Provider,
        build_sub_registry: Callable[[], ToolRegistry],
        system_prompt: str,
        compact_threshold_tokens: int,
        max_steps: int,
        on_event: Callable[[str, dict], None] | None,
    ) -> None:
        self.provider = provider
        self.build_sub_registry = build_sub_registry
        self.system_prompt = system_prompt
        self.compact_threshold_tokens = compact_threshold_tokens
        self.max_steps = max_steps
        self.on_event = on_event
        self._condition = threading.Condition()
        self._tasks: dict[int, _BackgroundTask] = {}

    def start(self, ctx: ToolContext, args: dict) -> str:
        """Start one sub-agent and return its handle without waiting."""
        from ..agent.loop import Agent  # local import avoids a cycle

        task = args["task"]
        extra = args.get("context", "").strip()
        full_task = task if not extra else f"{task}\n\nContext:\n{extra}"
        n = ctx.next_index("subagent")
        record = _BackgroundTask(id=n, task=full_task[:500])
        with self._condition:
            self._tasks[n] = record

        if self.on_event is not None:
            self.on_event("subagent_start", {"subagent": n, "task": full_task[:500]})

        def worker() -> None:
            sub_on_event = None
            if self.on_event is not None:
                def sub_on_event(kind, data):  # noqa: E306
                    self.on_event(kind, {**data, "subagent": n})

            sub_ctx = ToolContext(
                workdir=ctx.workdir,
                sandbox=ctx.sandbox,
                scope=f"sub{n}_",
                stop_event=ctx.stop_event,
                settings=ctx.settings,
            )
            status = "completed"
            result = ""
            tokens = 0
            usage: dict[str, int | float | bool] = {}
            try:
                sub = Agent(
                    provider=self.provider,
                    registry=self.build_sub_registry(),
                    ctx=sub_ctx,
                    system_prompt=self.system_prompt,
                    compact_threshold_tokens=self.compact_threshold_tokens,
                    max_steps=self.max_steps,
                    on_event=sub_on_event,
                    agent_role=f"Subagent {n}",
                    system_prompt_source=(
                        "mathmodel/agent/prompts.py · SUBAGENT_SYSTEM"
                    ),
                )
                result = sub.run(full_task)
                tokens = sub.total_usage.total_tokens
                usage = sub.total_usage.to_dict()
                if sub.last_stop_reason == "cancelled":
                    status = "cancelled"
            except Exception as exc:
                status = "failed"
                result = f"{type(exc).__name__}: {exc}"
            finally:
                with self._condition:
                    record.status = status
                    record.result = result
                    record.tokens = tokens
                    self._condition.notify_all()
                if self.on_event is not None:
                    self.on_event(
                        "subagent_end",
                        {
                            "subagent": n,
                            "tokens": tokens,
                            "usage": usage,
                            "status": status,
                            "task": full_task[:500],
                        },
                    )

        threading.Thread(target=worker, daemon=True, name=f"subagent-{n}").start()
        return (
            f"[SUB-{n} started in background]\n"
            f"Task: {full_task[:500]}\n"
            "Use collect_subagent_results to receive completed work and the "
            "current list of sub-agents that are still running."
        )

    def collect(self, ctx: ToolContext, args: dict) -> str:
        """Wait according to mode, then return results plus an activity snapshot."""
        mode = args.get("mode") or "first_completed"
        timeout = min(
            float(_MAX_COLLECT_TIMEOUT_SECONDS),
            max(
                0.0,
                float(args.get(
                    "timeout_seconds",
                    _DEFAULT_COLLECT_TIMEOUT_SECONDS,
                )),
            ),
        )
        deadline = time.monotonic() + timeout

        with self._condition:
            def ready() -> bool:
                newly_done = any(
                    task.status != "running" and not task.delivered
                    for task in self._tasks.values()
                )
                any_running = any(
                    task.status == "running" for task in self._tasks.values()
                )
                if mode == "available":
                    return True
                if mode == "all_completed":
                    return not any_running
                return newly_done or not any_running

            while not ready() and not ctx.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Wake regularly so a dashboard stop does not remain blocked
                # behind a long result-collection timeout.
                self._condition.wait(timeout=min(0.25, remaining))

            completed = [
                task for task in self._tasks.values()
                if task.status != "running" and not task.delivered
            ]
            for task in completed:
                task.delivered = True
            running = [
                task for task in self._tasks.values() if task.status == "running"
            ]
            delivered_count = sum(task.delivered for task in self._tasks.values())
            total_count = len(self._tasks)

        sections: list[str] = []
        if completed:
            sections.append("## Newly completed sub-agents")
            for task in completed:
                sections.append(
                    f"### SUB-{task.id} — {task.status}\n"
                    f"Task: {task.task}\n"
                    f"Tokens: {task.tokens}\n"
                    f"Result:\n{task.result}"
                )
        else:
            sections.append("## Newly completed sub-agents\nNone in this collection.")

        if running:
            sections.append("## Sub-agents still running")
            sections.extend(
                f"- SUB-{task.id}: {task.task}" for task in running
            )
        else:
            sections.append("## Sub-agents still running\nNone.")

        sections.append(
            "## Collaboration progress\n"
            f"{delivered_count}/{total_count} results delivered to the lead Agent; "
            f"{len(running)} still running."
        )
        return "\n\n".join(sections)

    def pending_summary(self) -> str | None:
        """Describe work that must be collected before the lead may finish."""
        with self._condition:
            running = [
                task for task in self._tasks.values() if task.status == "running"
            ]
            ready = [
                task for task in self._tasks.values()
                if task.status != "running" and not task.delivered
            ]
            if not running and not ready:
                return None
            lines = ["Background collaboration is not fully collected."]
            if ready:
                lines.append(
                    "Completed and ready to collect: "
                    + ", ".join(f"SUB-{task.id} ({task.task})" for task in ready)
                )
            if running:
                lines.append("Still running:")
                lines.extend(f"- SUB-{task.id}: {task.task}" for task in running)
            lines.append(
                "Call collect_subagent_results before giving the final answer."
            )
            return "\n".join(lines)


def make_background_subagent_tools(
    provider: Provider,
    build_sub_registry: Callable[[], ToolRegistry],
    system_prompt: str,
    compact_threshold_tokens: int = 1_000_000,
    max_steps: int = 30,
    on_event: Callable[[str, dict], None] | None = None,
) -> tuple[Tool, Tool, BackgroundSubagentManager]:
    """Return non-blocking spawn/collect tools and their shared task manager."""
    manager = BackgroundSubagentManager(
        provider,
        build_sub_registry,
        system_prompt,
        compact_threshold_tokens,
        max_steps,
        on_event,
    )
    spawn_tool = Tool(
        name="spawn_subagent",
        description=(
            "Start a bounded sub-task in the background and return immediately with "
            "its SUB id. Start independent tasks together, then call "
            "collect_subagent_results in a later turn."
        ),
        parameters=_PARAMS,
        handler=manager.start,
    )
    collect_tool = Tool(
        name="collect_subagent_results",
        description=(
            "Receive newly completed background sub-agent results. Every response "
            "also lists each sub-agent that is still running and its assigned task. "
            "Use mode='first_completed' to let the lead reason as soon as one result "
            "arrives; use mode='all_completed' before final synthesis."
        ),
        parameters=_COLLECT_PARAMS,
        handler=manager.collect,
    )
    return spawn_tool, collect_tool, manager


def make_spawn_subagent_tool(
    provider: Provider,
    build_sub_registry: Callable[[], ToolRegistry],
    system_prompt: str,
    compact_threshold_tokens: int = 1_000_000,
    max_steps: int = 30,
    on_event: Callable[[str, dict], None] | None = None,
) -> Tool:
    """Build the legacy blocking spawn tool.

    The lead dashboard Agent uses make_background_subagent_tools instead. This
    wrapper remains for callers that rely on the original synchronous contract.
    """

    def _spawn(ctx: ToolContext, args: dict) -> str:
        from ..agent.loop import Agent  # local import avoids a cycle

        task = args["task"]
        extra = args.get("context", "").strip()
        full_task = task if not extra else f"{task}\n\nContext:\n{extra}"

        n = ctx.next_index("subagent")
        # Tag every sub-event with its subagent id so the dashboard can nest it.
        sub_on_event = None
        if on_event is not None:
            def sub_on_event(kind, data):  # noqa: E306
                on_event(kind, {**data, "subagent": n})
            on_event("subagent_start", {"subagent": n, "task": full_task[:500]})

        # Share the same stop_event object (not a fresh one): a parent-level
        # cancellation must reach every in-flight sub-agent too.
        sub_ctx = ToolContext(workdir=ctx.workdir, sandbox=ctx.sandbox, scope=f"sub{n}_",
                               stop_event=ctx.stop_event, settings=ctx.settings)
        sub = Agent(
            provider=provider,
            registry=build_sub_registry(),
            ctx=sub_ctx,
            system_prompt=system_prompt,
            compact_threshold_tokens=compact_threshold_tokens,
            max_steps=max_steps,
            on_event=sub_on_event,
        )
        result = sub.run(full_task)
        tok = sub.total_usage.total_tokens
        if on_event is not None:
            on_event(
                "subagent_end",
                {
                    "subagent": n,
                    "tokens": tok,
                    "usage": sub.total_usage.to_dict(),
                },
            )
        return (
            f"[subagent #{n} finished; {tok} tokens used internally]\n{result}"
        )

    return Tool(
        name="spawn_subagent",
        description="Delegate a bounded sub-task to a fresh agent with an isolated "
        "context. Use when a sub-task will produce lots of intermediate steps but you "
        "only need its conclusion (it shares results/ and figures/). Returns the "
        "sub-agent's final summary.",
        parameters=_PARAMS,
        handler=_spawn,
    )
