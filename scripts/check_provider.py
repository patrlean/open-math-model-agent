"""Connectivity test: confirm the configured model id actually resolves and that
tool-calling + usage reporting work end to end.

Run:  ./.venv/bin/python -m scripts.check_provider
"""

from __future__ import annotations

import json

from mathmodel.config import build_provider, load_config


def main() -> None:
    cfg = load_config()
    print(f"provider={cfg['provider']}  model={cfg['model']}  base_url={cfg['base_url']}")
    provider = build_provider(cfg)

    # 1) plain chat + usage
    resp = provider.chat(
        messages=[{"role": "user", "content": "Reply with exactly: pong"}],
        max_tokens=16,
    )
    print("\n[1] plain chat")
    print("  text:", repr((resp.text or "").strip()))
    print("  usage:", resp.usage)
    print("  finish_reason:", resp.finish_reason)

    # 2) tool calling (the agent loop depends on this working)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "add",
                "description": "Add two integers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                    },
                    "required": ["a", "b"],
                },
            },
        }
    ]
    resp2 = provider.chat(
        messages=[{"role": "user", "content": "Use the add tool to compute 21 + 21."}],
        tools=tools,
    )
    print("\n[2] tool calling")
    if resp2.tool_calls:
        for tc in resp2.tool_calls:
            print(f"  tool_call: {tc.name}({tc.arguments})")
        try:
            args = json.loads(resp2.tool_calls[0].arguments)
            print("  parsed args:", args)
        except json.JSONDecodeError:
            print("  (arguments not valid JSON)")
    else:
        print("  NO tool_calls returned. text:", repr(resp2.text))
    print("  usage:", resp2.usage)

    print("\nOK: model id resolves and responds.")


if __name__ == "__main__":
    main()
