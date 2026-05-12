#!/usr/bin/env bash
set -e

INSTALL_DIR="$HOME/.claude/context-profiler"
SETTINGS="$HOME/.claude/settings.json"

echo "==> Installing Claude Code Context Profiler to $INSTALL_DIR"

# 1. Create install dir
mkdir -p "$INSTALL_DIR"

# 2. Copy files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/session_analyzer.py" "$INSTALL_DIR/session_analyzer.py"
cp "$SCRIPT_DIR/dashboard.html"      "$INSTALL_DIR/dashboard.html"
echo "    ✓ Copied session_analyzer.py and dashboard.html"

# 3. Pre-download tiktoken BPE file (required in restricted network environments)
TIKTOKEN_URL="https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
TIKTOKEN_CACHE_DIR="/tmp/data-gym-cache"
mkdir -p "$TIKTOKEN_CACHE_DIR"
CACHE_KEY=$(python3 -c "import hashlib; print(hashlib.sha1('$TIKTOKEN_URL'.encode()).hexdigest())")
if [ ! -f "$TIKTOKEN_CACHE_DIR/$CACHE_KEY" ]; then
    echo "    Downloading tiktoken BPE file..."
    curl -k -s -o "$TIKTOKEN_CACHE_DIR/$CACHE_KEY" "$TIKTOKEN_URL" && \
        echo "    ✓ tiktoken BPE cached" || \
        echo "    ⚠ tiktoken download failed, will use char/4 approximation"
else
    echo "    ✓ tiktoken BPE already cached"
fi

# 4. Inject PostToolUse hook into ~/.claude/settings.json
HOOK_CMD="python3 $INSTALL_DIR/session_analyzer.py"

if [ ! -f "$SETTINGS" ]; then
    cat > "$SETTINGS" <<EOF
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "$HOOK_CMD",
            "statusMessage": "分析上下文 Token 占用..."
          }
        ]
      }
    ]
  }
}
EOF
    echo "    ✓ Created $SETTINGS with hook"
else
    # Check if our hook is already present
    if grep -q "context-profiler" "$SETTINGS" 2>/dev/null; then
        echo "    ✓ Hook already present in $SETTINGS"
    else
        echo ""
        echo "    ⚠ $SETTINGS already exists."
        echo "    Please add the following to the PostToolUse hooks manually:"
        echo ""
        echo '    {'
        echo '      "matcher": ".*",'
        echo '      "hooks": [{'
        echo '        "type": "command",'
        echo "        \"command\": \"$HOOK_CMD\","
        echo '        "statusMessage": "分析上下文 Token 占用..."'
        echo '      }]'
        echo '    }'
        echo ""
    fi
fi

# 5. Check dependencies
python3 -c "import tiktoken" 2>/dev/null || {
    echo "    ⚠ tiktoken not installed. Run: pip install tiktoken"
}

echo ""
echo "==> Installation complete!"
echo ""
echo "    Next step: start the dashboard server"
echo "    cd $INSTALL_DIR && python3 -m http.server 17856"
echo "    Then open: http://localhost:17856/dashboard.html"
echo ""
echo "    The hook will auto-trigger after every Claude Code tool call."
