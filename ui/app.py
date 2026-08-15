#!/usr/bin/env python3
"""
Nikon Expert — Gradio 工程师交互界面
支持两种模式：
  BACKEND_MODE=local  — 直接调用 src.engine（默认，单机模式）
  BACKEND_MODE=remote — 通过 HTTP 调用 FastAPI 服务（多用户服务器模式）
"""
import sys, os, json, html as _html
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import requests as _requests
import gradio as gr
from ui.client import BackendClient

# ── 全局 BackendClient ──────────────────────────────────────────
backend = BackendClient()

# ── 本地模式：预加载引擎 ─────────────────────────────────────────
if backend.is_local:
    print("⚙️  正在加载 Nikon Expert 引擎，请稍候...")
    backend.init_engine()
    print("✅ 引擎就绪！\n")
else:
    print(f"🌐 远程模式：连接 {os.getenv('API_BASE_URL', 'http://localhost:8000')}")


# ── 读取 PDF 渲染器模板（用于 serve-pdf 路由） ────────────────────
_RENDERER_HTML = ""
try:
    _renderer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_renderer_page.html")
    with open(_renderer_path, encoding="utf-8") as f:
        _RENDERER_HTML = f.read()
except Exception:
    pass


# ── 回调函数 ─────────────────────────────────────────────────────
def _resolve_login_user(request):
    """从 Gradio 登录态解析 acl.User。security 关→None(不过滤)。
    开启但解析不到有效用户→抛错(fail-closed，宁可拒绝不放行)。"""
    from src import acl
    if not acl.security_enabled():
        return None
    uid = getattr(request, "username", None) if request is not None else None
    if not uid:
        raise acl.PermissionDenied("未登录")
    from src.fulltext import _get_conn
    return acl.load_user(uid, _get_conn())


def chat(question: str, history: list, request: gr.Request = None):
    if not question.strip():
        yield (history or []), "", ""
        return
    from src.router import auto_mode
    from src import acl
    mode_key = auto_mode(question)
    history = history or []

    new_history = history + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": ""},
    ]
    yield new_history, "", ""

    # 绑定登录用户到 contextvar：本地模式下引擎深处（含 agent 工具）据此按 ACL 过滤
    try:
        _user = _resolve_login_user(request)
    except acl.PermissionDenied:
        new_history[-1]["content"] = "⛔ 未识别到登录身份，无法检索。请重新登录。"
        yield new_history, "", ""
        return
    _tok = acl.set_current_user(_user)

    accumulated = ""
    citations_data = []
    try:
        for delta, is_final, citations, cit_data, has_result in backend.query_stream(question, mode=mode_key, history=history):
            if is_final:
                if has_result and citations:
                    citations_data = cit_data
                    sources = "\n\n**📚 参考来源：**\n" + "\n".join(f"- {c}" for c in citations)
                    new_history[-1]["content"] = accumulated + sources
                else:
                    new_history[-1]["content"] = accumulated or delta
            else:
                accumulated += delta
                new_history[-1]["content"] = accumulated
            yield new_history, "", ""
    except acl.PermissionDenied:
        new_history[-1]["content"] = "⛔ 权限不足，无法检索到你有权访问的资料。"
        yield new_history, "", ""
        return
    finally:
        acl.reset_current_user(_tok)

    cit_html = _build_citation_buttons(citations_data)
    yield new_history, "", cit_html


def _build_citation_buttons(citations_data: list) -> str:
    if not citations_data:
        return ""
    items = []
    for c in citations_data:
        fp = c.get("file_path", "")
        page = c.get("page")
        cite_text = c.get("cite_text", "")
        if not cite_text:
            continue
        safe_text = _html.escape(cite_text)
        fp_low = fp.lower() if fp else ""
        if fp and fp_low.endswith(".pdf"):
            pdf_url = f"/serve-pdf?path={_html.escape(_requests.utils.quote(fp, safe=''))}"
            if page:
                pdf_url += f"#page={page}"
            items.append(
                f'<a class="cite-link" href="{pdf_url}" target="_blank">'
                f'{safe_text}</a>'
            )
        elif fp and fp_low.endswith((".md", ".markdown", ".txt")):
            md_url = f"/serve-md?path={_html.escape(_requests.utils.quote(fp, safe=''))}"
            items.append(
                f'<a class="cite-link" href="{md_url}" target="_blank">'
                f'{safe_text}</a>'
            )
        else:
            items.append(f'<span class="cite-text">{safe_text}</span>')
    if not items:
        return ""
    return (
        '<div class="citation-bar">'
        '<span class="cite-label">📖 参考来源：</span>'
        + "&nbsp;·&nbsp;".join(items) +
        '</div>'
    )


def get_db_status():
    s = backend.db_status()
    if s.get("status") == "ok":
        fts_info = f" | FTS：{s.get('fts_records', 0):,} 条" if s.get("fts_records") else ""
        device = s.get("device", "")
        device_info = f" | {device}" if device else ""
        return f"✅ 知识库正常 | 向量：{s['points']:,}{fts_info} | {s.get('collection', '')}{device_info}"
    return f"⚠️ 知识库异常：{s.get('status', 'unknown')}"


LOCAL_URL  = "http://localhost:11434"
REMOTE_URL = os.getenv("REMOTE_OLLAMA_URL", "http://192.168.168.208:11434")

# provider 标签 ↔ id 映射（"外接大脑"下拉）
from src.providers import list_providers as _list_providers, list_models as _list_models, provider_key as _provider_key
_PROVIDER_CHOICES = [label for _id, label in _list_providers()]
_LABEL2ID = {label: pid for pid, label in _list_providers()}


def do_switch_provider(provider_label: str):
    """切换 provider：刷新可选模型下拉。不立即切换 LLM，选好模型点按钮才切。"""
    pid = _LABEL2ID.get(provider_label)
    if not pid:
        return gr.update(), "⚠️ 未知 provider"
    models = _list_models(pid)
    tip = ""
    # key 缺失提醒
    from src.providers import get_provider
    prov = get_provider(pid)
    if prov.get("needs_key") and not _provider_key(pid):
        tip = f"⚠️ 该大脑需在 .env 设置 `{prov['key_env']}`"
    elif prov.get("use_proxy"):
        tip = "🌐 海外大脑，将走本地代理；数据会外传该 API"
    elif prov["kind"] != "ollama":
        tip = "☁️ 云端大脑：数据会外传该 API（embedding/检索仍本地）"
    return gr.update(choices=models, value=models[0] if models else None), tip


def do_apply_model(provider_label: str, model_name: str):
    """真正切换 Settings.llm 到所选 provider + 模型。"""
    pid = _LABEL2ID.get(provider_label)
    if not pid:
        return "⚠️ 请选择大脑"
    if not model_name:
        return "⚠️ 请选择或填写模型名"
    try:
        desc = backend.switch_provider(pid, model_name)
    except Exception as e:
        return f"⚠️ 切换失败：{e}"
    return f"✅ 已切换至 **{desc}**"


def do_ingest_file(file_path):
    if not file_path:
        return "⚠️ 请先选择文件"
    result = backend.ingest_file(file_path)
    if isinstance(result, dict):
        return result.get("message", str(result))
    return str(result)


def do_get_kb_docs():
    docs = backend.get_kb_documents()
    if not docs:
        return pd.DataFrame(columns=["选择", "文档名", "类型", "位置", "Chunks"])
    df = pd.DataFrame(docs)
    # 统一列名
    col_map = {}
    for c in df.columns:
        if c in ("doc_name", "文档名"):
            col_map[c] = "文档名"
        elif c in ("doc_type", "类型"):
            col_map[c] = "类型"
        elif c in ("location", "位置"):
            col_map[c] = "位置"
        elif c in ("chunks", "Chunks"):
            col_map[c] = "Chunks"
    if col_map:
        df = df.rename(columns=col_map)
    if "选择" not in df.columns:
        df.insert(0, "选择", False)
    return df


def do_delete_selected(df_data):
    if df_data is None or len(df_data) == 0:
        return "⚠️ 列表为空", do_get_kb_docs()
    df = df_data if isinstance(df_data, pd.DataFrame) else pd.DataFrame(df_data)
    selected = df[df["选择"] == True]["文档名"].tolist()
    if not selected:
        return "⚠️ 请先勾选要删除的文档", df
    for name in selected:
        backend.delete_document(name)
    preview = "、".join(selected[:3]) + ("..." if len(selected) > 3 else "")
    return f"✅ 已删除 {len(selected)} 个文档：{preview}", do_get_kb_docs()


def do_fts_index(dir_path: str, pattern: str):
    if not dir_path or not dir_path.strip():
        return "⚠️ 请先选择目录"
    dir_path = dir_path.strip()
    pattern = pattern.strip() or "*.md"
    if not os.path.isdir(dir_path):
        return f"⚠️ 目录不存在：{dir_path}"
    result = backend.fts_index(dir_path, pattern)
    if isinstance(result, dict):
        msg = f"✅ 索引完成：{result.get('indexed', 0)} 个文件，{result.get('records', 0)} 条记录"
        errs = result.get("errors", [])
        if errs:
            msg += f"\n⚠️ 错误 {len(errs)} 个：" + "；".join(errs[:3])
        return msg
    return str(result)


def do_okf_import(path: str):
    if not path or not path.strip():
        return "⚠️ 请填写 OKF 目录或 .md 文件路径"
    path = path.strip()
    if not os.path.exists(path):
        return f"⚠️ 路径不存在：{path}"
    try:
        result = backend.ingest_okf(path)
    except Exception as e:
        return f"⚠️ 导入失败：{e}"
    if isinstance(result, dict):
        return result.get("message", f"✅ 导入完成：{result.get('chunks', 0)} 个 Chunk")
    return str(result)


def do_fts_clear():
    result = backend.fts_clear()
    count = result.get("cleared", 0) if isinstance(result, dict) else 0
    return f"✅ 已清空全文索引（{count} 条记录）"


def do_get_fts_paths():
    paths = backend.fts_paths()
    if not paths:
        return pd.DataFrame(columns=["路径", "类型"])
    return pd.DataFrame(paths)


# ── 界面布局 ─────────────────────────────────────────────────────
CITATION_CSS = """
.citation-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    padding: 8px 4px;
    border-top: 1px solid #e0e0e0;
}
.cite-label {
    font-size: 12px;
    color: #888;
    flex-shrink: 0;
}
.cite-link {
    font-size: 12px;
    color: #2a6cb6;
    text-decoration: none;
    padding: 2px 8px;
    border: 1px solid #c5ddf0;
    border-radius: 10px;
    background: #f0f6ff;
    white-space: nowrap;
}
.cite-link:hover {
    background: #dbe8f8;
    border-color: #4a90d9;
}
.cite-text {
    font-size: 12px;
    color: #666;
}
"""

# ══ 新界面资源（Claude 风格主题 · 侧栏 · 图标）══════════════════════
from pathlib import Path as _Path
from nikon_theme import nikon_theme
_UI_DIR = _Path(__file__).parent
_NK_CSS = (_UI_DIR / "nikon_expert.css").read_text(encoding="utf-8")
_FAVICON = str(_UI_DIR / "ne_kaushan.svg")
_LOGO_SVG = (_UI_DIR / "ne_kaushan.svg").read_text(encoding="utf-8").replace(
    'width="64" height="64"', 'class="nk-logo" width="26" height="26"')

_SIDE_TOP = (f'<div id="nk-side" style="padding:14px 12px 0;"><div class="nk-side-top">'
             f'{_LOGO_SVG}<span class="nk-name">Nikon Expert</span></div></div>')
_SIDE_RECENT = ('<div id="nk-side" style="padding:6px 12px 0;"><div class="nk-recent">'
                '<div class="nk-recent-head">最近</div>'
                '<a class="nk-recent-item">S207 对准误差排查</a>'
                '<a class="nk-recent-item">E-5301 报警排查</a>'
                '<a class="nk-recent-item">传送 SMIF 确认</a>'
                '<a class="nk-recent-item">GOCS 说明</a></div></div>')
_TOOLS = ('<div class="nk-tools">'
          '<span class="nk-pill">知识库检索 <code>nikon_query</code></span>'
          '<span class="nk-pill">错误码 <code>search_error_code</code></span>'
          '<span class="nk-pill">WPS 实时 <code>ask_wps</code></span>'
          '<span class="nk-pill">工作日志 <code>query_worklog</code></span></div>')


def _account_html(name="工程师", role="已登录"):
    initial = _html.escape((name or "工")[0])
    return (f'<div id="nk-side" style="padding:0 12px 12px;"><div class="nk-account">'
            f'<div class="nk-avatar">{initial}</div><div class="nk-acc-info">'
            f'<span class="nk-acc-name">{_html.escape(name)}</span>'
            f'<span class="nk-acc-role">{_html.escape(role)}</span></div></div></div>')


def _maintop_html():
    try:
        s = backend.db_status()
        pts = s.get("points", 0) if isinstance(s, dict) else 0
        ok = isinstance(s, dict) and s.get("status") == "ok"
    except Exception:
        pts, ok = 0, False
    try:
        model = backend.get_current_model()
    except Exception:
        model = os.getenv("LLM_MODEL", "")
    return (f'<div id="nk-maintop" style="display:flex;justify-content:space-between;'
            f'align-items:center;padding:14px 4px 4px;">'
            f'<span class="nk-tagline">光刻机设备知识库 · 按权限可见</span>'
            f'<span class="nk-status" style="font-family:var(--nk-mono);font-size:.68rem;'
            f'color:var(--nk-ink-faint);display:inline-flex;align-items:center;gap:6px;">'
            f'<span class="dot" style="width:6px;height:6px;border-radius:50%;'
            f'background:{"#6FAE8E" if ok else "#C0705C"};"></span>'
            f'{_html.escape(str(model))} · {pts:,} vectors · {"online" if ok else "offline"}</span></div>')


_VIEWS = ["chat", "kb", "worklog", "err"]


def _switch_view(target):
    vis = [gr.update(visible=(v == target)) for v in _VIEWS]
    btns = [gr.update(elem_classes=["nk-navbtn", "nk-navbtn-active"] if v == target
                      else ["nk-navbtn"]) for v in _VIEWS]
    return vis + btns


def do_ingest_files(paths):
    """异步上传：文件入队即返回，后台单线程串行摄入（不阻塞界面/查询）。
    PDF 在 CPU 上较慢也没关系——用户不用干等，任务面板显示进度，完成自动进列表。"""
    from src import ingest_queue
    if not paths:
        return "⚠️ 请先选择文件"
    if not isinstance(paths, list):
        paths = [paths]
    n = 0
    for p in paths:
        fp = p if isinstance(p, str) else getattr(p, "name", None)
        if fp:
            ingest_queue.enqueue(fp, os.path.basename(fp))
            n += 1
    if not n:
        return "⚠️ 未取到文件路径"
    return (f"✅ 已加入队列（{n} 个），后台处理中——**不用等，可继续用**。"
            "完成后会自动出现在下方「向量库文档管理」列表（大 PDF 可能要几分钟）。")


_JOB_ICON = {"queued": "⏳ 排队", "processing": "⚙️ 处理中", "done": "✅ 完成", "failed": "❌ 失败"}


def _fmt_elapsed(sec):
    sec = int(sec)
    return f"{sec//60}m{sec%60:02d}s" if sec >= 60 else f"{sec}s"


def _jobs_html():
    import time as _t
    from src import ingest_queue
    jobs = ingest_queue.recent_jobs()
    if not jobs:
        return ""
    rows = []
    for j in jobs:
        label = _JOB_ICON.get(j["status"], j["status"])
        if j["status"] == "processing" and j.get("ts_start"):
            label = f'⚙️ 处理中 · 已 {_fmt_elapsed(_t.time() - j["ts_start"])}'
        elif j["status"] == "failed" and j.get("message"):
            label = f'❌ 失败：{_html.escape(j["message"][:40])}'
        rows.append(
            f'<div class="nk-doc-row"><span class="nk-doc-title">{_html.escape(j["name"])}</span>'
            f'<span class="nk-doc-meta">{label}</span></div>')
    act = ingest_queue.active_count()
    head = (f'<div class="nk-sec">上传任务（{act} 个进行中 · CPU 嵌向量，大文件数分钟属正常）</div>'
            if act else '<div class="nk-sec">上传任务</div>')
    return head + f'<div class="nk-view" style="margin:0;">{"".join(rows)}</div>'


def _tick_jobs():
    """定时轮询：刷新任务面板；有任务刚完成则同时刷新文档表。"""
    from src import ingest_queue
    html = _jobs_html()
    if ingest_queue.take_dirty():
        return html, do_get_kb_docs()
    return html, gr.update()


def do_error_lookup(code, request: gr.Request = None):
    code = (code or "").strip()
    if not code:
        return "输入一个错误码，如 `E-5301`。"
    from src import acl
    _tok = None
    try:
        _tok = acl.set_current_user(_resolve_login_user(request))
    except acl.PermissionDenied:
        return "⛔ 未登录或无权限。"
    except Exception:
        pass
    try:
        from src.fulltext import search_fts_exact
        rows = search_fts_exact(code, limit=8)
    except acl.PermissionDenied:
        return "⛔ 无权访问相关资料。"
    except Exception as e:
        return f"查询失败：{e}"
    finally:
        if _tok is not None:
            acl.reset_current_user(_tok)
    if not rows:
        return f"未找到 **{code}** 的记录（可能未收录该错误码手册）。"
    out = [f"**{code}** 命中 {len(rows)} 条：\n"]
    for r in rows:
        out.append(f"- 《{r.get('doc_name','')}》{r.get('section_title','')} — "
                   f"{(r.get('text','') or '')[:180]}…")
    return "\n".join(out)


def do_worklog_recent(days):
    try:
        days = max(1, int(days or 7))
    except Exception:
        days = 7
    import sys as _sys
    _sp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if _sp not in _sys.path:
        _sys.path.insert(0, _sp)
    try:
        load_dotenv(os.path.join(os.path.dirname(_sp), ".feishu_env"), override=False)
    except Exception:
        pass
    url = os.getenv("FEISHU_WORKLOG_URL", "").strip()
    if not url:
        return "未配置 FEISHU_WORKLOG_URL。"
    try:
        import feishu_client
        wl = feishu_client.fetch_worklog(url)
    except Exception as e:
        return f"读取飞书日志失败：{e}\n（若 token 过期，请在本机重跑 feishu_auth.py login）"
    ds = wl.get("days") or []
    if not ds:
        return "飞书日志没解析出按日期的日报。"
    sel = ds[:days]
    head = (f"**{wl.get('title') or '飞书工作日志'}** · 最近 {len(sel)} 天\n\n"
            f"可用范围 {ds[-1]['date']} … {ds[0]['date']}")
    return head + "\n\n---\n\n" + "\n\n---\n\n".join(d["text"] for d in sel)


with gr.Blocks(title="Nikon Expert") as demo:
    with gr.Sidebar(elem_id="nk-side-wrap", width=280, open=True):
        gr.HTML(_SIDE_TOP)
        new_btn  = gr.Button("＋ 新建对话", elem_classes=["nk-newbtn"])
        nav_chat = gr.Button("💬 对话", elem_classes=["nk-navbtn", "nk-navbtn-active"])
        nav_kb   = gr.Button("📖 知识库", elem_classes=["nk-navbtn"])
        nav_log  = gr.Button("📋 工作日志", elem_classes=["nk-navbtn"])
        nav_err  = gr.Button("⚠ 错误码速查", elem_classes=["nk-navbtn"])
        gr.HTML(_SIDE_RECENT)
        gr.HTML(_account_html())

    gr.HTML(_maintop_html())

    # ── 视图：对话 ──
    with gr.Column(visible=True) as view_chat:
        chatbot = gr.Chatbot(height=460, show_label=False, elem_classes="chatbot",
                             value=[{"role": "assistant",
                                     "content": "机台知识库已就绪。问我错误码、对准、传送方式或工作日志。"}])
        with gr.Row(elem_id="nk-inputrow"):
            question = gr.Textbox(placeholder="给 Nikon Expert 发消息…", show_label=False,
                                  lines=1, max_lines=6, scale=12, container=False)
            send_btn = gr.Button("↑", variant="primary", scale=1, elem_id="nk-send")
        citation_buttons = gr.HTML("", elem_id="citation_buttons")
        gr.HTML(_TOOLS)
        with gr.Accordion("⚙️ 设置 · 切换大脑", open=False):
            with gr.Row():
                provider_dropdown = gr.Dropdown(choices=_PROVIDER_CHOICES,
                    value=_PROVIDER_CHOICES[0], label="🧠 大脑（provider）", interactive=True)
                model_dropdown = gr.Dropdown(choices=backend.get_ollama_models(),
                    value=backend.get_current_model(), label="🤖 模型",
                    allow_custom_value=True, interactive=True)
            switch_btn = gr.Button("切换大脑", size="sm")
            switch_status = gr.Markdown("")

    # ── 视图：知识库（真组件）──
    with gr.Column(visible=False, elem_id="nk-kb") as view_kb:
        gr.HTML('<div class="nk-view" style="margin-bottom:0;"><h2>知识库</h2>'
                '<div class="nk-view-sub">手册与经验笔记 · 上传即定级即可查</div></div>')
        gr.HTML('<div class="nk-sec first">上传文件到向量知识库</div>')
        kb_files = gr.File(label="选择文件（PDF / Markdown）", file_count="multiple",
                           file_types=[".pdf", ".md"], type="filepath")
        kb_upload_btn = gr.Button("上传并向量化", variant="primary")
        ingest_status = gr.Markdown("")
        jobs_html = gr.HTML("")
        jobs_timer = gr.Timer(3)   # 后台任务进度轮询

        gr.HTML('<div class="nk-sec">导入 OKF 知识库</div>')
        with gr.Row():
            okf_dir_input = gr.Textbox(placeholder="/path/to/okf_bundle 或 concept.md",
                                       show_label=False, scale=8, container=False)
            okf_import_btn = gr.Button("导入 OKF", scale=2)
        okf_status_md = gr.Markdown("")

        gr.HTML('<div class="nk-sec">全文搜索索引</div>')
        with gr.Row():
            fts_dir_input = gr.Textbox(placeholder="/path/to/your/notes",
                                       show_label=False, scale=7, container=False)
            fts_pattern = gr.Dropdown(["*.md", "*.txt", "*.md;*.txt", "*.pdf"], value="*.md",
                                      show_label=False, scale=3, container=False)
        with gr.Row():
            fts_index_btn = gr.Button("开始索引", variant="primary")
            fts_clear_btn = gr.Button("清空索引", elem_classes=["nk-danger"])
        fts_status_md = gr.Markdown("")
        gr.HTML('<div class="nk-sec">已索引目录</div>')
        fts_paths_table = gr.Dataframe(value=do_get_fts_paths, headers=["路径", "类型"],
                                       interactive=False, wrap=True)

        gr.HTML('<div class="nk-sec">向量库文档管理</div>')
        with gr.Row():
            refresh_kb_btn = gr.Button("刷新列表")
            del_btn = gr.Button("删除选中", elem_classes=["nk-danger"])
        kb_table = gr.Dataframe(value=do_get_kb_docs,
                                headers=["选择", "文档名", "类型", "位置", "Chunks"],
                                datatype=["bool", "str", "str", "str", "number"],
                                interactive=True, wrap=True)
        del_status = gr.Markdown("")

    # ── 视图：工作日志（飞书实时）──
    with gr.Column(visible=False) as view_log:
        gr.HTML('<div class="nk-view" style="margin-bottom:0;"><h2>工作日志</h2>'
                '<div class="nk-view-sub">实时拉取飞书 · nikon_query_worklog</div></div>')
        with gr.Row():
            wl_days = gr.Number(value=7, label="最近天数", precision=0, scale=2)
            wl_btn = gr.Button("读取日志", variant="primary", scale=2)
        wl_out = gr.Markdown("")

    # ── 视图：错误码速查 ──
    with gr.Column(visible=False) as view_err:
        gr.HTML('<div class="nk-view" style="margin-bottom:0;"><h2>错误码速查</h2>'
                '<div class="nk-view-sub">精确匹配 · nikon_search_error_code</div></div>')
        with gr.Row():
            err_code = gr.Textbox(placeholder="E-5301", show_label=False, scale=8, container=False)
            err_btn = gr.Button("查询", variant="primary", scale=2)
        err_out = gr.Markdown("")

    # ── 事件 ──
    _views = [view_chat, view_kb, view_log, view_err]
    _navs = [nav_chat, nav_kb, nav_log, nav_err]
    _nav_out = _views + _navs
    nav_chat.click(lambda: _switch_view("chat"), None, _nav_out)
    nav_kb.click(lambda: _switch_view("kb"), None, _nav_out)
    nav_log.click(lambda: _switch_view("worklog"), None, _nav_out)
    nav_err.click(lambda: _switch_view("err"), None, _nav_out)
    new_btn.click(lambda: _switch_view("chat") + [[], "", ""],
                  None, _nav_out + [chatbot, question, citation_buttons])

    send_btn.click(chat, [question, chatbot], [chatbot, question, citation_buttons])
    question.submit(chat, [question, chatbot], [chatbot, question, citation_buttons])
    provider_dropdown.change(do_switch_provider, [provider_dropdown], [model_dropdown, switch_status])
    switch_btn.click(do_apply_model, [provider_dropdown, model_dropdown], [switch_status])

    kb_upload_btn.click(do_ingest_files, [kb_files], [ingest_status])
    jobs_timer.tick(_tick_jobs, None, [jobs_html, kb_table])
    okf_import_btn.click(do_okf_import, [okf_dir_input], [okf_status_md])
    fts_index_btn.click(do_fts_index, [fts_dir_input, fts_pattern], [fts_status_md])
    fts_clear_btn.click(do_fts_clear, None, [fts_status_md])
    refresh_kb_btn.click(do_get_kb_docs, None, [kb_table])
    del_btn.click(do_delete_selected, [kb_table], [del_status, kb_table])

    wl_btn.click(do_worklog_recent, [wl_days], [wl_out])
    err_btn.click(do_error_lookup, [err_code], [err_out])
    err_code.submit(do_error_lookup, [err_code], [err_out])


# ── 注册 FastAPI 路由（本地模式需要 PDF 服务，远程模式由 API 服务提供） ──
import pathlib as _pl

def _register_routes(fastapi_app):
    from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse

    @fastapi_app.get("/serve-pdf")
    async def serve_pdf(path: str):
        try:
            resolved = _pl.Path(path).resolve()
        except Exception:
            return PlainTextResponse("Invalid path", status_code=400)
        if not resolved.exists():
            return PlainTextResponse("File not found", status_code=404)
        if not resolved.is_file():
            return PlainTextResponse("Not a file", status_code=400)
        if resolved.suffix.lower() != ".pdf":
            return PlainTextResponse("Only PDF files allowed", status_code=400)
        return FileResponse(str(resolved), media_type="application/pdf")

    @fastapi_app.get("/serve-md")
    async def serve_md(path: str):
        """渲染 OKF / Markdown 源文件：frontmatter 元数据卡片 + 正文 HTML。"""
        try:
            resolved = _pl.Path(path).resolve()
        except Exception:
            return PlainTextResponse("Invalid path", status_code=400)
        if not resolved.exists() or not resolved.is_file():
            return PlainTextResponse("File not found", status_code=404)
        if resolved.suffix.lower() not in (".md", ".markdown", ".txt"):
            return PlainTextResponse("Only markdown/text files allowed", status_code=400)

        import markdown as _md
        from src.ingestor import _parse_okf

        raw = resolved.read_text(encoding="utf-8", errors="replace")
        fm, body = _parse_okf(raw)
        body_html = _md.markdown(body, extensions=["fenced_code", "tables", "sane_lists"])

        # frontmatter → 元数据卡片
        meta_rows = ""
        label = {"type": "类型", "title": "标题", "description": "说明",
                 "tags": "标签", "timestamp": "时间", "machine_model": "机型",
                 "error_codes": "报警码", "resource": "来源"}
        for k in ["type", "title", "machine_model", "error_codes", "tags", "timestamp", "description", "resource"]:
            if k in fm and fm[k]:
                v = fm[k]
                if isinstance(v, (list, tuple)):
                    v = "、".join(str(x) for x in v)
                meta_rows += (f'<tr><td class="k">{label.get(k, k)}</td>'
                              f'<td class="v">{_html.escape(str(v))}</td></tr>')
        meta_card = f'<table class="meta">{meta_rows}</table>' if meta_rows else ""

        page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(str(fm.get('title') or resolved.stem))}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 860px; margin: 0 auto; padding: 32px 24px; line-height: 1.7; color: #1a1a1a; }}
  h1,h2,h3 {{ line-height: 1.3; }} h1 {{ font-size: 1.6rem; border-bottom: 2px solid #eee; padding-bottom: .3em; }}
  .meta {{ border-collapse: collapse; margin: 0 0 24px; width: 100%; background: #f7f8fa;
           border-radius: 8px; overflow: hidden; }}
  .meta td {{ padding: 8px 14px; border-bottom: 1px solid #eaecef; font-size: .92rem; }}
  .meta td.k {{ color: #666; white-space: nowrap; width: 88px; font-weight: 600; }}
  .filepath {{ color: #999; font-size: .8rem; margin-bottom: 18px; word-break: break-all; }}
  code {{ background: #f2f4f7; padding: 2px 5px; border-radius: 4px; }}
  pre {{ background: #f7f8fa; padding: 14px; border-radius: 8px; overflow-x: auto; }}
  table:not(.meta) {{ border-collapse: collapse; }} table:not(.meta) td, table:not(.meta) th {{ border: 1px solid #ddd; padding: 6px 10px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0d1117; color: #e6edf3; }}
    .meta {{ background: #161b22; }} .meta td {{ border-color: #30363d; }} .meta td.k {{ color: #8b949e; }}
    h1 {{ border-color: #30363d; }} code, pre {{ background: #161b22; }}
  }}
</style></head><body>
<div class="filepath">📗 {_html.escape(str(resolved))}</div>
{meta_card}
{body_html}
</body></html>"""
        return HTMLResponse(page)


if __name__ == "__main__":
    import threading

    import time
    port = int(os.getenv("UI_PORT", "7860"))

    # 绑定地址：UI_HOST 优先（云端部署建议绑 Tailscale IP，只在内网可达、不暴露公网）；
    # 否则本地模式 127.0.0.1、远程模式 0.0.0.0。
    host = os.getenv("UI_HOST") or ("127.0.0.1" if backend.is_local else "0.0.0.0")
    # headless（服务器无界面）：显式 HEADLESS、或绑了非 127 地址、或远程模式 → 不开 pywebview
    headless = bool(os.getenv("HEADLESS")) or host != "127.0.0.1" or (not backend.is_local)
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{host}:{port}"

    launch_kwargs = {"server_name": host, "server_port": port, "share": False,
                     "prevent_thread_lock": True, "theme": nikon_theme,
                     "css": _NK_CSS + "\n" + CITATION_CSS, "favicon_path": _FAVICON}
    # 登录：security 开启 → 逐人校验 users 表(per-user 身份，驱动 ACL)；
    #       否则回退单一共享账号 GRADIO_AUTH(向后兼容)。
    from src import acl
    if acl.security_enabled():
        def _multi_user_auth(username, password):
            try:
                from src.fulltext import _get_conn
                return acl.verify_login(username, password, _get_conn())
            except Exception:
                return False
        launch_kwargs["auth"] = _multi_user_auth
        launch_kwargs["auth_message"] = "Nikon Expert — 请用个人账号登录（资料按权限可见）"
        print("🔐 登录模式：per-user（users 表校验，ACL 生效）")
    else:
        _auth = os.getenv("GRADIO_AUTH", "")   # 形如 user:pass
        if _auth and ":" in _auth:
            u, p = _auth.split(":", 1)
            launch_kwargs["auth"] = (u, p)

    demo.queue()
    server_thread = threading.Thread(target=demo.launch, kwargs=launch_kwargs, daemon=True)
    server_thread.start()

    # 等待 Gradio 服务就绪
    for _ in range(40):
        try:
            _requests.get(f"http://{probe_host}:{port}", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    # 本地模式：注册 PDF/图片/markdown 服务路由
    if backend.is_local:
        _register_routes(demo.app)

    if headless:
        print(f"🌐 运行中（headless）：{url}" + ("  [已启用登录]" if launch_kwargs.get("auth") else ""))
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        try:
            import webview
            webview.create_window(
                "Nikon Expert",
                url,
                width=1280,
                height=820,
                min_size=(900, 600),
            )
            webview.start()
        except ImportError:
            print("pywebview 未安装，请在浏览器中访问 " + url)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
