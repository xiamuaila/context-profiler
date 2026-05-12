# 配置指南：Claude Code 上下文 Token 分析器

## 1. 安装依赖

```bash
pip install tiktoken
```

## 2. 预下载 tiktoken BPE 词表（网络受限环境必做）

tiktoken 首次运行时需要从外网下载 BPE 词表文件。在网络受限或 SSL 证书不受信的环境中，
需要手动将其缓存到本地：

```bash
mkdir -p /tmp/data-gym-cache
curl -k -o /tmp/data-gym-cache/$(python3 -c "
import hashlib
url = 'https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken'
print(hashlib.sha1(url.encode()).hexdigest())
") https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
```

之后 tiktoken 会自动从 `/tmp/data-gym-cache/` 读取，无需联网。

> 注意：`/tmp` 重启后会清空。如需持久化，将 `TIKTOKEN_CACHE_DIR` 设为其他路径，
> 并将上面命令中的 `/tmp/data-gym-cache` 替换为对应路径。

## 3. 配置 Claude Code Hook

在项目根目录的 `.claude/settings.json` 中添加以下配置，让每次工具调用后
自动触发分析脚本：

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

将路径替换为本项目的实际绝对路径。

## 4. 启动 Dashboard

```bash
cd /path/to/context-profiler
python3 -m http.server 17856
```

然后打开 `http://localhost:17856/dashboard.html`，每 5 秒自动刷新一次数据。

## 数据说明

| 字段 | 含义 |
| :--- | :--- |
| 总输入 Token | 最近一次 API 调用时模型收到的完整上下文大小 |
| 缓存命中 | 从 prompt cache 直接读取的 token 数（计费约为普通的 1/10）|
| 写入缓存 | 本轮新写入 prompt cache 的 token 数 |
| 未缓存 | 未命中缓存、正常计费的 token 数 |
| 文件 Token（累计）| 本次会话内对该文件所有 Read 调用的 token 之和 |
| Bash Token（累计）| 本次会话内所有 Bash 命令输出的 token 之和 |
