"""
Wraps the Claude API: builds the system prompt from bot_config.py and the
knowledge_base/ folder, runs the tool-use loop against services/tools.py,
and hands back plain text for the frontend.

Docs: https://platform.claude.com/docs/en/api/messages
      https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
"""

import json
import os
from pathlib import Path

import anthropic

import bot_config
from services.tools import TOOL_FUNCTIONS, TOOL_SCHEMAS

KB_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"

# Safety valve so one confused back-and-forth can't loop forever against
# a paid API if a tool result keeps making Claude want to call it again.
MAX_TOOL_ITERATIONS = 4


def _load_knowledge_base() -> str:
    """
    Concatenates every markdown file in knowledge_base/ into one block of
    text for the system prompt.

    This is intentionally simple — "stuff the whole document store into
    context" — which is fine while your docs are a handful of short
    files. If your real policy/FAQ library grows past what comfortably
    fits in a prompt, swap this for a retrieval step (e.g. embed the docs
    and pull back only the top few relevant chunks per question) without
    changing anything else in this file.
    """
    if not KB_DIR.exists():
        return ""
    sections = []
    for path in sorted(KB_DIR.glob("*.md")):
        title = path.stem.replace("_", " ").title()
        sections.append(f"### {title}\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(sections)


def build_system_prompt() -> str:
    knowledge_base = _load_knowledge_base()
    rules = "\n".join(f"- {rule}" for rule in bot_config.RULES)

    return f"""You are {bot_config.BOT_NAME}, the virtual customer support assistant for {bot_config.BUSINESS_NAME}, an e-commerce store.

# Tone
{bot_config.TONE}

# Rules
{rules}

# Tasks you can perform
You have tools to check an order's status, book a service appointment, and start a return. Use them instead of guessing whenever a request calls for one.

# Company knowledge base
Answer questions about policies, shipping, and products using ONLY the information below. If something isn't covered here and no tool applies, say so honestly and offer to connect the customer with a human associate.

{knowledge_base}
"""


def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=api_key)


def get_reply(user_message: str, history: list[dict]) -> dict:
    """
    Sends one turn to Claude, resolving any tool calls it makes along the
    way, and returns the final text reply.

    Args:
        user_message: the customer's new message.
        history: prior turns as [{"role": "user"|"assistant", "content": str}, ...].
                 The frontend keeps this in the browser and resends it each
                 time — there's no server-side session store in this POC.

    Returns:
        {"reply": str, "tool_used": str | None}
    """
    client = _get_client()
    messages = [*history, {"role": "user", "content": user_message}]
    tool_used = None

    response = client.messages.create(
        model=bot_config.CLAUDE_MODEL,
        max_tokens=bot_config.MAX_TOKENS,
        system=build_system_prompt(),
        tools=TOOL_SCHEMAS,
        messages=messages,
    )

    iterations = 0
    while response.stop_reason == "tool_use" and iterations < MAX_TOOL_ITERATIONS:
        iterations += 1
        tool_calls = [block for block in response.content if block.type == "tool_use"]

        # Claude's turn (including the tool_use block(s)) must be echoed
        # back before the tool_result, or the next call will 400.
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for call in tool_calls:
            tool_used = call.name
            fn = TOOL_FUNCTIONS.get(call.name)
            try:
                result = fn(**call.input) if fn else {"error": f"Unknown tool '{call.name}'"}
            except Exception as exc:
                # A malformed call (Claude passing a missing/unexpected
                # argument, for example) shouldn't take down the whole
                # request — feed the error back so Claude can recover
                # (retry, ask a clarifying question, or apologize).
                result = {"error": f"'{call.name}' failed: {exc}"}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})

        response = client.messages.create(
            model=bot_config.CLAUDE_MODEL,
            max_tokens=bot_config.MAX_TOKENS,
            system=build_system_prompt(),
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

    reply_text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not reply_text:
        # Can happen if we hit MAX_TOOL_ITERATIONS while Claude still wanted
        # to call another tool (its last response is then just a tool_use
        # block, with no text) — or any other case with no text content.
        # Never hand the frontend an empty string to render as a bubble.
        reply_text = (
            "Sorry, I'm having trouble putting together an answer for that one. "
            "Could you try rephrasing, or would you like me to connect you with "
            "a human associate?"
        )
    return {"reply": reply_text, "tool_used": tool_used}
