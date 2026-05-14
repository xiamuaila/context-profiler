---
name: context-profiler
description: Install the Claude Code Context Profiler — tracks per-file and per-command token usage by parsing Claude Code JSONL session files. Run /context-profiler to install.
---

# Context Profiler Installer

**Prerequisites**: Python 3.10+

When the user runs `/context-profiler`, perform the following steps in order. Use the Write tool for file creation and Bash for shell commands.

---

## Step 1 — Create install directory

```bash
mkdir -p ~/.claude/context-profiler
```

---

## Step 2 — Write session_analyzer.py

Write the following content exactly to `~/.claude/context-profiler/session_analyzer.py`:

```python
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


def find_session_file(session_id: str, cwd: str) -> "Path | None":
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
    files: dict = defaultdict(lambda: {"tokens": 0, "reads": 0})
    bash_stats = {"tokens": 0, "calls": 0}
    other_tools_stats = {"tokens": 0, "calls": 0}
    assistant_text_tokens = 0
    user_text_tokens = 0
    tool_use_meta_tokens = 0
    tool_use_meta_by_tool: dict = {}  # tokens per tool name

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
```

---

## Step 3 — Write dashboard.html

Write the following content exactly to `~/.claude/context-profiler/dashboard.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Claude Code 上下文分析</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background: #f0f2f5; color: #1d1d1f; padding: 24px; }
        .container { max-width: 920px; margin: 0 auto; }
        h1 { font-size: 20px; font-weight: 600; margin-bottom: 20px; }

        .card { background: white; border-radius: 12px; padding: 20px 24px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }

        .compaction-alert { background: #fff3cd; border: 1px solid #f5c842; border-radius: 10px; padding: 12px 18px; margin-bottom: 16px; font-size: 13px; color: #7a5c00; display: none; }
        .compaction-alert strong { display: block; margin-bottom: 4px; }

        .gauge-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
        .gauge-label { font-size: 12px; color: #999; font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; }
        .gauge-total { font-size: 28px; font-weight: 700; }
        .gauge-bar { height: 12px; background: #e8e8ed; border-radius: 6px; overflow: hidden; display: flex; }
        .gauge-seg { height: 100%; transition: width 0.4s ease; }
        .seg-files   { background: #007aff; }
        .seg-bash    { background: #ff9500; }
        .seg-conv    { background: #af52de; }
        .seg-system  { background: #d1d1d6; }
        .gauge-legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 10px; }
        .legend-item { display: flex; align-items: center; gap: 5px; font-size: 12px; color: #555; }
        .legend-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }

        .cache-row { display: flex; gap: 24px; align-items: center; }
        .cache-nums { display: flex; gap: 28px; }
        .cache-item { text-align: center; min-width: 70px; }
        .cache-value { font-size: 20px; font-weight: 700; }
        .cache-label { font-size: 11px; color: #999; margin-top: 2px; }
        .color-cached  { color: #34c759; }
        .color-created { color: #007aff; }
        .color-new     { color: #ff3b30; }
        .cache-bar-wrap { flex: 1; }
        .cache-bar-title { font-size: 11px; color: #999; margin-bottom: 5px; }
        .cache-bar-track { height: 8px; background: #e8e8ed; border-radius: 4px; overflow: hidden; display: flex; }
        .cb-read    { background: #34c759; height: 100%; }
        .cb-created { background: #007aff; height: 100%; }
        .cb-new     { background: #ff3b30; height: 100%; }

        .breakdown-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
        .bk-item { background: #f7f8fa; border-radius: 8px; padding: 12px 14px; }
        .bk-label { font-size: 11px; color: #888; margin-bottom: 4px; display: flex; align-items: center; gap: 5px; }
        .bk-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
        .bk-value { font-size: 18px; font-weight: 700; }
        .bk-sub { font-size: 11px; color: #aaa; margin-top: 2px; }

        .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
        .section-title { font-size: 14px; font-weight: 600; }
        .section-sub { font-size: 12px; color: #999; }

        table { width: 100%; border-collapse: collapse; }
        th { font-size: 11px; font-weight: 600; color: #999; text-transform: uppercase; letter-spacing: 0.04em; padding: 0 0 8px; text-align: left; border-bottom: 1px solid #f0f0f0; }
        td { padding: 9px 0; border-bottom: 1px solid #f7f7f7; font-size: 13px; vertical-align: middle; }
        tr:last-child td { border-bottom: none; }
        .file-path { font-family: "SF Mono", Menlo, Monaco, monospace; font-size: 12px; color: #333; word-break: break-all; max-width: 400px; }
        .token-num { font-weight: 600; white-space: nowrap; }
        .reads-badge { display: inline-block; background: #f0f0f0; color: #555; border-radius: 10px; padding: 1px 7px; font-size: 11px; }
        .mini-bar { height: 5px; background: #e8e8ed; border-radius: 3px; overflow: hidden; width: 80px; }
        .mini-fill { height: 100%; border-radius: 3px; transition: width 0.4s; }

        .empty { text-align: center; padding: 28px; color: #bbb; font-size: 13px; }
        .footer { text-align: center; font-size: 11px; color: #bbb; margin-top: 4px; }

        .util-card { background: white; border-radius: 12px; padding: 16px 24px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
        .util-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
        .util-label { font-size: 12px; color: #999; font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; }
        .util-pct { font-size: 24px; font-weight: 700; }
        .util-pct.warn  { color: #ff9500; }
        .util-pct.danger{ color: #ff3b30; }
        .util-track { height: 10px; background: #e8e8ed; border-radius: 5px; overflow: hidden; }
        .util-fill  { height: 100%; border-radius: 5px; background: #34c759; transition: width 0.4s, background 0.4s; }
        .util-fill.warn   { background: #ff9500; }
        .util-fill.danger { background: #ff3b30; }
        .util-sub { font-size: 12px; color: #999; margin-top: 6px; }
    </style>
</head>
<body>
<div class="container">
    <h1>Claude Code 上下文分析</h1>

    <div class="compaction-alert" id="compaction-alert">
        <strong>⚠️ 检测到 Context Compaction（上下文压缩）</strong>
        <span id="compaction-detail"></span>
        历史消息已被压缩为摘要，文件 token 累计数反映的是压缩前的完整历史。
    </div>

    <div class="util-card">
        <div class="util-header">
            <span class="util-label">上下文窗口利用率 <span id="util-model" style="font-weight:400;text-transform:none;letter-spacing:0"></span></span>
            <span class="util-pct" id="util-pct">—</span>
        </div>
        <div class="util-track">
            <div class="util-fill" id="util-fill" style="width:0%"></div>
        </div>
        <div class="util-sub" id="util-sub">—</div>
    </div>

    <div class="card">
        <div class="gauge-header">
            <span class="gauge-label">本轮 API 总输入 Token</span>
            <span class="gauge-total" id="total-tokens">—</span>
        </div>
        <div class="gauge-bar">
            <div class="gauge-seg seg-files"  id="bar-files"  style="width:0%"></div>
            <div class="gauge-seg seg-bash"   id="bar-bash"   style="width:0%"></div>
            <div class="gauge-seg seg-conv"   id="bar-conv"   style="width:0%"></div>
            <div class="gauge-seg seg-system" id="bar-system" style="width:0%"></div>
        </div>
        <div class="gauge-legend">
            <div class="legend-item"><div class="legend-dot" style="background:#007aff"></div><span id="lg-files">文件读取</span></div>
            <div class="legend-item"><div class="legend-dot" style="background:#ff9500"></div><span id="lg-bash">Bash 输出</span></div>
            <div class="legend-item"><div class="legend-dot" style="background:#af52de"></div><span id="lg-conv">对话内容</span></div>
            <div class="legend-item"><div class="legend-dot" style="background:#d1d1d6"></div><span id="lg-system">系统提示（估算）</span></div>
        </div>
    </div>

    <div class="card">
        <div class="cache-row">
            <div class="cache-nums">
                <div class="cache-item">
                    <div class="cache-value color-cached"  id="c-read">—</div>
                    <div class="cache-label">缓存命中</div>
                </div>
                <div class="cache-item">
                    <div class="cache-value color-created" id="c-created">—</div>
                    <div class="cache-label">写入缓存</div>
                </div>
                <div class="cache-item">
                    <div class="cache-value color-new"     id="c-new">—</div>
                    <div class="cache-label">未缓存</div>
                </div>
            </div>
            <div class="cache-bar-wrap">
                <div class="cache-bar-title">缓存效率（绿色越多越省钱）</div>
                <div class="cache-bar-track">
                    <div class="cb-read"    id="cb-read"    style="width:0%"></div>
                    <div class="cb-created" id="cb-created" style="width:0%"></div>
                    <div class="cb-new"     id="cb-new"     style="width:0%"></div>
                </div>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="section-header">
            <span class="section-title">上下文来源细分</span>
        </div>
        <div style="font-size:11px;font-weight:600;color:#34c759;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px;">✓ 精确（来自 tool_result，JSONL 完整记录）</div>
        <div class="breakdown-grid" style="margin-bottom:16px;">
            <div class="bk-item">
                <div class="bk-label"><div class="bk-dot" style="background:#007aff"></div>文件读取（Read）</div>
                <div class="bk-value" id="bk-files">—</div>
                <div class="bk-sub" id="bk-files-sub">—</div>
            </div>
            <div class="bk-item">
                <div class="bk-label"><div class="bk-dot" style="background:#ff9500"></div>Bash 输出</div>
                <div class="bk-value" id="bk-bash">—</div>
                <div class="bk-sub" id="bk-bash-sub">—</div>
            </div>
            <div class="bk-item">
                <div class="bk-label"><div class="bk-dot" style="background:#ff9f0a"></div>其他工具输出</div>
                <div class="bk-value" id="bk-other">—</div>
                <div class="bk-sub" id="bk-other-sub">—</div>
            </div>
        </div>
        <div style="font-size:11px;font-weight:600;color:#ff9500;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px;">~ JSONL 下界（实际含 Claude Code 注入内容，此处未追踪）</div>
        <div class="breakdown-grid" style="margin-bottom:16px;">
            <div class="bk-item">
                <div class="bk-label"><div class="bk-dot" style="background:#af52de"></div>Claude 回复文本</div>
                <div class="bk-value" id="bk-assistant">—</div>
                <div class="bk-sub">历次回复累计</div>
            </div>
            <div class="bk-item" style="grid-column: span 2;">
                <div class="bk-label"><div class="bk-dot" style="background:#af52de"></div>工具调用参数
                    <span style="margin-left:6px;font-size:10px;background:#fff3e0;color:#e65100;border-radius:4px;padding:1px 5px;">⚠ Write/Edit 会把文件完整内容塞进参数</span>
                </div>
                <div class="bk-value" id="bk-toolmeta">—</div>
                <div id="bk-toolmeta-breakdown" style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;"></div>
                <div class="bk-sub" style="margin-top:4px;">每次调用工具时传入的参数 JSON 累计。Write 工具含文件完整内容，Edit 含 old/new 两段文本，Bash 含完整命令字符串——这些全部留在上下文历史里。</div>
            </div>
            <div class="bk-item">
                <div class="bk-label"><div class="bk-dot" style="background:#af52de"></div>你的消息文字</div>
                <div class="bk-value" id="bk-user">—</div>
                <div class="bk-sub">历次输入累计</div>
            </div>
        </div>
        <div style="font-size:11px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px;">? 未归因（JSONL 不记录，无法细分）</div>
        <div class="breakdown-grid">
            <div class="bk-item" style="grid-column: span 3; background:#f5f5f7;">
                <div class="bk-label"><div class="bk-dot" style="background:#d1d1d6"></div>系统开销 + 动态注入</div>
                <div class="bk-value" id="bk-system">—</div>
                <div class="bk-sub">= 总量 − JSONL追踪合计。含：系统提示、工具定义、Skills、MCP tools，以及 Claude Code 每轮注入的 system-reminder（任务提醒、技能列表、git状态等）。用 <strong>/context</strong> 可获得更准确的系统侧细分。</div>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="section-header">
            <span class="section-title">文件读取占用</span>
            <span class="section-sub" id="file-count">— 个文件</span>
        </div>
        <table>
            <thead>
                <tr>
                    <th>文件路径</th>
                    <th>Token 累计</th>
                    <th>读取次数</th>
                    <th>占比</th>
                </tr>
            </thead>
            <tbody id="file-list">
                <tr><td colspan="4" class="empty">暂无数据</td></tr>
            </tbody>
        </table>
    </div>

    <div class="card">
        <div class="section-header"><span class="section-title">Bash 输出占用</span></div>
        <div id="bash-summary" style="font-size:14px;color:#444;">—</div>
    </div>

    <div class="footer" id="footer">—</div>
</div>

<script>
    function escHtml(s) {
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    function fmt(n) { return Number(n).toLocaleString(); }
    function pct(a, b) { return b > 0 ? Math.min(100, a / b * 100).toFixed(1) : 0; }

    async function loadData() {
        let d;
        try {
            const r = await fetch('context_stats.json?t=' + Date.now());
            d = await r.json();
        } catch {
            document.getElementById('file-list').innerHTML = '<tr><td colspan="4" class="empty">暂无数据，请确保 Claude Code 正在运行。</td></tr>';
            return;
        }

        const total    = d.total_input_tokens || 0;
        const files    = d.files || {};
        const bash     = d.bash || { tokens: 0, calls: 0 };
        const otherT   = d.other_tools || { tokens: 0, calls: 0 };
        const astText  = (d.assistant_text || {}).tokens || 0;
        const userText = (d.user_text || {}).tokens || 0;
        const toolMeta    = (d.tool_use_meta || {}).tokens || 0;
        const toolMetaBy  = (d.tool_use_meta || {}).by_tool || {};
        const sysEst   = d.unattributed || 0;
        const compactions = d.compaction_events || [];

        const fileTok  = Object.values(files).reduce((s, v) => s + v.tokens, 0);
        const bashTok  = bash.tokens;
        const convTok  = astText + userText + toolMeta + otherT.tokens;

        const alertEl = document.getElementById('compaction-alert');
        if (compactions.length > 0) {
            alertEl.style.display = 'block';
            const last = compactions[compactions.length - 1];
            document.getElementById('compaction-detail').textContent =
                `共 ${compactions.length} 次，最近一次：${fmt(last.before)} → ${fmt(last.after)} tokens。`;
        } else {
            alertEl.style.display = 'none';
        }

        const ctxWindow = d.context_window || 200000;
        const utilPct = total / ctxWindow * 100;
        const utilEl   = document.getElementById('util-pct');
        const utilFill = document.getElementById('util-fill');
        const level = utilPct >= 85 ? 'danger' : utilPct >= 65 ? 'warn' : '';
        utilEl.className   = 'util-pct ' + level;
        utilFill.className = 'util-fill ' + level;
        utilEl.textContent = utilPct.toFixed(1) + '%';
        utilFill.style.width = Math.min(100, utilPct).toFixed(1) + '%';
        document.getElementById('util-model').textContent = d.model ? `· ${d.model}` : '';
        const remaining = ctxWindow - total;
        document.getElementById('util-sub').textContent =
            `已用 ${fmt(total)} / ${fmt(ctxWindow)} tokens，剩余约 ${fmt(remaining)} tokens` +
            (utilPct >= 85 ? ' — ⚠️ 接近上限，建议 /compact' : utilPct >= 65 ? ' — 已超过 2/3，留意上下文增长' : '');

        document.getElementById('total-tokens').textContent = fmt(total) + ' tokens';
        document.getElementById('bar-files').style.width  = pct(fileTok, total) + '%';
        document.getElementById('bar-bash').style.width   = pct(bashTok, total) + '%';
        document.getElementById('bar-conv').style.width   = pct(convTok, total) + '%';
        document.getElementById('bar-system').style.width = pct(sysEst, total) + '%';
        document.getElementById('lg-files').textContent   = `文件读取 ${fmt(fileTok)} (${pct(fileTok, total)}%)`;
        document.getElementById('lg-bash').textContent    = `Bash 输出 ${fmt(bashTok)} (${pct(bashTok, total)}%)`;
        document.getElementById('lg-conv').textContent    = `对话内容 ${fmt(convTok)} (${pct(convTok, total)}%)`;
        document.getElementById('lg-system').textContent  = `系统+注入（未归因）${fmt(sysEst)} (${pct(sysEst, total)}%)`;

        const cached  = d.cache_read_tokens || 0;
        const created = d.cache_creation_tokens || 0;
        const newTok  = d.new_tokens || 0;
        document.getElementById('c-read').textContent    = fmt(cached);
        document.getElementById('c-created').textContent = fmt(created);
        document.getElementById('c-new').textContent     = fmt(newTok);
        document.getElementById('cb-read').style.width    = pct(cached, total) + '%';
        document.getElementById('cb-created').style.width = pct(created, total) + '%';
        document.getElementById('cb-new').style.width     = pct(newTok, total) + '%';

        document.getElementById('bk-files').textContent     = fmt(fileTok) + ' tokens';
        document.getElementById('bk-files-sub').textContent = Object.keys(files).length + ' 个文件';
        document.getElementById('bk-bash').textContent      = fmt(bashTok) + ' tokens';
        document.getElementById('bk-bash-sub').textContent  = bash.calls + ' 次调用';
        document.getElementById('bk-other').textContent     = fmt(otherT.tokens) + ' tokens';
        document.getElementById('bk-other-sub').textContent = otherT.calls + ' 次调用';
        document.getElementById('bk-assistant').textContent = fmt(astText) + ' tokens';
        document.getElementById('bk-toolmeta').textContent  = fmt(toolMeta) + ' tokens';
        const TOOL_TIPS = { Write: '含文件完整内容', Edit: '含 old/new 两段文本', Bash: '含完整命令字符串' };
        const bdEl = document.getElementById('bk-toolmeta-breakdown');
        const toolMetaSorted = Object.entries(toolMetaBy).sort((a,b) => b[1]-a[1]);
        bdEl.innerHTML = toolMetaSorted.map(([name, t]) => {
            const tip = TOOL_TIPS[name] ? ` · ${TOOL_TIPS[name]}` : '';
            return `<span style="font-size:11px;background:#ede7f6;color:#6a1b9a;border-radius:4px;padding:2px 7px;white-space:nowrap;">${name} ${fmt(t)}${tip}</span>`;
        }).join('');
        document.getElementById('bk-user').textContent      = fmt(userText) + ' tokens';
        document.getElementById('bk-system').textContent    = fmt(sysEst) + ' tokens（' + pct(sysEst, total) + '%）';

        const sorted = Object.entries(files).sort((a, b) => b[1].tokens - a[1].tokens);
        document.getElementById('file-count').textContent = sorted.length + ' 个文件';
        const tbody = document.getElementById('file-list');
        tbody.innerHTML = sorted.length === 0
            ? '<tr><td colspan="4" class="empty">本次会话尚未读取任何文件</td></tr>'
            : sorted.map(([path, info]) => `
                <tr>
                    <td><div class="file-path">${escHtml(path)}</div></td>
                    <td><span class="token-num">${fmt(info.tokens)}</span></td>
                    <td><span class="reads-badge">×${info.reads}</span></td>
                    <td>
                        <div class="mini-bar"><div class="mini-fill seg-files" style="width:${pct(info.tokens, fileTok)}%"></div></div>
                        <div style="font-size:11px;color:#999;margin-top:2px">${pct(info.tokens, fileTok)}%</div>
                    </td>
                </tr>`).join('');

        document.getElementById('bash-summary').textContent = bashTok > 0
            ? `共 ${fmt(bashTok)} tokens，来自 ${bash.calls} 次调用`
            : '本次会话尚未执行 Bash 命令';

        document.getElementById('footer').textContent =
            `会话 ${(d.session_id || '').slice(0, 8)}…  ·  ${new Date().toLocaleTimeString()} 自动刷新`;
    }

    loadData();
    setInterval(loadData, 5000);
</script>
</body>
</html>
```

---

## Step 4 — Install tiktoken

```bash
python3 -c "import tiktoken" 2>/dev/null && echo "tiktoken already installed" || {
    echo "Installing tiktoken..."
    python3 -m pip install tiktoken 2>/dev/null \
        || python3 -m pip install --user tiktoken 2>/dev/null \
        || echo "⚠ tiktoken unavailable — session_analyzer will use char/4 fallback (lower accuracy)"
}
```

---

## Step 5 — Pre-cache tiktoken BPE (network-restricted environments)

```bash
mkdir -p /tmp/data-gym-cache
TIKTOKEN_URL="https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
CACHE_KEY=$(python3 -c "import hashlib; print(hashlib.sha1('$TIKTOKEN_URL'.encode()).hexdigest())")
if [ ! -f "/tmp/data-gym-cache/$CACHE_KEY" ]; then
    curl -k -s -o "/tmp/data-gym-cache/$CACHE_KEY" "$TIKTOKEN_URL" \
        && echo "tiktoken BPE cached" \
        || echo "tiktoken download failed, will use char/4 fallback"
else
    echo "tiktoken BPE already cached"
fi
```

---

## Step 6 — Install global PostToolUse hook

Use `sys.executable` to record the absolute Python path in the hook command so it works regardless of virtualenv state. Read `~/.claude/settings.json` first; if it exists, merge with existing content rather than overwriting.

Run this Python snippet to inject the hook:

```bash
python3 - <<'PYEOF'
import sys, json, os
from pathlib import Path

settings_path = Path.home() / ".claude/settings.json"
install_dir   = Path.home() / ".claude/context-profiler"
hook_cmd      = f"{sys.executable} {install_dir}/session_analyzer.py"

if settings_path.exists():
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
else:
    settings = {}

if "context-profiler" in settings_path.read_text(encoding="utf-8") if settings_path.exists() else "":
    print("Hook already present — skipping")
    sys.exit(0)

hook_entry = {
    "matcher": ".*",
    "hooks": [{
        "type": "command",
        "command": hook_cmd,
        "statusMessage": "分析上下文 Token 占用..."
    }]
}
settings.setdefault("hooks", {}).setdefault("PostToolUse", []).append(hook_entry)
settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Hook installed: {hook_cmd}")
PYEOF
```

---

## Step 7 — Start dashboard server in background

```bash
cd ~/.claude/context-profiler
python3 -m http.server 17856 > /tmp/context-profiler-server.log 2>&1 &
disown
echo "Dashboard server started on port 17856 (log: /tmp/context-profiler-server.log)"
```

Then tell the user to open: `http://localhost:17856/dashboard.html`

The dashboard auto-refreshes every 5 seconds. Data appears after Claude Code executes its next tool call.

---

## Step 8 — Smoke test

```bash
echo '{"session_id":"","cwd":"/tmp"}' | python3 ~/.claude/context-profiler/session_analyzer.py \
    && echo "✓ session_analyzer.py loads correctly" \
    || echo "⚠ session_analyzer.py failed to run — check Python version (3.10+ required)"
```

---

## After installation

Tell the user:

- **Restart Claude Code** for the hook to take effect (hooks are loaded at session start)
- The hook is **global** — it works across all Claude Code sessions automatically
- `context_stats.json` appears after the **first tool call** in a new session, then updates on every subsequent tool call
- Dashboard shows per-file token usage, Bash output, cache efficiency, and context window utilization
- File/Bash stats are **accurate** (from `tool_result` content in JSONL); conversation text stats are **JSONL lower-bounds** (Claude Code injects extra content at API time that isn't stored in JSONL)
- Use `/context` in Claude Code for the system-side breakdown (system prompt, tool definitions, skills)
