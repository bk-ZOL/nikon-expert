#!/usr/bin/env python3
"""Build / rebuild the diagram identifier inverted-index + page tags.

Incremental by default: only (re)indexes documents new or changed since last run.
Runs over whatever is already ingested — no per-document config needed.

Usage:
  ./.venv/bin/python scripts/build_diagram_index.py            # incremental
  ./.venv/bin/python scripts/build_diagram_index.py --full     # rebuild all
  ./.venv/bin/python scripts/build_diagram_index.py --types circuit_diagram manual
"""
import sys, os, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(".env")

from src.diagram_index import build_index

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="rebuild all (ignore state)")
    ap.add_argument("--types", nargs="*", default=["circuit_diagram"],
                    help="doc_type(s) to index (default: circuit_diagram)")
    args = ap.parse_args()
    stats = build_index(incremental=not args.full,
                        doc_types=tuple(args.types) if args.types else None)
    print("done:", stats)
