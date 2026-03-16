#!/bin/bash
# ════════════════════════════════════════════════════════
# Nikon Expert — 命令行快速查询
# 用法：
#   bash query.sh "你的问题"
#   bash query.sh                    # 进入交互模式
#   bash query.sh "E-5301" --troubleshoot
# ════════════════════════════════════════════════════════
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    ollama serve &>/tmp/ollama.log &
    sleep 2
fi

if [ -n "$1" ] && [ "$1" != "--troubleshoot" ]; then
    QUESTION="$1"
    MODE="qa"
    if [ "$2" = "--troubleshoot" ] || [ "$2" = "-t" ]; then
        MODE="troubleshoot"
    fi
    conda run -n nikon_expert python scripts/query_cli.py "$QUESTION" --mode "$MODE"
else
    # 交互模式
    conda run -n nikon_expert python scripts/query_cli.py
fi
