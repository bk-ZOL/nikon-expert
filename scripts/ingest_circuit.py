#!/usr/bin/env python3
"""摄入大型电路图 PDF 的文字层（逐页 FTS，存真实页码+路径）。

用法：
    python scripts/ingest_circuit.py "/path/to/电路图.pdf" [--model NSR-S207D/S307E]
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingestor import ingest_circuit_pdf


def main():
    ap = argparse.ArgumentParser(description="Ingest a large circuit-diagram PDF (text layer → FTS)")
    ap.add_argument("pdf", help="电路图 PDF 路径")
    ap.add_argument("--model", default="", help="机台型号标注，如 NSR-S207D/S307E")
    ap.add_argument("--limit", type=int, default=0, help="仅摄入前 N 页（调试）")
    ap.add_argument("--vector", action="store_true",
                    help="同时向量化（BGE-M3 多语言，支持中文跨语言检索；较慢）")
    args = ap.parse_args()

    n = ingest_circuit_pdf(args.pdf, machine_model=args.model, limit=args.limit,
                           vector=args.vector)
    print(f"\n📊 完成：{n} 页文字写入 FTS（可按元件/接线号/信号名检索并定位到页）")


if __name__ == "__main__":
    main()
