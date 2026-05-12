---
name: context-profiler
description: Install the Claude Code Context Profiler — a tool that tracks per-file and per-command token usage in your session by parsing the Claude Code JSONL session file. Run /context-profiler to install. Safe to delete this skill file after installation (~1.9k tokens saved per conversation).
---

# Context Profiler Installer

This skill installs the Claude Code Context Profiler into `~/.claude/context-profiler/`.

**When the user runs `/context-profiler`**, perform the following steps:

## Step 1 — Create install directory

```bash
mkdir -p ~/.claude/context-profiler
```

## Step 2 — Write session_analyzer.py

Write the following content exactly to `~/.claude/context-profiler/session_analyzer.py`:

```python
#!/usr/bin/env python3
"""
Analyzes Claude Code session JSONL to compute accurate per-file context token usage.
Triggered via PostToolUse hook after any tool call.
"""

import sys
import json
import os
import tiktoken
from pathlib import Path
from collections import defaultdict

STATS_FILE = Path(__file__).parent / "context_stats.json"
SESSIONS_BASE = Path.home() / ".claude/projects"
ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    try:
        return len(ENCODING.encode(text))
    except Exception:
        return len(text) // 4


def find_session_file(session_id: str, cwd: str) -> Path | None:
    hashed_cwd = cwd.replace("/", "-")
    candidate = SESSIONS_BASE / hashed_cwd / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
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

    CONTEXT_WINDOWS = {
        "claude-opus-4": 200_000, "claude-sonnet-4": 200_000,
        "claude-haiku-4": 200_000, "claude-3-5-sonnet": 200_000,
        "claude-3-5-haiku": 200_000, "claude-3-opus": 200_000,
    }

    def get_context_window(model: str) -> int:
        for prefix, size in CONTEXT_WINDOWS.items():
            if model.startswith(prefix):
                return size
        return 200_000

    tool_uses = {}
    usage_sequence = []
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
                    tool_uses[block["id"]] = {"name": block["name"], "input": block.get("input", {})}
            if "usage" in msg:
                u = msg["usage"]
                usage_sequence.append(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0))
                latest_usage = u

    compaction_events = []
    for i in range(1, len(usage_sequence)):
        prev, cur = usage_sequence[i - 1], usage_sequence[i]
        if prev > 0 and cur < prev * 0.9:
            compaction_events.append({"at_turn": i, "before": prev, "after": cur})

    files: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "reads": 0})
    bash_stats = {"tokens": 0, "calls": 0}
    other_tools_stats = {"tokens": 0, "calls": 0}
    assistant_text_tokens = user_text_tokens = tool_use_meta_tokens = 0

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
                    tool_use_meta_tokens += count_tokens(json.dumps(block.get("input", {})))
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

    input_tokens = (latest_usage.get("input_tokens", 0) + latest_usage.get("cache_creation_input_tokens", 0) + latest_usage.get("cache_read_input_tokens", 0))
    jsonl_tracked = sum(v["tokens"] for v in files.values()) + bash_stats["tokens"] + other_tools_stats["tokens"] + assistant_text_tokens + user_text_tokens + tool_use_meta_tokens

    return {
        "session_id": entries[0].get("sessionId", "") if entries else "",
        "total_input_tokens": input_tokens,
        "cache_read_tokens": latest_usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": latest_usage.get("cache_creation_input_tokens", 0),
        "new_tokens": latest_usage.get("input_tokens", 0),
        "output_tokens": latest_usage.get("output_tokens", 0),
        "files": dict(files), "bash": bash_stats, "other_tools": other_tools_stats,
        "assistant_text": {"tokens": assistant_text_tokens},
        "user_text": {"tokens": user_text_tokens},
        "tool_use_meta": {"tokens": tool_use_meta_tokens},
        "unattributed": max(0, input_tokens - jsonl_tracked),
        "compaction_events": compaction_events,
        "model": model, "context_window": get_context_window(model),
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
```

## Step 3 — Copy dashboard.html

Check if the user has already cloned the context-token repo. If yes, copy the dashboard:

```bash
# Find the repo if already cloned
REPO=$(find ~ -name "dashboard.html" -path "*/context-token/*" 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
if [ -n "$REPO" ]; then
    cp "$REPO/dashboard.html" ~/.claude/context-profiler/dashboard.html
    echo "Copied dashboard.html from $REPO"
else
    echo "dashboard.html not found locally — please copy it manually from the repo"
fi
```

If the repo is not found, tell the user they need to copy `dashboard.html` from the repo manually.

## Step 4 — Pre-cache tiktoken BPE (network-restricted environments)

```bash
mkdir -p /tmp/data-gym-cache
TIKTOKEN_URL="https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
CACHE_KEY=$(python3 -c "import hashlib; print(hashlib.sha1('$TIKTOKEN_URL'.encode()).hexdigest())")
[ -f "/tmp/data-gym-cache/$CACHE_KEY" ] || curl -k -s -o "/tmp/data-gym-cache/$CACHE_KEY" "$TIKTOKEN_URL" && echo "tiktoken cached" || echo "tiktoken download failed, will use char/4 fallback"
```

## Step 5 — Install global PostToolUse hook

Check `~/.claude/settings.json`. If it doesn't exist or has no `PostToolUse` hook for context-profiler, add it:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/context-profiler/session_analyzer.py",
            "statusMessage": "分析上下文 Token 占用..."
          }
        ]
      }
    ]
  }
}
```

Use the Read and Edit tools to safely merge with existing settings rather than overwriting.

## Step 6 — Start dashboard

```bash
cd ~/.claude/context-profiler && python3 -m http.server 17856
```

Then tell the user: open `http://localhost:17856/dashboard.html` in the browser. Data refreshes every 5 seconds automatically.

---

## After installation

Tell the user:
- The hook is now **global** — it works across all Claude Code sessions
- Dashboard shows per-file token usage, Bash output, cache efficiency, context window utilization
- File/Bash stats are **accurate** (from tool_result content in JSONL)
- Conversation text stats are **JSONL lower-bounds** (Claude Code injects extra content at API time that isn't stored in JSONL)
- Use `/context` in Claude Code for system-side breakdown (system prompt, tool defs, skills)
- **Tip**: you can safely delete this skill file (`~/.claude/skills/context-profiler.md`) after installation to save ~1.9k tokens per conversation
