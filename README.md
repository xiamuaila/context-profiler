# Claude Code Context Profiler

实时分析 Claude Code 会话中各来源的 token 占用，帮你在上下文溢出前精准定位"吃掉"上下文的元凶。

---

## 为什么需要它

现有工具（如 `claude-hud`）只显示总 token 使用率，无法回答：

- 哪个文件被反复读取、累计占了多少 token？
- Bash 命令输出有没有撑爆上下文？
- 现在的上下文里，有多少是缓存命中（便宜），多少是新计算（贵）？
- 上下文窗口还剩多少余量？有没有发生过 Compaction？

---

## 功能亮点

- **精确文件归因** — 解析本地 JSONL 会话文件，精确追踪每个文件每次 Read 的实际 token 消耗
- **Bash 输出统计** — 量化每次 Bash 调用的输出体积，找出上下文杀手
- **缓存效率可视化** — 直观展示 prompt cache 命中（绿）/ 写入（蓝）/ 未命中（红）比例
- **上下文窗口利用率** — 实时显示距离 200k 上限还剩多少，超过 65% 变黄、85% 变红
- **Compaction 检测** — 自动识别 Claude Code 触发的上下文压缩事件并告警
- **零侵入** — 通过 PostToolUse hook 自动触发，Claude Code 每次调用工具后静默更新

---

## 快速安装

**前置条件**：Python 3.10+、pip

首先 clone 仓库：

```bash
git clone https://github.com/your-username/context-profiler.git
```

---

### 方式一：Claude Code Skill（推荐）

Skill 文件内嵌了完整的 `session_analyzer.py`，并能自动找到 `dashboard.html`、智能合并 `~/.claude/settings.json`（不会破坏已有配置）、启动 Dashboard——全程在 Claude Code 内完成，无需切换终端。

```bash
mkdir -p ~/.claude/skills
cp context-profiler/context-profiler.md ~/.claude/skills/
```

然后在任意 Claude Code 会话中运行：

```
/context-profiler
```

Claude 会引导你完成全部步骤。安装完成后可删除 skill 文件（节省 ~1.9k tokens/会话）：

```bash
rm ~/.claude/skills/context-profiler.md
```

---

### 方式二：Shell 脚本

```bash
cd context-profiler
bash install.sh
```

脚本会自动：
1. 将文件安装到 `~/.claude/context-profiler/`
2. 预缓存 tiktoken BPE 词表（支持网络受限环境）
3. 将 hook 注入 `~/.claude/settings.json`

安装完毕后启动 Dashboard：

```bash
cd ~/.claude/context-profiler
python3 -m http.server 17856
```

打开 http://localhost:17856/dashboard.html，每 5 秒自动刷新。

---

### 方式三：手动配置

**1. 安装依赖**

```bash
pip install tiktoken
```

**2. 配置 PostToolUse Hook**

在 `~/.claude/settings.json` 中添加：

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /absolute/path/to/context-profiler/session_analyzer.py",
            "statusMessage": "分析上下文 Token 占用..."
          }
        ]
      }
    ]
  }
}
```

**3. 启动 Dashboard**

```bash
cd /path/to/context-profiler
python3 -m http.server 17856
```

---

## 网络受限环境

tiktoken 首次运行需要从外网下载 BPE 词表。SSL 证书不受信时，手动预缓存：

```bash
mkdir -p /tmp/data-gym-cache
TIKTOKEN_URL="https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
curl -k -s -o "/tmp/data-gym-cache/$(python3 -c "
import hashlib
print(hashlib.sha1('$TIKTOKEN_URL'.encode()).hexdigest())
")" "$TIKTOKEN_URL"
```

> `/tmp` 重启后清空。如需持久化，设置环境变量 `TIKTOKEN_CACHE_DIR` 并修改路径。
>
> 无法下载时，脚本会自动回退到 `len(text) // 4` 近似估算，精度略低但不影响使用。

---

## 工作原理

### 数据来源：JSONL 会话文件

Claude Code 把每轮完整对话实时追加写入本地文件：

```
~/.claude/projects/<cwd哈希>/<session-id>.jsonl
```

每行是一条消息，有两种关键结构：

**assistant 消息**（Claude 说话 / 调用工具时产生）

```json
{
  "role": "assistant",
  "content": [
    { "type": "tool_use", "id": "toolu_xxx", "name": "Read",
      "input": { "file_path": "/path/to/file.py" } }
  ],
  "usage": {
    "input_tokens": 1,
    "cache_read_input_tokens": 41154,
    "cache_creation_input_tokens": 416,
    "output_tokens": 289
  }
}
```

**user 消息**（工具执行结果返回给模型时产生）

```json
{
  "role": "user",
  "content": [
    { "type": "tool_result", "tool_use_id": "toolu_xxx",
      "content": "1\t#!/usr/bin/env python3\n2\t..." }
  ]
}
```

`tool_result.content` 是模型**实际收到**的内容——带行号前缀、可能只是文件的一部分——比重新读磁盘更准确。

### 归因逻辑

通过 `tool_use_id` 把"是哪个文件"和"内容是什么"串联起来：

```
tool_use(id=toolu_xxx, name=Read, file_path=/path/to/file.py)
          ↕ id 对应
tool_result(tool_use_id=toolu_xxx, content="1\tline1\n2\tline2...")
```

同一文件被读 N 次，token 数累加（因为每次 Read 都是独立的 tool_result 消息留在上下文里）。

### Token 数据来源

每条 assistant 消息的 `usage` 字段是 Anthropic API 的真实返回：

| 字段 | 含义 |
| :--- | :--- |
| `cache_read_input_tokens` | 从 prompt cache 命中的（计费约为普通的 1/10）|
| `cache_creation_input_tokens` | 本轮新写入 cache 的 |
| `input_tokens` | 完全未缓存、正常计费的 |

取**最后一条** assistant 消息的 usage，代表当前上下文的最新状态。

### 数据可信度

| 来源 | 可信度 | 说明 |
| :--- | :--- | :--- |
| 文件读取 (Read) | ✅ 精确 | 来自 tool_result，是模型实际收到的内容 |
| Bash 输出 | ✅ 精确 | 同上 |
| 其他工具输出 | ✅ 精确 | 同上 |
| 对话文本 / 工具参数 | ⚠️ 下界 | 从 JSONL 计算，Claude Code 在 API 调用时会额外注入内容（system-reminder、git 状态等），这部分不写入 JSONL |
| 系统提示（估算）| ❓ 残差 | = 总量 − JSONL 追踪合计，含系统提示、工具定义、Skills、MCP tools 等 |

---

## Dashboard 使用说明

**上下文窗口利用率**：绿 → 黄（>65%）→ 红（>85%），超过 85% 建议执行 `/compact`

**总输入 Token 彩条**

- 蓝色：文件读取累计
- 橙色：Bash 输出累计
- 紫色：对话内容（回复 + 用户消息 + 工具参数）
- 灰色：系统提示（估算残差）

**缓存效率**：绿色（命中）越多越省钱

**文件读取表**：按 token 降序，`读取次数` 高的文件是优化重点

**Compaction 告警**：检测到上下文压缩时，顶部显示黄色提示及压缩前后 token 变化

### 常见优化场景

| 观察到 | 建议 |
| :--- | :--- |
| 某文件读取次数 ≥ 3，token 占比高 | 一次性读入并在对话中引用，避免重复 Read |
| Bash 输出 token 占比 >20% | 用 `head`/`grep` 缩减输出，或分步执行 |
| 缓存命中率 <50% | 会话太短或频繁重启，prompt cache 未充分复用 |
| 利用率接近 100% | 执行 `/compact` 或开启新会话 |

---

## 文件结构

```
context-profiler/
├── session_analyzer.py    # 核心：解析 JSONL，输出 context_stats.json
├── dashboard.html         # 可视化 Dashboard（浏览器打开）
├── install.sh             # 一键安装脚本
├── context-profiler.md    # Claude Code Skill 文件（/context-profiler 命令）
├── hook_handler.py        # 旧版实现（已由 session_analyzer.py 取代，保留供参考）
├── setup_hook.md          # 手动配置快速参考
├── .claude/
│   └── settings.json      # 项目级 Hook 配置示例
└── README.md
```

> `context_stats.json` 是运行时生成的，已加入 `.gitignore`。

---

## Roadmap

- [ ] Token 趋势折线图：直观看到每轮对话后上下文的增长曲线
- [ ] Bash 命令级别归因：当前统计 Bash 总量，细化到每条命令的输出 token 数
- [ ] 多会话对比：并排比较不同 session，找出哪类任务最耗上下文
- [ ] Compaction 后归因重置：压缩后自动重置文件 token 累计，避免误读历史数据
- [ ] API 代理模式：拦截 API 请求直接解析系统提示，彻底消灭灰色估算区域

---

## Contributing

欢迎提 Issue 和 PR！

- Bug 报告请附上 `context_stats.json`（注意脱敏文件路径）和复现步骤
- 新功能请先开 Issue 讨论方向

---

## License

MIT
