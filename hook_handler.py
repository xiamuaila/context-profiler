#!/usr/bin/env python3
"""
Claude Code Hook Handler for File Context Profiling.
This script is triggered by the PostToolUse hook when a 'Read' tool is executed.
It calculates the token usage of the read file and appends it to a local stats file.
"""

import sys
import json
import os
import tiktoken

# Configuration
STATS_FILE = os.path.join(os.path.dirname(__file__), "context_stats.json")
ENCODING_NAME = "cl100k_base"  # Default for Claude models

def get_token_count(text: str) -> int:
    """Calculate the number of tokens in a given text."""
    try:
        encoding = tiktoken.get_encoding(ENCODING_NAME)
        return len(encoding.encode(text))
    except Exception:
        # Fallback: approximate 1 token ~= 4 chars
        return len(text) // 4

def main():
    # Read input from stdin (provided by Claude Code Hook)
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    # Check if this is a 'Read' tool execution
    tool_name = input_data.get("tool_name")
    if tool_name != "Read":
        sys.exit(0)

    # Extract file path and content from tool input/output
    tool_input = input_data.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("path")
    
    # Note: The hook input might not directly contain the file content.
    # In a real implementation, we might need to read the file again or 
    # rely on the tool's output if available in the hook context.
    # For this prototype, we'll assume we can read the file from the path.
    
    if not file_path or not os.path.exists(file_path):
        sys.exit(0)

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        token_count = get_token_count(content)
    except Exception:
        sys.exit(0)

    # Update stats file
    stats = {}
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                stats = json.load(f)
        except:
            stats = {}

    # Accumulate stats
    if file_path not in stats:
        stats[file_path] = {"tokens": 0, "reads": 0}
    
    stats[file_path]["tokens"] = token_count  # Overwrite with latest count
    stats[file_path]["reads"] += 1

    # Write back to stats file
    with open(STATS_FILE, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2)

if __name__ == "__main__":
    main()
