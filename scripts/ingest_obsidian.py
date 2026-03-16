#!/usr/bin/env python3
"""
摄入 Obsidian Vault 到向量数据库。
用法：
    python scripts/ingest_obsidian.py /path/to/vault
    python scripts/ingest_obsidian.py           # 使用 .env 中的 OBSIDIAN_VAULT_PATH
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src.ingestor import ingest_obsidian, db_status


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("vault", nargs="?", default=os.getenv("OBSIDIAN_VAULT_PATH", ""))
    parser.add_argument("--limit", type=int, default=0, help="只摄入前 N 个文件（0=全部）")
    args = parser.parse_args()
    vault = args.vault

    if not vault:
        print("❌ 请提供 Obsidian Vault 路径：")
        print("   python scripts/ingest_obsidian.py /path/to/vault")
        print("   或在 .env 中设置 OBSIDIAN_VAULT_PATH")
        sys.exit(1)

    if not os.path.isdir(vault):
        print(f"❌ 路径不存在：{vault}")
        sys.exit(1)

    n = ingest_obsidian(vault, limit=args.limit)

    print("\n📊 向量库状态：")
    s = db_status()
    print(f"   Collection：{s['collection']}")
    print(f"   总向量数：  {s['points']}")


if __name__ == "__main__":
    main()
