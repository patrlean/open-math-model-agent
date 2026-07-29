"""Route a new dashboard conversation to chat or the modeling workflow."""

from __future__ import annotations

import re
from typing import Any, Callable, Literal

from ..config import build_provider
from ..providers.base import Provider, Usage, model_request_context

ConversationMode = Literal["chat", "modeling"]

_MODELING_HINTS = re.compile(
    r"(数学建模|建立.{0,8}模型|构建.{0,8}模型|建模|优化|仿真|模拟|预测|拟合|"
    r"回归|聚类|规划问题|运筹|微分方程|目标函数|约束条件|敏感性分析|"
    r"求解算法|赛题|论文|latex|mcm|icm|cumcm|结果表|数据分析)",
    re.IGNORECASE,
)
_CASUAL_EXACT = re.compile(
    r"^(你好|您好|嗨|hi|hello|在吗|谢谢|多谢|好的|好|ok|再见|拜拜)"
    r"[呀啊呢吗吧哦～~!！。,.，\s]*$",
    re.IGNORECASE,
)
_CASUAL_HINTS = re.compile(
    r"(你是谁|你能做什么|怎么使用|如何使用|介绍一下你自己|帮助中心|"
    r"随便聊聊|聊聊天|谢谢你|辛苦了)",
    re.IGNORECASE,
)

_ROUTER_SYSTEM = """\
You route the first message of a mathematical-modeling application.
Return exactly one word: MODELING or CHAT.

Use MODELING only when the user is asking to solve, formulate, compute, analyze,
optimize, simulate, validate, or write up a concrete mathematical/data-modeling
task. A substantial problem statement also counts as MODELING.

Use CHAT for greetings, thanks, product questions, general knowledge questions,
concept explanations, casual conversation, or messages that do not yet contain a
concrete modeling task. Do not answer the message. Return one word only."""


def _heuristic_route(task: str) -> ConversationMode | None:
    text = task.strip()
    if not text:
        return "chat"
    if _CASUAL_EXACT.fullmatch(text) or _CASUAL_HINTS.search(text):
        return "chat"
    if _MODELING_HINTS.search(text):
        return "modeling"
    if len(text) >= 180 or text.count("\n") >= 4:
        return "modeling"
    return None


def route_new_message(
    cfg: dict[str, Any],
    task: str,
    *,
    has_files: bool,
    provider: Provider | None = None,
    on_usage: Callable[[Usage], None] | None = None,
) -> ConversationMode:
    """Classify the first meaningful request.

    Uploaded materials always enter the modeling workflow. Clear greetings and
    clear modeling requests are handled without an extra API call. Ambiguous
    text is classified by the configured provider; if routing is unavailable,
    the conservative local fallback treats short general questions as chat and
    substantial problem statements as modeling.
    """
    if has_files:
        return "modeling"

    heuristic = _heuristic_route(task)
    if heuristic is not None:
        return heuristic

    try:
        router = provider or build_provider(cfg)
        with model_request_context(
            agent_role="Intent Router",
            phase="intent_routing",
            system_prompt_source="mathmodel/agent/intent.py · _ROUTER_SYSTEM",
        ):
            response = router.chat(
                [
                    {"role": "system", "content": _ROUTER_SYSTEM},
                    {"role": "user", "content": task.strip()},
                ],
                temperature=0,
                max_tokens=8,
            )
        if on_usage is not None:
            on_usage(response.usage)
        label = (response.text or "").strip().upper()
        if "MODELING" in label:
            return "modeling"
        if "CHAT" in label:
            return "chat"
    except Exception:
        pass

    return "modeling" if len(task.strip()) >= 120 else "chat"
