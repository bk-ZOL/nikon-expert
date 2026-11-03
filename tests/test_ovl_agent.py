"""OVL 接进智能体：会话里的 MESR、摘要、路由。不依赖 ovl-analyzer 本体。

Run: python3 -m pytest tests/test_ovl_agent.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import ovl_view
from src.router import is_ovl_query, should_use_agent


@pytest.fixture(autouse=True)
def _clean_uploads():
    ovl_view.clear_uploads()
    yield
    ovl_view.clear_uploads()


class _Img:
    def save(self, path, format=None):
        from pathlib import Path
        Path(path).write_bytes(b"\x89PNG")


class _DF:
    columns = ["参数", "数值", "单位", "层级"]

    def itertuples(self, index=False):
        assert index is False
        yield ("Tx", 0.12, "μm", "晶圆级")
        yield ("tx", -0.01, "μm", "Shot 内")


def _touch(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"mesr")
    return str(p)


def test_remember_is_per_user_and_latest_wins(tmp_path):
    a = _touch(tmp_path, "lotA.MESR")
    b = _touch(tmp_path, "lotB.MESR;5")
    ovl_view.remember_uploads("u1", [a])
    ovl_view.remember_uploads("u2", [b])
    assert ovl_view.pending_names("u1") == ["lotA.MESR"]
    assert ovl_view.pending_names("u2") == ["lotB.MESR;5"]
    # 再传一次 A，变成最近一份
    ovl_view.remember_uploads("u1", [a, b])
    assert ovl_view.pending_names("u1")[-1] == "lotB.MESR;5"
    # 空上传不清掉已记住的
    ovl_view.remember_uploads("u1", [])
    assert "lotA.MESR" in ovl_view.pending_names("u1")


def test_resolve_filename_wafer_and_reject_arbitrary_path(tmp_path):
    path = _touch(tmp_path, "lotA.MESR")
    ovl_view.remember_uploads("u1", [path])
    got, wafer, err = ovl_view.resolve_remembered("lotA wafer 2", key="u1")
    assert err == ""
    assert got == os.path.abspath(path)
    assert wafer == 2
    got, wafer, err = ovl_view.resolve_remembered("", key="u1")
    assert got == os.path.abspath(path) and wafer is None and err == ""
    _got, _wafer, err = ovl_view.resolve_remembered("/etc/passwd", key="u1")
    assert "不能按磁盘路径" in err
    _got, _wafer, err = ovl_view.resolve_remembered("", key="nobody")
    assert "还没有 MESR" in err


def test_missing_analyzer_is_a_message_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("OVL_ANALYZER_PATH", raising=False)
    monkeypatch.setattr(ovl_view, "_loaded", False)
    path = _touch(tmp_path, "lotA.MESR")
    ovl_view.remember_uploads("u1", [path])
    out = ovl_view.analyze_remembered("lotA.MESR", key="u1")
    assert "ovl-analyzer" in out["text"]
    assert out["note"] == out["text"]


def test_summary_and_plot_link(tmp_path, monkeypatch):
    monkeypatch.setenv("OVL_ARTIFACT_DIR", str(tmp_path / "arts"))
    monkeypatch.setattr(ovl_view, "_load", lambda: None)

    def fake_show(path, scale_factor=1.0, with_table=True):
        assert with_table is False
        return {"info": "**lotA.MESR**　类型 OVL　叠对", "image": _Img(), "can_fit": True,
                "table": None, "header": "", "raw": ""}

    def fake_ten(path, wafer, scale=1.0):
        assert wafer == 3
        return {"params": _DF(), "stats": "残差还剩 0.01", "image": _Img()}

    monkeypatch.setattr(ovl_view, "show", fake_show)
    monkeypatch.setattr(ovl_view, "wafers", lambda path: [1, 3])
    monkeypatch.setattr(ovl_view, "ten_param", fake_ten)
    path = _touch(tmp_path, "lotA.MESR")
    ovl_view.remember_uploads("u1", [path])
    out = ovl_view.analyze_remembered("wafer 3", key="u1")
    assert "Tx" in out["text"] and "0.12" in out["text"]
    assert "Wafer 3" in out["text"]
    assert "不入库" in out["text"]
    assert "/serve-ovl?f=" in out["text"]
    assert "残差还剩" in out["note"]
    assert "![" in out["note"]
    name = out["text"].split("/serve-ovl?f=", 1)[1].split()[0]
    saved = ovl_view.artifact_file(name)
    assert saved is not None and saved.is_file()
    assert ovl_view.artifact_file("../" + name) is None
    assert ovl_view.artifact_file("etc/passwd") is None


def test_non_vector_skips_ten_param(tmp_path, monkeypatch):
    monkeypatch.setenv("OVL_ARTIFACT_DIR", str(tmp_path / "arts"))
    monkeypatch.setattr(ovl_view, "_load", lambda: None)
    monkeypatch.setattr(ovl_view, "show", lambda *a, **k: {
        "info": "**curve.dat**　类型 FOCUS", "image": _Img(), "can_fit": False,
    })

    def _no(*a, **k):
        raise AssertionError("非叠对不应做 10 参数")

    monkeypatch.setattr(ovl_view, "ten_param", _no)
    path = _touch(tmp_path, "curve.dat")
    ovl_view.remember_uploads("u1", [path])
    out = ovl_view.analyze_remembered(key="u1")
    assert "不是叠对" in out["text"]
    assert "测量曲线" in out["note"]


def test_page_entry_still_errors_clearly_without_package(monkeypatch):
    monkeypatch.delenv("OVL_ANALYZER_PATH", raising=False)
    monkeypatch.setattr(ovl_view, "_loaded", False)
    with pytest.raises(RuntimeError, match="OVL_ANALYZER_PATH"):
        ovl_view.show("/tmp/does-not-need-to-exist.MESR", 1.0)


def test_router_sends_mesr_to_agent_but_not_plain_overlay_sop(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ROUTER", "keyword")
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_ALWAYS", "false")
    monkeypatch.setenv("SECURITY_ENABLED", "false")
    assert is_ovl_query("帮我看这份 MESR 的 10 参数")
    assert is_ovl_query("OVL分析一下叠对")
    assert should_use_agent("帮我看这份 MESR 的 10 参数") is True
    assert is_ovl_query("overlay baseline 校正") is False
    assert should_use_agent("什么是 overlay") is False
    monkeypatch.setenv("AGENT_ENABLED", "false")
    assert should_use_agent("分析 MESR") is False
    monkeypatch.setenv("AGENT_ENABLED", "true")

    path = _touch(tmp_path, "lotA.MESR")
    ovl_view.remember_uploads("shared", [path])
    assert is_ovl_query("overlay baseline 校正") is True
    hist = [{"role": "user", "content": "分析这份 MESR"}, {"role": "assistant", "content": "好"}]
    ovl_view.clear_uploads()
    assert is_ovl_query("wafer 2", history=hist) is True
    assert is_ovl_query("wafer 2") is False


def test_build_tools_calls_ovl_analyze(tmp_path, monkeypatch):
    pytest.importorskip("llama_index.core.tools")
    pytest.importorskip("dotenv")
    monkeypatch.setenv("SECURITY_ENABLED", "false")
    from src.tools import SourceCollector, build_tools

    path = _touch(tmp_path, "lotA.MESR")
    ovl_view.remember_uploads("shared", [path])

    def fake(which="", key=None):
        assert "wafer" in (which or "")
        return {"text": "Tx=0.12", "note": "图在这里"}

    monkeypatch.setattr(ovl_view, "analyze_remembered", fake)
    col = SourceCollector()
    tools = build_tools(col, question="分析这份 MESR")
    names = [t.metadata.name for t in tools]
    assert "ovl_analyze" in names
    tool = next(t for t in tools if t.metadata.name == "ovl_analyze")
    assert "lotA.MESR" in (tool.metadata.description or "")
    assert tool.fn(which="wafer 2") == "Tx=0.12"
    assert col.notes() and "图在这里" in col.notes()[0]
    assert "未入库" in col.notes()[0]


def test_tool_is_registered_in_source():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools = open(os.path.join(root, "src", "tools.py"), encoding="utf-8").read()
    fns = tools.split("fns = [", 1)[1].split("]", 1)[0]
    assert "def ovl_analyze" in tools
    assert "ovl_analyze" in fns
    agent = open(os.path.join(root, "src", "agent.py"), encoding="utf-8").read()
    assert "ovl_analyze" in agent
    engine = open(os.path.join(root, "src", "engine.py"), encoding="utf-8").read()
    assert '"ovl_analyze"' in engine
    assert 'final.get("notes")' in engine
    mcp = open(os.path.join(root, "mcp_tools_extra.py"), encoding="utf-8").read()
    assert "def nikon_analyze_ovl" in mcp
    app = open(os.path.join(root, "ui", "app.py"), encoding="utf-8").read()
    assert "附上 MESR" in app
    assert "def ovl_show" in app and "ovl_view.show" in app
    assert "/serve-ovl" in app
