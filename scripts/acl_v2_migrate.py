"""权限 v2 迁移（2026-10-15）：归属域细分为 类别 × 机型 × 客户。

做的事：
  1. 备份 data/fts.db；
  2. doc_acl 加 category / models / customer 三列，按现有定级 + 全文索引里的机型标签补齐；
  3. 账号的旧授权（internal / vendor:nikon / customer:x）换算成 cat:/model:/cust: 新格式；
  4. 新字段写进 Qdrant payload（按 doc_name），并建 payload 索引；
  5. 逐个账号对比迁移前后可见的资料，必须一致（不一致不提交）。

用法（在 /opt/nikon-expert 下）：
  .venv/bin/python scripts/acl_v2_migrate.py            # 只预览
  .venv/bin/python scripts/acl_v2_migrate.py --commit   # 写入
"""
import argparse
import collections
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.getenv("NE_ROOT") or Path(__file__).resolve().parent.parent)
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
if os.getenv("NE_CODE"):                  # 预览时用临时目录里的新代码、正式目录里的数据
    sys.path.insert(0, os.getenv("NE_CODE"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")
os.environ["SECURITY_ENABLED"] = "true"   # 下面用 acl.visible 做对比，需要真的执行判定

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from src import acl  # noqa: E402
from src.acl_classify import category_of, customer_of, models_of, models_str  # noqa: E402

DB = os.getenv("FTS_DB_PATH", "./data/fts.db")


def old_visible(user_scopes, clearance, uid, row, named):
    """迁移前的判定（acl.visible 旧版）。"""
    sec, owner, review = row["sec_level"], row["owner_scope"], row["review_status"]
    if review != "approved" or clearance < sec:
        return False
    if not (owner == acl.PUBLIC_SCOPE or owner in user_scopes):
        return False
    if sec >= 4 and uid not in named.get(row["doc_id"], set()):
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # ── 1. 备份
    if args.commit:
        bak = f"{DB}.bak-acl-v2-{time.strftime('%Y%m%d_%H%M%S')}"
        dst = sqlite3.connect(bak)
        con.backup(dst)
        dst.close()
        print(f"已备份：{bak}")

    # ── 2. doc_acl 补三列
    cols = {r[1] for r in con.execute("PRAGMA table_info(doc_acl)")}
    for c in ("category", "models", "customer"):
        if c not in cols:
            if args.commit:
                con.execute(f"ALTER TABLE doc_acl ADD COLUMN {c} TEXT")
            print(f"doc_acl 加列 {c}")

    # 一次扫全文索引：文件名 → 机型标签，doc_id → doc_type
    tags, dtype = collections.defaultdict(set), {}
    for r in con.execute("SELECT doc_id, doc_name, doc_type, machine_model FROM doc_fts"):
        if r["machine_model"]:
            tags[r["doc_name"]].add(r["machine_model"])
        dtype.setdefault(r["doc_id"], r["doc_type"] or "")
    for r in con.execute("SELECT doc_id, doc_name, doc_type, machine_model FROM doc_meta"):
        dtype.setdefault(r["doc_id"], r["doc_type"] or "")
        if r["machine_model"]:
            tags[r["doc_name"]].add(r["machine_model"])

    rows = [dict(r) for r in con.execute(
        "SELECT doc_id, filename, sec_level, owner_scope, review_status, source_class FROM doc_acl")]
    stat = collections.Counter()
    by_name = {}
    for r in rows:
        r["category"] = category_of(dtype.get(r["doc_id"], ""), r["source_class"])
        r["customer"] = customer_of(r["owner_scope"])
        r["models_list"] = models_of(tags.get(r["filename"]))
        r["models"] = models_str(r["models_list"])
        stat[(r["category"], r["customer"], r["models"])] += 1
        by_name.setdefault(r["filename"], r)
        if args.commit:
            con.execute("UPDATE doc_acl SET category=?, models=?, customer=? WHERE doc_id=?",
                        (r["category"], r["models"], r["customer"], r["doc_id"]))
    print(f"\ndoc_acl {len(rows)} 行 / {len(by_name)} 个文件，按 类别·客户·机型：")
    for k, v in stat.most_common():
        print(f"  {v:5d}  {acl.CATEGORIES.get(k[0], k[0])} · {k[1]} · {k[2]}")

    # ── 3. 账号授权换算
    named = collections.defaultdict(set)
    for r in con.execute("SELECT doc_id, uid FROM doc_acl_users"):
        named[r["doc_id"]].add(r["uid"])
    users = [dict(r) for r in con.execute("SELECT uid, role, clearance, status FROM users")]
    old_scopes = collections.defaultdict(list)
    for r in con.execute("SELECT uid, scope, granted_by, valid_until FROM user_scopes"):
        old_scopes[r["uid"]].append(dict(r))

    print("\n账号授权换算 + 迁移前后可见资料对比：")
    ok = True
    for u in users:
        uid = u["uid"]
        srows = old_scopes.get(uid, [])
        scopes = {s["scope"] for s in srows}
        cats, models, custs = acl.parse_grants(scopes)
        new_scopes = sorted({f"cat:{c}" for c in cats} | {f"model:{m}" for m in models} | {f"cust:{c}" for c in custs})
        until = max((s["valid_until"] for s in srows), default="2036-12-31T00:00:00Z")
        clearance = min(int(u["clearance"]), acl.ROLE_MAX_CLEARANCE.get(u["role"], 0))
        nu = acl.User(uid=uid, role=u["role"], clearance=clearance, active_scopes=frozenset(new_scopes))
        before = {r["filename"] for r in rows if old_visible(scopes, clearance, uid, r, named)}
        after = {r["filename"] for r in rows if acl.visible(nu, {**r, "models": r["models_list"]})}
        same = before == after
        ok &= same
        print(f"  {uid:10s} {u['role']:16s} L{clearance}  {sorted(scopes)}")
        print(f"             → {new_scopes}")
        print(f"             可见文件 迁移前 {len(before)} / 迁移后 {len(after)}  {'✅ 一致' if same else '❌ 不一致'}")
        if not same:
            print("               多出：", sorted(after - before)[:10])
            print("               少了：", sorted(before - after)[:10])
        if args.commit and same:
            con.execute("DELETE FROM user_scopes WHERE uid=?", (uid,))
            for sc in new_scopes:
                con.execute("INSERT INTO user_scopes(uid, scope, granted_by, valid_until) VALUES (?,?,?,?)",
                            (uid, sc, "migrate-v2", until))

    if not ok:
        print("\n❌ 有账号迁移前后可见范围不一致，未提交。")
        con.rollback()
        return 1

    # ── 4. Qdrant payload
    from qdrant_client import QdrantClient, models as qm
    url = os.getenv("QDRANT_URL")
    coll = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    if args.commit and url:
        client = QdrantClient(url=url)
        for field in ("category", "customer", "models"):
            try:
                client.create_payload_index(coll, field_name=field, field_schema="keyword")
            except Exception as e:
                print(f"  payload 索引 {field}：{e}")
        for i, (name, r) in enumerate(by_name.items(), 1):
            client.set_payload(
                collection_name=coll,
                payload={"category": r["category"], "models": r["models_list"], "customer": r["customer"]},
                points=qm.Filter(must=[qm.FieldCondition(key="doc_name", match=qm.MatchValue(value=name))]),
            )
            if i % 50 == 0:
                print(f"  Qdrant payload {i}/{len(by_name)}", flush=True)
        client.close()
        print(f"  Qdrant payload 已写 {len(by_name)} 个文件")

    if args.commit:
        con.commit()
        print("\n✅ 已提交")
    else:
        print("\n（预览，未写入；加 --commit 提交）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
