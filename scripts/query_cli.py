#!/usr/bin/env python3
"""
命令行查询工具。
用法：
    python scripts/query_cli.py "Nikon 对焦系统工作原理？"
    python scripts/query_cli.py "E-5301 如何排查？" --mode troubleshoot
    python scripts/query_cli.py                     # 进入交互模式
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src.engine import query


def print_result(result: dict):
    print("\n" + "═" * 60)
    if result["has_result"]:
        print("📋 回答：")
        print("─" * 60)
        print(result["answer"])
        print("\n📚 参考来源（已按相关度排序）：")
        for c in result["citations"]:
            print(f"  {c}")
    else:
        print("⚠️  " + result["answer"])
    print("═" * 60 + "\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nikon Expert CLI")
    parser.add_argument("question", nargs="?", default=None, help="要查询的问题")
    parser.add_argument("--mode", choices=["qa", "troubleshoot"],
                        default="qa", help="查询模式（qa|troubleshoot）")
    args = parser.parse_args()

    if args.question:
        # 单次查询
        print(f"\n🔍 查询：{args.question}")
        result = query(args.question, mode=args.mode)
        print_result(result)
    else:
        # 交互模式
        print("\n🤖 Nikon Expert 交互模式（输入 q 退出，输入 t 切换故障排查模式）")
        mode = "qa"
        while True:
            try:
                user_input = input(f"\n[{mode}] > ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n\n再见！")
                break

            if not user_input:
                continue
            if user_input.lower() in ("q", "quit", "exit"):
                print("再见！")
                break
            if user_input.lower() == "t":
                mode = "troubleshoot" if mode == "qa" else "qa"
                print(f"✅ 切换至：{mode} 模式")
                continue

            result = query(user_input, mode=mode)
            print_result(result)


if __name__ == "__main__":
    main()
