#!/usr/bin/env python3
"""Mac 本地向量化助手 —— 在你的 Mac 上跑：选本地资料 → 本地嵌向量(快，MPS) → 自动推云端。

    python scripts/local_ingest_ui.py        # 浏览器自动开 http://127.0.0.1:7870

适合 demo 前批量把大手册在 Mac 上算好推上云，避开云端 2 核 CPU 慢。
云端知识库地址默认 root@100.93.22.104，可用环境变量 NE_SERVER 覆盖。
"""
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import gradio as gr

SERVER = os.getenv("NE_SERVER", "root@100.93.22.104")
SERVER_DIR = os.getenv("NE_SERVER_DIR", "/opt/nikon-expert")
_SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=25"]
_SCP = ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=25"]


def process(paths, progress=gr.Progress()):
    if not paths:
        yield "⚠️ 请先选择文件"
        return
    if not isinstance(paths, list):
        paths = [paths]
    files = [(p if isinstance(p, str) else getattr(p, "name", None)) for p in paths]
    files = [f for f in files if f]
    n = len(files)
    logs = []
    for i, src in enumerate(files):
        base = os.path.basename(src)
        t0 = time.time()
        out = src + ".nebundle"

        progress(i / n, desc=f"[{i+1}/{n}] {base} · 本地嵌向量中（BGE-M3 / MPS，大文件约 1–2 分钟）…")
        logs.append(f"⚙️ [{i+1}/{n}] {base} · 本地嵌向量中…")
        yield "\n".join(logs)
        r = subprocess.run([sys.executable, os.path.join(_ROOT, "scripts", "embed_bundle.py"), src, out],
                           cwd=_ROOT, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            logs[-1] = f"❌ [{i+1}/{n}] {base} 本地嵌向量失败：{((r.stderr or r.stdout) or '')[-200:]}"
            yield "\n".join(logs); continue

        progress((i + 0.6) / n, desc=f"[{i+1}/{n}] {base} · 上传云端…")
        rn = os.path.basename(out)
        up = subprocess.run(_SCP + [out, f"{SERVER}:/tmp/{rn}"], capture_output=True, text=True)
        if up.returncode != 0:
            logs[-1] = f"❌ [{i+1}/{n}] {base} 上传失败：{(up.stderr or '')[-200:]}"
            yield "\n".join(logs)
            try: os.remove(out)
            except Exception: pass
            continue

        progress((i + 0.85) / n, desc=f"[{i+1}/{n}] {base} · 云端导入中…")
        im = subprocess.run(_SSH + [SERVER,
            f"cd {SERVER_DIR} && QDRANT_URL=http://127.0.0.1:6333 .venv/bin/python "
            f"scripts/import_bundle.py /tmp/{rn} && rm -f /tmp/{rn}"],
            capture_output=True, text=True)
        try: os.remove(out)
        except Exception: pass

        dt = int(time.time() - t0)
        if im.returncode == 0:
            logs[-1] = f"✅ [{i+1}/{n}] {base} 完成（{dt}s）— 已进云端知识库，可直接检索"
        else:
            logs[-1] = f"⚠️ [{i+1}/{n}] {base} 嵌好但云端导入报错：{(im.stderr or '')[-200:]}"
        progress((i + 1) / n, desc=f"[{i+1}/{n}] {base} 完成")
        yield "\n".join(logs)

    progress(1.0, desc="全部完成")
    logs.append("\n🎉 全部处理完毕。云端知识库已更新，去网页问答即可检索到。")
    yield "\n".join(logs)


with gr.Blocks(title="Nikon Expert · 本地向量化") as demo:
    gr.Markdown(
        "## 🖥 本地向量化 → 上传云端\n"
        "在**你的 Mac** 上嵌向量（Apple Silicon 加速，比云端快很多），完成后自动推送到云端知识库。\n"
        f"适合批量把大手册提前入库。云端：`{SERVER}`")
    files = gr.File(label="选择本地资料（PDF / Markdown，可多选）",
                    file_count="multiple", file_types=[".pdf", ".md"], type="filepath")
    btn = gr.Button("🚀 本地向量化并上传", variant="primary")
    out = gr.Textbox(label="进度 / 结果", lines=12)
    btn.click(process, [files], [out])
    gr.Markdown("_提示：上传阶段会用 SSH 连云端；若卡在“上传/导入”，多半是 Clash 代理抖动，重试即可。_")


if __name__ == "__main__":
    port = int(os.getenv("LOCAL_UI_PORT", "7870"))
    print(f"\n🖥  本地向量化助手：http://127.0.0.1:{port}\n")
    demo.launch(server_name="127.0.0.1", server_port=port, inbrowser=True)
