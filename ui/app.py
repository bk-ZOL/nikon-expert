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
def chat(question: str, history: list):
    if not question.strip():
        yield (history or []), "", ""
        return
    from src.router import auto_mode
    mode_key = auto_mode(question)
    history = history or []

    new_history = history + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": ""},
    ]
    yield new_history, "", ""

    accumulated = ""
    citations_data = []
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
        if fp and fp.lower().endswith(".pdf"):
            pdf_url = f"/serve-pdf?path={_html.escape(_requests.utils.quote(fp, safe=''))}"
            if page:
                pdf_url += f"#page={page}"
            items.append(
                f'<a class="cite-link" href="{pdf_url}" target="_blank">'
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


def do_switch_server(server_label: str):
    url = REMOTE_URL if "内网" in server_label else LOCAL_URL
    models = backend.get_ollama_models(url)
    if not models:
        return f"⚠️ 无法连接到 {url}，请检查网络", gr.update()
    if not models:
        return f"⚠️ 无法连接到 {url}，请检查网络", gr.update()
    import os as _os
    _os.environ["LLM_BASE_URL"] = url
    if models:
        backend.switch_llm(models[0], url)
    return f"✅ 已切换到 **{server_label}**（{url}）", gr.update(choices=models, value=models[0] if models else None)


def do_switch_model(model_name: str, server_label: str):
    if not model_name:
        return "⚠️ 请选择模型"
    url = REMOTE_URL if "内网" in server_label else LOCAL_URL
    import os as _os
    _os.environ["LLM_BASE_URL"] = url
    backend.switch_llm(model_name, url)
    return f"✅ 已切换至 **{model_name}**"


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
                        server_radio = gr.Radio(
                            choices=["💻 本地 Ollama", "🏢 公司内网 Ollama"],
                            value="💻 本地 Ollama",
                            label="服务器",
                        )
                        model_dropdown = gr.Dropdown(
                            choices=backend.get_ollama_models(),
                            value=backend.get_current_model(),
                            label="🤖 LLM 模型",
                            interactive=True,
                        )
                    with gr.Column(scale=1):
                        switch_btn = gr.Button("切换模型", size="sm")
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
    server_radio.change(do_switch_server, inputs=[server_radio], outputs=[switch_status, model_dropdown])
    switch_btn.click(do_switch_model, inputs=[model_dropdown, server_radio], outputs=[switch_status])
    ingest_btn.click(do_ingest_file, inputs=[upload_file], outputs=[ingest_status])
    refresh_kb_btn.click(do_get_kb_docs, outputs=[kb_table])
    del_btn.click(do_delete_selected, inputs=[kb_table], outputs=[del_status, kb_table])
    fts_index_btn.click(do_fts_index, inputs=[fts_dir_input, fts_pattern], outputs=[fts_status_md])
    fts_clear_btn.click(do_fts_clear, outputs=[fts_status_md])


# ── 注册 FastAPI 路由（本地模式需要 PDF 服务，远程模式由 API 服务提供） ──
import pathlib as _pl

def _register_routes(fastapi_app):
    from fastapi.responses import FileResponse, PlainTextResponse

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


if __name__ == "__main__":
    import threading

    port = int(os.getenv("UI_PORT", "7860"))

    # 服务器模式：监听 0.0.0.0，不启动 pywebview
    is_server = not backend.is_local
    host = "0.0.0.0" if is_server else "127.0.0.1"
    url = f"http://{host}:{port}"

    demo.queue()
    server_thread = threading.Thread(
        target=demo.launch,
        kwargs={"server_name": host, "server_port": port, "share": False, "prevent_thread_lock": True, "css": CITATION_CSS},
        daemon=True,
    )
    server_thread.start()

    # 等待 Gradio 服务就绪
    import time
    for _ in range(30):
        try:
            _requests.get(f"http://127.0.0.1:{port}", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    # 本地模式：注册 PDF 服务路由
    if backend.is_local:
        _register_routes(demo.app)

    if is_server:
        print(f"🌐 服务器模式运行中：{url}")
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
