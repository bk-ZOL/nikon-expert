#!/usr/bin/env python3
"""scripts/classify_docs.py —— 存量文档自动定级（方案第 6.1 规则 + 第 9 章底稿）。

从 doc_meta 读全部文档，按文件名/类型规则写入 doc_acl。
  · L1–L3 自动判定 → review_status=approved（直接可检索）
  · L4（商务/客户交付物）→ review_status=pending（管理员复核+点名 ACL 后才可见）——止血
  · 判不出 → pending, L4, internal（不可检索，进管理员待办）
默认只 dry-run 打印；加 --commit 才写库。已有『人工定级』(classified_by!='auto') 的行默认不覆盖，--force 强制。

    python scripts/classify_docs.py                    # 预览
    python scripts/classify_docs.py --commit           # 写入
"""
import argparse
import os
import re
import sqlite3
import sys
from collections import Counter

# 客户词典：文件名/路径命中 → 归属域（方案 6.1 规则 5 / 第 9 章）
CUSTOMER_DICT = [
    (("星钥", "苏州星钥", "xingyue"), "customer:xingyue"),
    (("欣奕华", "xinyihua"), "customer:xinyihua"),
    (("长春", "changchun"), "customer:changchun"),
    (("鹏程", "pcl"), "customer:pcl"),
    (("8632018",), "customer:8632018"),
]

# 原厂资料文件名模式（vendor:nikon, L3）
OEM_NAME_PAT = re.compile(
    r"(Technical Information|Maintenance Guide|SM_UsersManual|レチクル設計|Reticle Design"
    r"|^EA\d|^SD\d-EXX4|^LC[-/]|^A0-EXX4|-ELCIRC-|-ELSMIF|^HFE-|^INL-|^PPD-|^RCBR-"
    r"|MCSV .*(Function|Manual)|FOUNDATION|Nikon NSR|Constants and Programs Backup"
    r"|技术规格书)",
    re.IGNORECASE,
)
OEM_TEXT_MARK = re.compile(r"NIKON CORPORATION|CONFIDENTIAL|S\d{3}E-RF-", re.IGNORECASE)


def classify(doc_name: str, doc_type: str) -> dict:
    """返回 {sec_level, owner_scope, review_status, source_class}。判不出 → 兜底不可见。"""
    name = doc_name or ""

    # 规则 1/2：原厂资料（含电路图 doc_type）
    if doc_type == "circuit_diagram" or OEM_NAME_PAT.search(name):
        return _row(3, "vendor:nikon", "approved", "oem_manual")

    # 规则 3：商务应答（L4，止血→pending）
    if any(k in name for k in ("技术应答表", "应答表", "报价", "承诺", "应答")):
        return _row(4, "internal", "pending", "commercial")

    # 规则 4/5：客户交付物（ATP/翻新报告/工作报告/验收）+ 客户词典 → L4 pending
    # 注意：不含"调试履历"——那是内部履历(rule 6)，非客户交付物
    if any(k in name for k in ("ATP", "翻新报告", "工作报告", "验收")):
        scope = _match_customer(name)
        if scope:
            return _row(4, scope, "pending", "customer_deliverable")
        # 命中交付物但认不出客户 → 待人工指定客户，保持不可见
        return _row(4, "internal", "pending", "customer_deliverable")

    # 纯客户词典命中（未含交付物关键词，仍谨慎按 L4 pending）
    scope = _match_customer(name)
    if scope:
        return _row(4, scope, "pending", "customer_deliverable")

    # 规则 6：故障履历 / 调试日志（internal, L3）
    if doc_type in ("fault_history", "worklog", "feishu_worklog") or any(
        k in name for k in ("履历", "故障排查", "调试日志", "worklog")
    ):
        return _row(3, "internal", "approved", "fault_history")

    # 系统自身文档
    if "系统实现方案" in name or "Nikon_Expert" in name:
        return _row(3, "internal", "approved", "internal_sop")

    # 规则 7：SOP / Checksheet / 通用作业（internal, L2）
    if doc_type in ("sop", "checksheet") or any(
        k in name for k in ("SOP", "操作规程", "Checksheet", "清单", "常用命令",
                            "计测", "流程", "调整", "作业", "备件", "耗材", "指南", "规范")
    ):
        return _row(2, "internal", "approved", "internal_sop")

    # 原厂正文特征兜底（若可从文本判定，此处仅按名，文本判定留摄入侧）
    if OEM_TEXT_MARK.search(name):
        return _row(3, "vendor:nikon", "approved", "oem_manual")

    # 来源待确认（方案 9 章 ★）：会议纪要 / PPMI → pending 逐份确认
    if any(k in name for k in ("会议纪要", "PPMI", "纪要")):
        return _row(4, "internal", "pending", None)

    # doc_type 兜底：已知类型归内部，不掉进 pending 黑洞
    # （OEM 手册已被上面 rule 1 捕获，走到这里的 manual 均为自编内部技术资料）
    if doc_type == "manual":
        return _row(3, "internal", "approved", "internal_sop")
    if doc_type == "obsidian_note":
        return _row(2, "internal", "approved", "internal_sop")

    # 兜底：连 doc_type 都无法归类 = 全员不可见 + 进待办（方案 6.1 兜底原则）
    return _row(4, "internal", "pending", None)


def _row(sec_level, owner_scope, review_status, source_class):
    return {"sec_level": sec_level, "owner_scope": owner_scope,
            "review_status": review_status, "source_class": source_class}


def _match_customer(name: str):
    for keys, scope in CUSTOMER_DICT:
        if any(k in name for k in keys):
            return scope
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="写库（默认仅预览）")
    ap.add_argument("--force", action="store_true", help="覆盖已人工定级的行")
    args = ap.parse_args()

    db_path = os.getenv("FTS_DB_PATH", "./data/fts.db")
    conn = sqlite3.connect(db_path)
    docs = conn.execute(
        "SELECT DISTINCT doc_id, doc_name, doc_type FROM doc_meta"
    ).fetchall()
    if not docs:
        print("doc_meta 为空")
        return 1

    # 已人工定级的 doc_id（保护，不覆盖）
    manual = {r[0] for r in conn.execute(
        "SELECT doc_id FROM doc_acl WHERE classified_by IS NOT NULL AND classified_by != 'auto'"
    ).fetchall()} if _table_exists(conn, "doc_acl") else set()

    stats = Counter()
    by_scope_level = Counter()
    written = 0
    skipped_manual = 0
    for doc_id, doc_name, doc_type in docs:
        if doc_id in manual and not args.force:
            skipped_manual += 1
            continue
        c = classify(doc_name, doc_type)
        stats[c["review_status"]] += 1
        by_scope_level[(c["owner_scope"], c["sec_level"], c["review_status"])] += 1
        if args.commit:
            conn.execute(
                """INSERT INTO doc_acl
                   (doc_id, filename, sec_level, owner_scope, review_status,
                    source_class, classified_by, classified_at)
                   VALUES (?,?,?,?,?,?, 'auto', datetime('now'))
                   ON CONFLICT(doc_id) DO UPDATE SET
                     filename=excluded.filename, sec_level=excluded.sec_level,
                     owner_scope=excluded.owner_scope, review_status=excluded.review_status,
                     source_class=excluded.source_class, classified_by='auto',
                     classified_at=datetime('now')""",
                (doc_id, doc_name, c["sec_level"], c["owner_scope"],
                 c["review_status"], c["source_class"]),
            )
            written += 1
    if args.commit:
        conn.commit()

    print(f"库: {db_path}   文档(doc_id)总数: {len(docs)}   保护未覆盖(人工): {skipped_manual}")
    print(f"review_status 分布: {dict(stats)}")
    print("\n归属域 / 密级 / 状态  →  doc_id 数：")
    for (scope, lvl, rev), n in sorted(by_scope_level.items(), key=lambda x: -x[1]):
        print(f"  {scope:<20} L{lvl}  {rev:<9} {n:>6}")
    print(f"\n{'已写入 ' + str(written) + ' 行' if args.commit else '预览(未写库)，加 --commit 落库'}")
    conn.close()
    return 0


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


if __name__ == "__main__":
    sys.exit(main())
