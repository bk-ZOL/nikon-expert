#!/usr/bin/env python3
"""
摄入 Nikon 指定知识库目录（PDF + MD）。
- Embedding 模型只初始化一次
- 显式 del 中间对象，减少内存峰值
- 断点续传：崩溃后重新运行会跳过已处理的 PDF
"""
import sys, os, gc, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from pathlib import Path

# ── 目标目录 ──────────────────────────────────────────────────
BASE = "/Users/jayce/Library/CloudStorage/GoogleDrive-xbtxhuhhis@gmail.com/我的云端硬盘/Obsidian/New_Work_Library/02_Projects_Work_项目与工作流/Active_Projects_进行中/01_Knowledge_Base_知识库/Nikon_Lithography_光刻设备"

TARGET_DIRS = [
    f"{BASE}/Model_207_307/01_Manuals_电路图_手册",
    f"{BASE}/Model_207_307/02_SOPs_作业指导书",
    f"{BASE}/Model_204/01_Manuals_电路图_手册",
]

CHECKPOINT_FILE = "./data/ingest_checkpoint.json"


def load_checkpoint():
    if Path(CHECKPOINT_FILE).exists():
        with open(CHECKPOINT_FILE) as f:
            return set(json.load(f).get("done", []))
    return set()


def save_checkpoint(done_set):
    Path(CHECKPOINT_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"done": list(done_set)}, f)


def main():
    print("=" * 60)
    print("Nikon Expert — 精准知识库摄入（支持断点续传）")
    print("=" * 60)

    # ── 1. 统计文件 ───────────────────────────────────────────
    all_pdfs, all_mds = [], []
    for d in TARGET_DIRS:
        p = Path(d)
        if not p.exists():
            print(f"⚠️  目录不存在，跳过：{d}")
            continue
        pdfs = sorted(p.rglob("*.pdf"))
        mds  = [f for f in sorted(p.rglob("*.md"))
                if not any(part.startswith('.') for part in f.parts)]
        print(f"📂 {p.name}：{len(pdfs)} PDF，{len(mds)} MD")
        all_pdfs.extend(pdfs)
        all_mds.extend(mds)
    print(f"\n📊 合计：{len(all_pdfs)} PDF + {len(all_mds)} MD")

    # ── 加载断点 ──────────────────────────────────────────────
    done_set = load_checkpoint()
    resume   = bool(done_set)
    if resume:
        print(f"📌 发现断点，已完成 {len(done_set)} 个，继续未完成部分...")
    print("=" * 60)

    # ── 2. 初始化（只做一次）──────────────────────────────────
    import pymupdf4llm
    from llama_index.core import StorageContext, Settings, VectorStoreIndex, Document
    from llama_index.core.node_parser import SentenceSplitter
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    embed_path  = os.getenv("EMBED_MODEL_PATH", "./models/bge-m3")
    qdrant_path = os.getenv("QDRANT_PATH", "./data/qdrant_db")
    collection  = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

    print("\n⚙️  加载 Embedding 模型（仅此一次）...")
    Settings.embed_model = HuggingFaceEmbedding(
        model_name=embed_path, max_length=512, device="mps"
    )
    Settings.llm = None

    print("⚙️  连接 Qdrant...")
    client = QdrantClient(path=qdrant_path)
    existing = [c.name for c in client.get_collections().collections]

    if not resume:
        # 首次运行：清空旧数据
        if collection in existing:
            client.delete_collection(collection)
            print(f"   🗑  已清空：{collection}")
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
        )
        print(f"   ✅ 新建 Collection：{collection}")
    else:
        print(f"   📌 续传模式，保留现有 Collection：{collection}")

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    total_chunks = 0

    def get_ctx():
        vs = QdrantVectorStore(client=client, collection_name=collection)
        return StorageContext.from_defaults(vector_store=vs)

    # ── 3. 摄入 PDF ───────────────────────────────────────────
    skipped = 0
    for i, pdf in enumerate(all_pdfs, 1):
        pdf_key = str(pdf)

        # 断点续传：已完成的跳过
        if pdf_key in done_set:
            print(f"\n[{i}/{len(all_pdfs)}] ⏭  跳过（已完成）：{pdf.name}")
            continue

        # 推断机型
        model = next((p for p in pdf.parts if "Model_" in p), "")
        print(f"\n[{i}/{len(all_pdfs)}] 📄 {pdf.name}")
        try:
            pages = pymupdf4llm.to_markdown(str(pdf), page_chunks=True)
            docs  = []
            for chunk in pages:
                text = chunk.get("text", "").strip()
                if not text:
                    continue
                page = chunk.get("metadata", {}).get("page", 0) + 1
                docs.append(Document(
                    text=text,
                    metadata={
                        "doc_name":      pdf.name,
                        "doc_type":      "manual",
                        "language":      "en",
                        "page_start":    page,
                        "page_end":      page,
                        "machine_model": model,
                        "chunk_type":    "body",
                    },
                ))
            del pages  # 立即释放大对象

            if not docs:
                print("   ⚠️  无可用文本，跳过")
                skipped += 1
                done_set.add(pdf_key)
                save_checkpoint(done_set)
                continue

            nodes = splitter.get_nodes_from_documents(docs, show_progress=False)
            del docs  # 立即释放

            VectorStoreIndex(nodes, storage_context=get_ctx(), show_progress=True)
            n_chunks = len(nodes)
            del nodes  # 立即释放

            total_chunks += n_chunks
            print(f"   ✅ {n_chunks} chunks")

            done_set.add(pdf_key)
            save_checkpoint(done_set)
            gc.collect()
            try:
                import torch
                if torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            except Exception:
                pass

        except Exception as e:
            print(f"   ⚠️  跳过：{e}")
            skipped += 1
            done_set.add(pdf_key)
            save_checkpoint(done_set)

    # ── 4. 摄入 MD ────────────────────────────────────────────
    if all_mds:
        print(f"\n📝 摄入 {len(all_mds)} 个 MD 文件...")
        from llama_index.core.readers import SimpleDirectoryReader
        import uuid
        readable = []
        for f in all_mds:
            try:
                with open(f, "rb"): pass
                readable.append(f)
            except OSError:
                pass
        if readable:
            reader = SimpleDirectoryReader(
                input_files=readable,
                file_metadata=lambda p: {
                    "doc_name": Path(p).name,
                    "doc_type": "obsidian_note",
                    "language": "zh",
                    "file_path": str(p),
                    "doc_id":   str(uuid.uuid4()),
                },
            )
            md_docs  = reader.load_data()
            md_nodes = splitter.get_nodes_from_documents(md_docs, show_progress=True)
            del md_docs
            VectorStoreIndex(md_nodes, storage_context=get_ctx(), show_progress=True)
            total_chunks += len(md_nodes)
            print(f"   ✅ {len(md_nodes)} chunks")
            del md_nodes
            gc.collect()

    # ── 5. 汇总 ───────────────────────────────────────────────
    info = client.get_collection(collection)
    client.close()

    # 清理断点文件
    if Path(CHECKPOINT_FILE).exists():
        Path(CHECKPOINT_FILE).unlink()
        print("   🧹 断点文件已清理")

    print("\n" + "=" * 60)
    print(f"🎉 摄入完成！")
    print(f"   PDF: {len(all_pdfs) - skipped} 个成功，{skipped} 个跳过")
    print(f"   总 Chunk：{total_chunks}")
    print(f"   向量库：{info.points_count:,} 个向量")
    print("=" * 60)


if __name__ == "__main__":
    main()
