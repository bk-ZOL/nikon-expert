#!/usr/bin/env python3
"""
Nikon Expert — Gradio 工程师交互界面
用法：python ui/app.py
然后在浏览器打开：http://localhost:7860
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import requests as _requests
import gradio as gr
from src.engine import (
    query, query_stream, _init_engine, engine_db_status,
    switch_llm, get_current_model,
    ingest_file, get_kb_documents, delete_document,
)
from src.fulltext import index_directory, clear_fts_index, get_indexed_paths, fts_status

# ── 预加载引擎（避免首次查询等待过长）──────────────────────────
print("⚙️  正在加载 Nikon Expert 引擎，请稍候...")
_init_engine()
print("✅ 引擎就绪！\n")


# ── 回调函数 ─────────────────────────────────────────────────────
def chat(question: str, history: list):
    if not question.strip():
        yield (history or []), ""
        return
    from src.router import auto_mode
    mode_key = auto_mode(question)
    history = history or []

    new_history = history + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": ""},
    ]
    yield new_history, ""

    accumulated = ""
    for delta, is_final, citations, has_result in query_stream(question, mode=mode_key, history=history):
        if is_final:
            if has_result and citations:
                sources = "\n\n**📚 参考来源：**\n" + "\n".join(f"- {c}" for c in citations)
                new_history[-1]["content"] = accumulated + sources
            else:
                new_history[-1]["content"] = accumulated or delta
        else:
            accumulated += delta
            new_history[-1]["content"] = accumulated
        yield new_history, ""


def get_db_status():
    s = engine_db_status()
    if s["status"] == "ok":
        fts_info = f" | FTS：{s.get('fts_records', 0):,} 条" if s.get("fts_records") else ""
        return f"✅ 知识库正常 | 向量：{s['points']:,}{fts_info} | {s['collection']}"
    return f"⚠️ 知识库异常：{s['status']}"


LOCAL_URL  = "http://localhost:11434"
REMOTE_URL = os.getenv("REMOTE_OLLAMA_URL", "http://192.168.168.208:11434")


def get_ollama_models(base_url: str = LOCAL_URL):
    try:
        r = _requests.get(f"{base_url}/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def do_switch_server(server_label: str):
    url = REMOTE_URL if "内网" in server_label else LOCAL_URL
    models = get_ollama_models(url)
    if not models:
        return f"⚠️ 无法连接到 {url}，请检查网络", gr.update()
    import os as _os
    _os.environ["LLM_BASE_URL"] = url
    switch_llm(models[0])
    return f"✅ 已切换到 **{server_label}**（{url}）", gr.update(choices=models, value=models[0])


def do_switch_model(model_name: str, server_label: str):
    if not model_name:
        return "⚠️ 请选择模型"
    url = REMOTE_URL if "内网" in server_label else LOCAL_URL
    import os as _os
    _os.environ["LLM_BASE_URL"] = url
    switch_llm(model_name)
    return f"✅ 已切换至 **{model_name}**"


def do_ingest_file(file_path):
    if not file_path:
        return "⚠️ 请先选择文件"
    result = ingest_file(file_path)
    return result["message"]


def do_get_kb_docs():
    docs = get_kb_documents()
    if not docs:
        return pd.DataFrame(columns=["选择", "文档名", "类型", "位置", "Chunks"])
    df = pd.DataFrame(docs)
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
        delete_document(name)
    preview = "、".join(selected[:3]) + ("..." if len(selected) > 3 else "")
    return f"✅ 已删除 {len(selected)} 个文档：{preview}", do_get_kb_docs()


# ── FTS 索引回调 ──────────────────────────────────────────────
def do_fts_index(dir_path: str, pattern: str):
    if not dir_path or not dir_path.strip():
        return "⚠️ 请先选择目录"
    dir_path = dir_path.strip()
    pattern = pattern.strip() or "*.md"
    if not os.path.isdir(dir_path):
        return f"⚠️ 目录不存在：{dir_path}"
    result = index_directory(dir_path, pattern)
    msg = f"✅ 索引完成：{result['indexed']} 个文件，{result['records']} 条记录"
    if result["errors"]:
        msg += f"\n⚠️ 错误 {len(result['errors'])} 个：" + "；".join(result["errors"][:3])
    return msg


def do_fts_clear():
    count = clear_fts_index(doc_type="grep_source")
    return f"✅ 已清空全文索引（{count} 条记录）"


def do_get_fts_paths():
    paths = get_indexed_paths()
    if not paths:
        return pd.DataFrame(columns=["路径", "类型"])
    return pd.DataFrame(paths)


# ── 界面布局 ─────────────────────────────────────────────────────
with gr.Blocks(title="Nikon Expert") as demo:

    gr.HTML("""
    <div class="header">
        <h1>🔬 Nikon Expert</h1>
        <p style="color:#666">光刻机设备知识库 · 纯本地运行 · 零数据外传</p>
    </div>
    """)

    with gr.Tabs():

        # ── Tab 1：对话 ───────────────────────────────────────────
        with gr.Tab("💬 对话"):
            with gr.Row():
                status_text = gr.Markdown(get_db_status(), elem_classes=["status-bar"])

            gr.Markdown("---")

            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("""
**自动识别模式：**
- 含 Error Code 或故障关键词 → 故障排查
- 其他问题 → 知识问答
                    """)
                    gr.Markdown("---")
                    server_radio = gr.Radio(
                        choices=["💻 本地 Ollama", "🏢 公司内网 Ollama"],
                        value="💻 本地 Ollama",
                        label="服务器",
                    )
                    model_dropdown = gr.Dropdown(
                        choices=get_ollama_models(),
                        value=get_current_model(),
                        label="🤖 LLM 模型",
                        interactive=True,
                    )
                    switch_btn = gr.Button("切换模型", size="sm")
                    switch_status = gr.Markdown("")
                    gr.Markdown("---")
                    gr.Markdown("**示例问题：**")
                    gr.Markdown("""
- Nikon 对焦系统工作原理
- E-5301 报警如何排查？
- Alignment 失败的常见原因
- PM 检查 Checksheet 第5项要求
                    """)

                with gr.Column(scale=2):
                    chatbot = gr.Chatbot(label="对话记录", height=500)
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
                        refresh_btn = gr.Button("刷新知识库状态", size="sm")

        # ── Tab 2：知识库 ─────────────────────────────────────────
        with gr.Tab("📂 知识库"):

            # ── 区块 A：上传文件 (RAG) ─────────────────────────
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

            # ── 区块 B：全文索引目录 (Grep) ───────────────────
            gr.Markdown("### 全文搜索索引")
            with gr.Row():
                fts_dir_input = gr.Textbox(
                    label="索引目录",
                    placeholder="点击下方按钮选择目录...",
                    scale=3,
                )
                fts_pattern = gr.Dropdown(
                    choices=["*.md", "*.txt", "*.md;*.txt", "*.pdf", "*.md;*.txt;*.pdf"],
                    value="*.md",
                    label="文件类型",
                    scale=1,
                )
            with gr.Row():
                # Gradio 没有原生目录选择器，用隐藏 file 组件 + JS hack
                fts_folder_picker = gr.File(
                    label="选择目录（选择目录中的任意一个文件即可）",
                    file_types=[".md", ".txt", ".pdf"],
                    type="filepath",
                    visible=False,
                )
                fts_select_btn = gr.Button("📁 选择目录", size="sm")
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

            # ── 区块 C：向量库文档管理 ─────────────────────────
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
    send_btn.click(chat, inputs=[question, chatbot], outputs=[chatbot, question])
    question.submit(chat, inputs=[question, chatbot], outputs=[chatbot, question])
    clear_btn.click(lambda: ([], ""), outputs=[chatbot, question])  # type: ignore
    refresh_btn.click(get_db_status, outputs=[status_text])
    server_radio.change(do_switch_server, inputs=[server_radio], outputs=[switch_status, model_dropdown])
    switch_btn.click(do_switch_model, inputs=[model_dropdown, server_radio], outputs=[switch_status])
    ingest_btn.click(do_ingest_file, inputs=[upload_file], outputs=[ingest_status])
    refresh_kb_btn.click(do_get_kb_docs, outputs=[kb_table])
    del_btn.click(do_delete_selected, inputs=[kb_table], outputs=[del_status, kb_table])

    # FTS 索引事件：选择文件后取其父目录
    fts_select_btn.click(
        None,
        inputs=[fts_folder_picker],
        outputs=[fts_folder_picker],
        js="""
        () => {
            // 触发隐藏的 file input 弹出文件选择器
            const fileInput = document.querySelector('input[type="file"]');
            if (fileInput) fileInput.click();
            return [];
        }
        """,
    )
    fts_folder_picker.change(
        lambda fp: os.path.dirname(fp) if fp else "",
        inputs=[fts_folder_picker],
        outputs=[fts_dir_input],
    )
    fts_index_btn.click(do_fts_index, inputs=[fts_dir_input, fts_pattern], outputs=[fts_status_md])
    fts_clear_btn.click(do_fts_clear, outputs=[fts_status_md])


if __name__ == "__main__":
    import threading

    port = int(os.getenv("UI_PORT", "7860"))
    url = f"http://127.0.0.1:{port}"

    demo.queue()
    # 不自动开浏览器，由 pywebview 接管
    server_thread = threading.Thread(
        target=demo.launch,
        kwargs={"server_name": "127.0.0.1", "server_port": port, "share": False, "prevent_thread_lock": True},
        daemon=True,
    )
    server_thread.start()

    # 等待 Gradio 服务就绪
    import time
    for _ in range(30):
        try:
            _requests.get(url, timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    # 用 pywebview 打开原生窗口（macOS 原生风格，无浏览器地址栏）
    try:
        import webview
        webview.create_window(
            "Nikon Expert",
            url,
            width=1200,
            height=800,
            min_size=(900, 600),
        )
        webview.start()
    except ImportError:
        print("pywebview 未安装，回退到浏览器模式")
        demo.launch(server_name="0.0.0.0", server_port=port, share=False, inbrowser=True)
