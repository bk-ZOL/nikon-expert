#!/bin/bash
# 自动重启摄入脚本，直到所有 PDF 处理完成（断点续传）
cd "/Users/jayce/Library/CloudStorage/GoogleDrive-xbtxhuhhis@gmail.com/我的云端硬盘/Obsidian/nikon_expert"
CONDA=/opt/homebrew/Caskroom/miniforge/base/bin/conda
ATTEMPT=0

while true; do
    ATTEMPT=$((ATTEMPT + 1))
    echo ""
    echo "=========================================="
    echo "第 $ATTEMPT 次运行..."
    echo "=========================================="

    PYTHONUNBUFFERED=1 $CONDA run --no-capture-output -n nikon_expert python -u scripts/ingest_nikon.py
    EXIT=$?

    if [ $EXIT -eq 0 ]; then
        echo ""
        echo "✅ 摄入完成！"
        break
    elif [ $EXIT -eq 137 ]; then
        echo ""
        echo "⚠️  内存不足被终止（OOM），5 秒后自动续传..."
        sleep 5
    else
        echo ""
        echo "❌ 意外错误（exit $EXIT），停止。"
        break
    fi
done
