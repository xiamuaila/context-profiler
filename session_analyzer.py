#!/usr/bin/env python3
"""
Analyzes Claude Code session JSONL to compute accurate per-file context token usage.
Triggered via PostToolUse hook after any tool call.

Unlike the original hook_handler.py which re-read files from disk, this script reads
the session's JSONL directly — so it measures what the model actually received
(tool_result content), not the current state of the file.
"""

import sys
import json
import os
from pathlib import Path
from collections import defaultdict

STATS_FILE = Path(__file__).parent / "context_stats.json"
SESSIONS_BASE = Path.home() / ".claude/projects"


def count_tokens(text: str) -> int:
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return len(text) // 4


def find_session_file(session_id: str, cwd: str) -> Path | None:
    # Claude Code hashes the cwd by replacing '/' with '-'
    hashed_cwd = cwd.replace("/", "-")
    candidate = SESSIONS_BASE / hashed_cwd / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    # Fallback: search all project dirs
    if not SESSIONS_BASE.exists():
        return None
    for project_dir in SESSIONS_BASE.iterdir():
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def analyze_session(session_file: Path) -> dict:
    entries = []
    with open(session_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Context window sizes by model family (tokens)
    CONTEXT_WINDOWS = {
        "claude-opus-4":    200_000,
        "claude-sonnet-4":  200_000,
        "claude-haiku-4":   200_000,
        "claude-3-5-sonnet": 200_000,
        "claude-3-5-haiku":  200_000,
        "claude-3-opus":    200_000,
    }

    def get_context_window(model: str) -> int:
        for prefix, size in CONTEXT_WINDOWS.items():
            if model.startswith(prefix):
                return size
        return 200_000  # safe default for all current Claude models

    # Build map of tool_use_id → {name, input}
    tool_uses = {}
    usage_sequence = []  # total_input_tokens per assistant message, for compaction detection
    latest_usage = {}
    model = ""

    for e in entries:
        msg = e.get("message", {})
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            if not model and msg.get("model"):
                model = msg["model"]
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_uses[block["id"]] = {
                        "name": block["name"],
                        "input": block.get("input", {}),
                    }
            if "usage" in msg:
                u = msg["usage"]
                total = (
                    u.get("input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                )
                usage_sequence.append(total)
                latest_usage = u

    # Detect compaction events: consecutive total_input_tokens drop >10%
    compaction_events = []
    for i in range(1, len(usage_sequence)):
        prev, cur = usage_sequence[i - 1], usage_sequence[i]
        if prev > 0 and cur < prev * 0.9:
            compaction_events.append({"at_turn": i, "before": prev, "after": cur})

    # Accumulate token costs from tool_results, assistant text, user messages, tool_use metadata
    files: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "reads": 0})
    bash_stats = {"tokens": 0, "calls": 0}
    other_tools_stats = {"tokens": 0, "calls": 0}
    assistant_text_tokens = 0
    user_text_tokens = 0
    tool_use_meta_tokens = 0
    tool_use_meta_by_tool: dict[str, int] = {}  # tokens per tool name

    for e in entries:
        msg = e.get("message", {})
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "assistant":
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    assistant_text_tokens += count_tokens(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    t = count_tokens(json.dumps(block.get("input", {})))
                    tool_use_meta_tokens += t
                    name = block.get("name", "unknown")
                    tool_use_meta_by_tool[name] = tool_use_meta_by_tool.get(name, 0) + t

        elif role == "user":
            content_blocks = msg.get("content", [])
            if isinstance(content_blocks, str):
                user_text_tokens += count_tokens(content_blocks)
                continue
            if not isinstance(content_blocks, list):
                continue
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    user_text_tokens += count_tokens(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    raw = block.get("content", "")
                    if not isinstance(raw, str):
                        continue
                    tokens = count_tokens(raw)
                    tool = tool_uses.get(tid, {})
                    tool_name = tool.get("name", "unknown")
                    if tool_name == "Read":
                        fp = tool["input"].get("file_path", "unknown")
                        files[fp]["tokens"] += tokens
                        files[fp]["reads"] += 1
                    elif tool_name == "Bash":
                        bash_stats["tokens"] += tokens
                        bash_stats["calls"] += 1
                    else:
                        other_tools_stats["tokens"] += tokens
                        other_tools_stats["calls"] += 1

    # Total input tokens = new + cache_creation + cache_read
    input_tokens = (
        latest_usage.get("input_tokens", 0)
        + latest_usage.get("cache_creation_input_tokens", 0)
        + latest_usage.get("cache_read_input_tokens", 0)
    )

    jsonl_tracked = (
        sum(v["tokens"] for v in files.values())
        + bash_stats["tokens"]
        + other_tools_stats["tokens"]
        + assistant_text_tokens
        + user_text_tokens
        + tool_use_meta_tokens
    )

    # unattributed = total - what we can see in JSONL.
    # This bucket includes: real system prompt, tool definitions, skills, MCP tools,
    # AND dynamic injections Claude Code adds at API-call time (system-reminders,
    # git status, date, skill listings) that are NOT written to the JSONL file.
    # It is NOT a pure "system prompt" estimate.
    unattributed = max(0, input_tokens - jsonl_tracked)

    return {
        "session_id": entries[0].get("sessionId", "") if entries else "",
        "total_input_tokens": input_tokens,
        "cache_read_tokens": latest_usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": latest_usage.get("cache_creation_input_tokens", 0),
        "new_tokens": latest_usage.get("input_tokens", 0),
        "output_tokens": latest_usage.get("output_tokens", 0),
        # Accurate: derived from tool_result content stored in JSONL
        "files": dict(files),
        "bash": bash_stats,
        "other_tools": other_tools_stats,
        # JSONL lower-bound: real API request has additional injected content not in JSONL
        "assistant_text": {"tokens": assistant_text_tokens},
        "user_text": {"tokens": user_text_tokens},
        "tool_use_meta": {"tokens": tool_use_meta_tokens, "by_tool": tool_use_meta_by_tool},
        # Not attributable from JSONL: system prompt + tool defs + injected reminders
        "unattributed": unattributed,
        "compaction_events": compaction_events,
        "model": model,
        "context_window": get_context_window(model),
    }


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    session_id = hook_input.get("session_id", "")
    cwd = hook_input.get("cwd", os.getcwd())

    if not session_id:
        sys.exit(0)

    session_file = find_session_file(session_id, cwd)
    if not session_file:
        sys.exit(0)

    stats = analyze_session(session_file)

    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
