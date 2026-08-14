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

with gr.Blocks(title="Nikon Expert") as demo:

    gr.HTML("""
    <div class="header">
        <h1>🔬 Nikon Expert</h1>
        <p style="color:#666">光刻机设备知识库 · 纯本地运行 · 零数据外传</p>
    </div>
    """)

    with gr.Tabs():

        # ── Tab 1：对话（左右分栏） ─────────────────────────────
        with gr.Tab("💬 对话"):
            with gr.Row():
                status_text = gr.Markdown(get_db_status(), elem_classes=["status-bar"])

            gr.Markdown("---")

            with gr.Row():
                with gr.Column(scale=1):
                    pass

                with gr.Column(scale=2):
                    chatbot = gr.Chatbot(label="对话记录", height=500)
                    citation_buttons = gr.HTML("", elem_id="citation_buttons")
                    with gr.Row():
                        question = gr.Textbox(
                            label="输入问题",
                            placeholder="请描述故障现象、Error Code，或直接提问...",
                            lines=2,
                            scale=4,
                        )
                        send_btn = gr.Button("发送", variant="primary", scale=1)
                    with gr.Row():
                        clear_btn = gr.Button("清空对话", size="sm")
                        refresh_btn = gr.Button("刷新状态", size="sm")

            with gr.Accordion("⚙️ 设置", open=False):
                with gr.Row():
                    with gr.Column(scale=1):
                        provider_dropdown = gr.Dropdown(
                            choices=_PROVIDER_CHOICES,
                            value=_PROVIDER_CHOICES[0],
                            label="🧠 大脑（provider）",
                            interactive=True,
                        )
                        model_dropdown = gr.Dropdown(
                            choices=backend.get_ollama_models(),
                            value=backend.get_current_model(),
                            label="🤖 模型（可下拉选或手填）",
                            allow_custom_value=True,
                            interactive=True,
                        )
                    with gr.Column(scale=1):
                        switch_btn = gr.Button("切换大脑", size="sm")
                        switch_status = gr.Markdown("")
                        gr.Markdown("**示例问题：**")
                        gr.Markdown("""
- Nikon 对焦系统工作原理
- E-5301 报警如何排查？
- Alignment 失败的常见原因
- PM 检查 Checksheet 第5项要求
                        """)

        # ── Tab 2：知识库 ─────────────────────────────────────────
        with gr.Tab("📂 知识库"):

            gr.Markdown("### 上传文件到向量知识库")
            with gr.Row():
                upload_file = gr.File(
                    label="选择文件（PDF / Markdown）",
                    file_types=[".pdf", ".md"],
                    type="filepath",
                )
                ingest_btn = gr.Button("上传并向量化", variant="primary", scale=1)
                ingest_status = gr.Markdown("")

            gr.Markdown("---")

            gr.Markdown("### 导入 OKF 知识库（Open Knowledge Format）")
            gr.Markdown("_Google OKF 标准：每个概念一个 `.md`（YAML frontmatter + 正文）。"
                        "填 OKF 目录或单个 `.md` 的完整路径，一键导入向量库 + 全文索引。_")
            with gr.Row():
                okf_dir_input = gr.Textbox(
                    label="OKF 目录 / 文件路径",
                    placeholder="/path/to/okf_bundle 或 /path/to/concept.md",
                    scale=3,
                )
                okf_import_btn = gr.Button("📗 导入 OKF", variant="primary", scale=1)
            okf_status_md = gr.Markdown("")

            gr.Markdown("---")

            gr.Markdown("### 全文搜索索引")
            gr.Markdown("_输入笔记目录的完整路径，如 `/Users/yourname/Documents/Notes`_")
            with gr.Row():
                fts_dir_input = gr.Textbox(
                    label="目录路径",
                    placeholder="/path/to/your/notes",
                    scale=3,
                )
                fts_pattern = gr.Dropdown(
                    choices=["*.md", "*.txt", "*.md;*.txt", "*.pdf", "*.md;*.txt;*.pdf"],
                    value="*.md",
                    label="文件类型",
                    scale=1,
                )
            with gr.Row():
                fts_index_btn = gr.Button("🔍 开始索引", variant="primary", size="sm")
                fts_clear_btn = gr.Button("清空索引", variant="stop", size="sm")
            fts_status_md = gr.Markdown("")

            gr.Markdown("### 已索引目录")
            fts_paths_table = gr.DataFrame(
                value=do_get_fts_paths,
                datatype=["str", "str"],
                column_count=(2, "fixed"),
                label="索引来源",
                interactive=False,
                wrap=True,
            )

            gr.Markdown("---")

            gr.Markdown("### 向量库文档管理")
            with gr.Row():
                refresh_kb_btn = gr.Button("刷新列表", size="sm")
                del_btn = gr.Button("删除选中", variant="stop", size="sm")
            kb_table = gr.DataFrame(
                value=do_get_kb_docs,
                datatype=["bool", "str", "str", "str", "number"],
                column_count=(5, "fixed"),
                label="文档列表",
                interactive=True,
                wrap=True,
            )
            del_status = gr.Markdown("")

    # ── 事件绑定 ─────────────────────────────────────────────────
    send_btn.click(chat, inputs=[question, chatbot], outputs=[chatbot, question, citation_buttons])
    question.submit(chat, inputs=[question, chatbot], outputs=[chatbot, question, citation_buttons])
    clear_btn.click(lambda: ([], "", ""), outputs=[chatbot, question, citation_buttons])
    refresh_btn.click(get_db_status, outputs=[status_text])
    provider_dropdown.change(do_switch_provider, inputs=[provider_dropdown], outputs=[model_dropdown, switch_status])
    switch_btn.click(do_apply_model, inputs=[provider_dropdown, model_dropdown], outputs=[switch_status])
    ingest_btn.click(do_ingest_file, inputs=[upload_file], outputs=[ingest_status])
    refresh_kb_btn.click(do_get_kb_docs, outputs=[kb_table])
    del_btn.click(do_delete_selected, inputs=[kb_table], outputs=[del_status, kb_table])
    okf_import_btn.click(do_okf_import, inputs=[okf_dir_input], outputs=[okf_status_md])
    fts_index_btn.click(do_fts_index, inputs=[fts_dir_input, fts_pattern], outputs=[fts_status_md])
    fts_clear_btn.click(do_fts_clear, outputs=[fts_status_md])


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
                     "prevent_thread_lock": True, "css": CITATION_CSS}
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
