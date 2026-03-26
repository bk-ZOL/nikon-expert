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
    query, _init_engine, engine_db_status,
    switch_llm, get_current_model,
    ingest_file, get_kb_documents, delete_document,
)

# ── 预加载引擎（避免首次查询等待过长）──────────────────────────
print("⚙️  正在加载 Nikon Expert 引擎，请稍候...")
_init_engine()
print("✅ 引擎就绪！\n")


# ── 回调函数 ─────────────────────────────────────────────────────
def chat(question: str, mode: str, history: list):
    if not question.strip():
        return history, ""
    mode_key = "troubleshoot" if "故障" in mode else "qa"
    result = query(question, mode=mode_key)
    if result["has_result"]:
        sources = "\n\n**📚 参考来源：**\n" + "\n".join(f"- {c}" for c in result["citations"])
        bot_msg = result["answer"] + sources
    else:
        bot_msg = result["answer"]
    history = history or []
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": bot_msg})
    return history, ""


def get_db_status():
    s = engine_db_status()
    if s["status"] == "ok":
        return f"✅ 知识库正常 | 向量数：{s['points']:,} | Collection：{s['collection']}"
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
    """切换 Ollama 服务器（本地 / 内网）"""
    url = REMOTE_URL if "内网" in server_label else LOCAL_URL
    models = get_ollama_models(url)
    if not models:
        return f"⚠️ 无法连接到 {url}，请检查网络", gr.update()
    # 更新引擎的 LLM base_url
    import os as _os
    _os.environ["LLM_BASE_URL"] = url
    switch_llm(models[0])  # 切换到该服务器第一个可用模型
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
                    mode = gr.Radio(
                        choices=["📖 知识问答", "🔧 故障排查"],
                        value="📖 知识问答",
                        label="查询模式",
                    )
                    gr.Markdown("""
**📖 知识问答**：设备原理、调机参数、SOP
**🔧 故障排查**：输入 Error Code 或故障现象
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
- PM 检查 checksheet 第5项要求
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

        # ── Tab 2：上传文档 ───────────────────────────────────────
        with gr.Tab("📤 上传文档"):
            gr.Markdown("### 上传新资料到知识库")
            gr.Markdown("支持格式：**PDF**（手册/电路图/SOP）、**MD**（Obsidian 笔记）")
            with gr.Row():
                with gr.Column(scale=1):
                    upload_file = gr.File(
                        label="选择文件",
                        file_types=[".pdf", ".md"],
                        type="filepath",
                    )
                    ingest_btn = gr.Button("摄入到知识库", variant="primary")
                    ingest_status = gr.Markdown("")
                with gr.Column(scale=1):
                    gr.Markdown("""
**注意事项：**
- 纯图片扫描 PDF 无法提取文字，建议先 OCR 处理
- 摄入大文件（>200页）需要约 1-3 分钟
- 摄入期间可以继续使用对话功能
- 摄入完成后立即可以被检索到
                    """)

        # ── Tab 3：知识库管理 ─────────────────────────────────────
        with gr.Tab("📚 知识库管理"):
            gr.Markdown("### 已收录文档")
            gr.Markdown("勾选要删除的文档，然后点击「删除选中」按钮。")
            with gr.Row():
                refresh_kb_btn = gr.Button("🔄 刷新列表", size="sm")
                del_btn = gr.Button("🗑 删除选中", variant="stop", size="sm")
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
    send_btn.click(chat, inputs=[question, mode, chatbot], outputs=[chatbot, question])
    question.submit(chat, inputs=[question, mode, chatbot], outputs=[chatbot, question])
    clear_btn.click(lambda: ([], ""), outputs=[chatbot, question])  # type: ignore
    refresh_btn.click(get_db_status, outputs=[status_text])
    server_radio.change(do_switch_server, inputs=[server_radio], outputs=[switch_status, model_dropdown])
    switch_btn.click(do_switch_model, inputs=[model_dropdown, server_radio], outputs=[switch_status])
    ingest_btn.click(do_ingest_file, inputs=[upload_file], outputs=[ingest_status])
    refresh_kb_btn.click(do_get_kb_docs, outputs=[kb_table])
    del_btn.click(do_delete_selected, inputs=[kb_table], outputs=[del_status, kb_table])


if __name__ == "__main__":
    port = int(os.getenv("UI_PORT", "7860"))
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        share=False,
        inbrowser=True,
    )
