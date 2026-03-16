#!/usr/bin/env python3
"""
批量摄入所有数据源：Obsidian + PDF + Excel + 故障履历
用法：python scripts/ingest_all.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src.ingestor import (
    ingest_obsidian, ingest_pdf_dir, db_status
)
from pathlib import Path


def main():
    total = 0

    # 1. Obsidian Vault
    vault = os.getenv("OBSIDIAN_VAULT_PATH", "")
    if vault and os.path.isdir(vault):
        print("\n[1/3] 摄入 Obsidian Vault...")
        total += ingest_obsidian(vault)
    else:
        print("\n[1/3] 跳过 Obsidian（未设置 OBSIDIAN_VAULT_PATH 或路径不存在）")

    # 2. PDF 手册
    manual_dir = os.getenv("MANUALS_DIR", "./data/raw/manuals")
    pdfs = list(Path(manual_dir).glob("*.pdf"))
    if pdfs:
        print(f"\n[2/3] 摄入 PDF 手册（{len(pdfs)} 个）...")
        total += ingest_pdf_dir(manual_dir)
    else:
        print(f"\n[2/3] 跳过 PDF（{manual_dir} 目录为空，可将手册放入后重新运行）")

    # 3. Excel Checksheet
    cs_dir = os.getenv("CHECKSHEETS_DIR", "./data/raw/checksheets")
    excels = list(Path(cs_dir).glob("*.xlsx")) + list(Path(cs_dir).glob("*.xls"))
    if excels:
        from src.ingestor import ingest_excel
        print(f"\n[3/3] 摄入 Checksheet（{len(excels)} 个）...")
        for f in excels:
            total += ingest_excel(str(f))
    else:
        print(f"\n[3/3] 跳过 Checksheet（{cs_dir} 目录为空）")

    print(f"\n{'='*50}")
    print(f"✅ 全部摄入完成，本次共 {total} 个 Chunk")
    s = db_status()
    print(f"   数据库总向量数：{s['points']}")


if __name__ == "__main__":
    main()
