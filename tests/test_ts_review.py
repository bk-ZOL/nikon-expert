"""Troubleshooting 待确认：批量确认只动 pending，确认后才给别人看、才被智能体引用。

运行：python -m pytest tests/test_ts_review.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src import acl, kb_extract, ts_table


@pytest.fixture()
def tsdb(tmp_path, monkeypatch):
    db = str(tmp_path / "ts.db")
    monkeypatch.setenv("TS_TABLE_DB", db)
    monkeypatch.setenv("FTS_DB_PATH", str(tmp_path / "fts.db"))
    monkeypatch.setenv("SECURITY_ENABLED", "false")
    monkeypatch.setattr(ts_table, "DB", db)
    return db


def _eng(*scopes):
    return acl.User(uid="eng", role="engineer", clearance=4, active_scopes=frozenset(scopes))


def _insert(status="pending", title="晶圆卡盘真空不足", machine="S207D", origin="field",
            customers="", customer="none", updated_at="2026-10-01 00:00:00", code=""):
    c = ts_table._conn()
    n = c.execute("SELECT COUNT(*) FROM ts_entries").fetchone()[0] + 1
    key = f"k{n}"
    c.execute("""INSERT INTO ts_entries(entry_key, machine, unit, error_code, title, status, origin,
                 customers, match_key, updated_at, root_causes, solutions)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
              (key, machine, "WS", code, title, status, origin, customers,
               ts_table.match_key(code), updated_at, "[]",
               '[{"action": "重接真空管", "count": 1}]'))
    eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("""INSERT INTO ts_cases(entry_id, description, machine, unit, src_doc, dedup_key, review, customer)
                 VALUES (?,?,?,?,?,?,?,?)""",
              (eid, title, machine, "WS", f"doc-{n}", f"dedup-{n}", "pending", customer))
    c.commit()
    c.close()
    return eid


def test_batch_gate_requires_ack_and_respects_all_vs_selected():
    err, ids = ts_table.review_batch_ids(False, True, [1], [9])
    assert ids == [] and "核对" in err
    err, ids = ts_table.review_batch_ids(True, False, [], [9])
    assert ids == [] and "勾选" in err
    err, ids = ts_table.review_batch_ids(True, False, [3, "4", 3, "x"], [])
    assert err == "" and ids == [3, 4]
    err, ids = ts_table.review_batch_ids(True, True, [1, 2], [9])
    assert err == "" and ids == [9]
    err, ids = ts_table.review_batch_ids("yes", False, "8", [])
    assert ids == [8]


def test_confirm_only_pending_and_publishes_cases(tsdb):
    keep_title = "待确认卡盘"
    pending = _insert(status="pending", title=keep_title)
    done = _insert(status="confirmed", title="已经确认")
    rejected = _insert(status="rejected", title="已经驳回")
    res = ts_table.confirm_entries("admin", [pending, pending, str(pending), done, rejected, 99999, "nope"])
    assert res["confirmed"] == [pending]
    assert res["skipped"] == [done, rejected]
    assert res["missing"] == [99999]
    c = ts_table._conn()
    row = c.execute("SELECT status, edited, has_new, title, edited_by FROM ts_entries WHERE id=?", (pending,)).fetchone()
    assert row["status"] == "confirmed" and row["edited"] == 1 and row["has_new"] == 0
    assert row["title"] == keep_title and row["edited_by"] == "admin"
    assert c.execute("SELECT review FROM ts_cases WHERE entry_id=?", (pending,)).fetchone()["review"] == "confirmed"
    assert c.execute("SELECT status FROM ts_entries WHERE id=?", (rejected,)).fetchone()["status"] == "rejected"
    c.close()
    again = ts_table.confirm_entries("admin", [pending])
    assert again["confirmed"] == [] and again["skipped"] == [pending]
    assert ts_table.confirm_entries("admin", [])["confirmed"] == []


def test_pending_hidden_until_confirmed_and_customer_still_applies(tsdb, monkeypatch):
    monkeypatch.setenv("SECURITY_ENABLED", "true")
    pending = _insert(status="pending", title="待确认卡盘")
    _insert(status="confirmed", title="已确认卡盘")
    _insert(status="rejected", title="已驳回卡盘")
    secret = _insert(status="confirmed", title="客户现场卡盘", customers="xingyue", customer="xingyue")
    field = _eng("cat:fault_history", "model:S207D")
    insider = _eng("cat:fault_history", "model:S207D", "cust:xingyue")
    titles = {e["title"] for e in ts_table.search(field, admin=False)}
    assert titles == {"已确认卡盘"}
    assert "客户现场卡盘" not in titles
    assert any(e["id"] == secret for e in ts_table.search(insider, admin=False))
    ts_table.confirm_entries("admin", [pending])
    titles = {e["title"] for e in ts_table.search(field, admin=False)}
    assert "待确认卡盘" in titles and "已驳回卡盘" not in titles
    text = ts_table.lookup_for_agent("卡盘", field)
    assert "待确认卡盘" in text and "已驳回卡盘" not in text and "客户现场卡盘" not in text
    assert "客户现场卡盘" in ts_table.lookup_for_agent("卡盘", insider)


def test_oem_pending_needs_manual_grant_after_confirm(tsdb, monkeypatch):
    monkeypatch.setenv("SECURITY_ENABLED", "true")
    eid = _insert(status="pending", title="原厂真空报警", origin="oem")
    manual = _eng("cat:oem_manual", "model:S207D")
    field = _eng("cat:fault_history", "model:S207D")
    assert ts_table.search(manual, admin=False) == []
    ts_table.confirm_entries("admin", [eid])
    assert any(e["id"] == eid for e in ts_table.search(manual, admin=False))
    assert ts_table.search(field, admin=False) == []


def test_pending_queue_is_oldest_first_and_filtered(tsdb):
    _insert(title="后到", updated_at="2026-10-02 08:00:00", machine="S207D")
    _insert(title="先到", updated_at="2026-10-01 08:00:00", machine="S207D")
    _insert(title="另一台", updated_at="2026-09-01 08:00:00", machine="S206D")
    _insert(title="已确认不在队列", status="confirmed", updated_at="2026-01-01 00:00:00")
    rows = ts_table.pending_queue(admin=True, machine="S207D")
    assert [e["title"] for e in rows] == ["先到", "后到"]


def test_manual_extract_uses_live_script_not_retired_unit(monkeypatch):
    monkeypatch.delenv("KB_EXTRACT_NOW_MINUTES", raising=False)
    seen = {}

    def which(name):
        return "/bin/systemd-run" if name == "systemd-run" else None

    def run(argv, capture_output=False, text=False):
        seen["argv"] = list(argv)

        class R:
            returncode = 0
            stderr = ""
            stdout = "Running"

        return R()

    info = kb_extract.start_manual_extract(run=run, spawn=lambda argv: (_ for _ in ()).throw(AssertionError("spawn")),
                                           which=which)
    blob = " ".join(info["argv"])
    assert info["ok"] is True and info["via"] == "systemd-run"
    assert info["unit"] == "nikon-kb-extract-manual"
    assert info["minutes"] == 90
    assert seen["argv"][0] == "systemd-run"
    assert "--unit" in seen["argv"] and "nikon-kb-extract-manual" in seen["argv"]
    assert "scripts/kb_extract.py" in blob and "--minutes" in seen["argv"] and "90" in seen["argv"]
    assert "nikon-ts-extract" not in blob and "ts_extract.py" not in blob
    assert os.path.isfile(seen["argv"][seen["argv"].index("--") + 2])


def test_manual_extract_without_systemd_spawns_same_script(monkeypatch):
    monkeypatch.setenv("KB_EXTRACT_NOW_MINUTES", "15")
    seen = {}

    def spawn(argv):
        seen["argv"] = list(argv)
        return "/tmp/nikon-kb-extract-manual.log"

    info = kb_extract.start_manual_extract(which=lambda name: None, spawn=spawn,
                                           run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("run")))
    assert info["ok"] is True and info["via"] == "process" and info["minutes"] == 15
    assert info["log"].endswith("nikon-kb-extract-manual.log")
    assert seen["argv"][-2:] == ["--minutes", "15"]
    assert seen["argv"][1].endswith(os.path.join("scripts", "kb_extract.py"))
    assert "nikon-ts-extract" not in " ".join(seen["argv"])


def test_manual_minutes_rejects_unbounded_runs():
    with pytest.raises(ValueError):
        kb_extract.manual_minutes(0)
    with pytest.raises(ValueError):
        kb_extract.manual_minutes(481)


def test_ui_wires_queue_and_live_extract():
    text = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "app.py"),
                encoding="utf-8").read()
    assert "nikon-ts-extract" not in text
    assert "start_manual_extract" in text
    assert "待确认队列" in text
    assert "确认勾选的条目" in text
    assert "我已核对" in text
    assert "驳回这条" in text
    assert "立即抽取新资料" in text
