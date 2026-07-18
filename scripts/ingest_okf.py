#!/usr/bin/env python3
"""摄入 OKF（Open Knowledge Format）知识库。

用法：
    python scripts/ingest_okf.py <OKF目录或.md文件> [--limit N]
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingestor import ingest_okf


def main():
    ap = argparse.ArgumentParser(description="Ingest an OKF bundle (dir) or a single .md file")
    ap.add_argument("path", help="OKF 目录或单个 .md 文件")
    ap.add_argument("--limit", type=int, default=0, help="仅摄入前 N 个文件（调试用）")
    args = ap.parse_args()

    n = ingest_okf(args.path, limit=args.limit)
    print(f"\n📊 完成：共写入 {n} 个 Chunk")


if __name__ == "__main__":
    main()
