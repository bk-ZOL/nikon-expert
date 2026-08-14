#!/usr/bin/env python3
"""
sync_kwiki.py (v2) — 把 WPS kwiki 知识库单向同步到本地镜像，并增量摄入 nikon-expert。

v2 变更（基于真实 API 返回调整）：
  - run_cli 网络失败自动重试 3 次（EOF 偶发）
  - 文件条目无修改时间字段，变更 token 改用 ctime+size，内容哈希兜底
  - WPS 原生文档 title 不带扩展名，按 doc_type 补：w→.docx s→.xlsx p→.pptx d→.dbt
  - 补齐顶层目录映射（厂内翻新报告 / Meas. / 技术协议 / 装机调试履历）

架构：
    WPS kwiki（编辑/协作层）
        │ 本脚本：kwiki-cli file-list 递归遍历 + file-download(file_base64)
        ▼
    本地镜像目录（保留知识库文件夹结构）
        │ 按后缀/目录映射分发到现有摄入管线
        ▼
    Qdrant + FTS（agent 检索层）

token 处理：本脚本【不】读取、不传递、不打印 token。
kwiki-cli 自己从 keyring / 环境变量 X_KWIKI_AUTH 解析鉴权。

用法：
    export KWIKI_KB_KUID="0s_3096687680"
    python scripts/sync_kwiki.py --probe        # 打印根目录原始 JSON
    python scripts/sync_kwiki.py --dry-run      # 只列出将同步的文件
    python scripts/sync_kwiki.py                # 同步 + 摄入
    python scripts/sync_kwiki.py --no-ingest    # 只镜像，不摄入
    python scripts/sync_kwiki.py --force        # 全量重下（建议每月跑一次，
                                                #   兜底 ctime+size 检测不到的编辑）

环境变量：
    KWIKI_KB_KUID     必填，知识库空间 kuid（0s 开头）
    KWIKI_MIRROR_DIR  镜像目录，默认 ./data/kwiki_mirror
    KWIKI_CLI         cli 命令名，默认 kwiki-cli
    MACHINE_MODEL     打给所有文档的机型标签，默认 NSR-S207D
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

KWIKI_CLI = os.getenv("KWIKI_CLI", "kwiki-cli")
KB_KUID = os.getenv("KWIKI_KB_KUID", "")
MIRROR = Path(os.getenv("KWIKI_MIRROR_DIR", "./data/kwiki_mirror"))
MACHINE_MODEL = os.getenv("MACHINE_MODEL", "NSR-S207D")
STATE_FILE = MIRROR / ".sync_state.json"

# ── WPS 顶层目录 -> (doc_type, 是否同步) ─────────────────────────
# 前缀匹配。兼容现有中文目录名和建议的编号目录名（10_/20_/...），
# 可以边迁移目录边同步。命中 doc_type 决定摄入管线；False = 不同步（检索噪声）。
DIR_MAP = [
    ("00_",         ("index",         True)),
    ("10_",         ("fault_history", True)),
    ("故障排查",     ("fault_history", True)),
    ("装机调试履历", ("fault_history", True)),
    ("20_",         ("sop",           True)),
    ("标准作业",     ("sop",           True)),
    ("30_",         ("manual",        True)),
    ("技术规格",     ("manual",        True)),
    ("厂务条件",     ("manual",        True)),
    ("ATP",         ("manual",        True)),
    ("厂内翻新报告", ("manual",        True)),
    ("Meas.",       ("checksheet",    True)),
    ("40_",         ("checksheet",    True)),
    ("物料备件",     ("checksheet",    True)),
    ("50_",         ("manual",        True)),
    ("光刻原理",     ("manual",        True)),
    ("调机视频",     ("manual",        True)),   # 只同步梗概文档，视频本体下载不了也没用
    ("90_",         ("",              False)),
    ("RFQ",         ("",              False)),
    ("相关计划",     ("",              False)),
    ("机台改造",     ("",              False)),
    ("技术协议",     ("",              False)),   # 商务文件，排障检索纯噪声
]

# WPS 原生文档 title 不带扩展名，按 doc_type 补
EXT_MAP = {"w": ".docx", "s": ".xlsx", "p": ".pptx", "d": ".dbt"}
# OTL 智能文档 file-download 会报业务错误，直接跳过
UNDOWNLOADABLE = {"o"}
# 这些 WPS 业务错误码 = 文档类型 API 根本不支持导出（在线文档"f"400408003、
# dbt/智能文档 400408000），非网络/临时故障 → 归为「不可下载」而非「失败」。
# 不写 state：每轮仍轻量探测一次（4xx 不重试，一次调用即返回），
# 将来若该文档转成可导出格式能自动补入。
UNDOWNLOADABLE_CODES = ("400408003", "400408000")
# download_file 的第三态哨兵：区别于 True(成功)/False(真失败)
_UNDOWNLOADABLE = object()
INGESTABLE_SUFFIXES = {".docx", ".pdf", ".md", ".xlsx", ".csv", ".txt"}
# 超过此大小不下载（视频等），单位 MB
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "50"))


def sniff_ext(path: Path) -> str:
    """按文件头魔数猜扩展名。未知 doc_type 下载下来没后缀时用。"""
    try:
        head = path.read_bytes()[:12]
    except OSError:
        return ""
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK"):
        import zipfile
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            if any(n.startswith("word/") for n in names):
                return ".docx"
            if any(n.startswith("xl/") for n in names):
                return ".xlsx"
            if any(n.startswith("ppt/") for n in names):
                return ".pptx"
        except Exception:
            pass
        return ".zip"
    if b"ftyp" in head:
        return ".mp4"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return ".doc"  # 旧版 Office 二进制格式（doc/xls/ppt 同魔数，doc 最常见）
    return ""


# ─────────────────────────────────────────────────────────────
# kwiki-cli 封装
# ─────────────────────────────────────────────────────────────

# 服务端会掐连续请求（EOF），每次调用之间强制间隔
THROTTLE = float(os.getenv("KWIKI_THROTTLE", "1.0"))
_last_call = [0.0]


def run_cli(*args) -> dict:
    """调用 kwiki-cli，返回解析后的 JSON。限速 + 失败重试 5 次（指数退避）。"""
    cmd = [KWIKI_CLI, "kwiki", *args, "--format", "json"]
    last_err = ""
    for attempt in range(5):
        wait = THROTTLE - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            out = proc.stdout.strip()
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                raise RuntimeError(f"kwiki-cli 输出不是 JSON:\n{out[:500]}")
        last_err = proc.stderr.strip()[:500]
        if attempt < 4:
            backoff = 3 * (attempt + 1)
            print(f"    ↻ 请求被断开，{backoff}s 后重试({attempt + 1}/5)...")
            time.sleep(backoff)
    raise RuntimeError(f"kwiki-cli 失败(重试5次): {' '.join(cmd)}\nstderr: {last_err}")


def list_folder(kuid: str) -> list:
    """列出一个 kuid 下的全部条目（处理分页）。"""
    items, page_token = [], ""
    while True:
        args = ["file-list", "--kuid", kuid, "--page-size", "100"]
        if page_token:
            args += ["--page-token", page_token]
        payload = run_cli(*args)
        items.extend(payload.get("list", []) or [])
        page_token = payload.get("next_page_token", "") or ""
        if not page_token:
            break
    return items


def resolve_title(item: dict) -> str:
    """title 无扩展名时按 doc_type 补，保证本地文件名可被后缀分发。"""
    title = str(item.get("title", "untitled"))
    dtype = str(item.get("doc_type", ""))
    if dtype in EXT_MAP and not Path(title).suffix:
        title += EXT_MAP[dtype]
    return title


def walk_kb(kuid: str, rel_path: str = "") -> list:
    """
    递归遍历知识库，返回文件清单：
    [{"kuid", "title", "doc_type", "rel_path", "token"}]
    token = ctime:size（列表无修改时间字段，编辑不改 ctime，
    尺寸不变的编辑检测不到——用定期 --force 兜底）
    """
    out = []
    for item in list_folder(kuid):
        dtype = str(item.get("doc_type", ""))
        ikuid = str(item.get("kuid", ""))
        if not ikuid:
            continue
        if dtype == "folder":
            title = str(item.get("title", "untitled"))
            out.extend(walk_kb(ikuid, rel_path=f"{rel_path}/{title}".strip("/")))
        else:
            out.append({
                "kuid": ikuid,
                "title": resolve_title(item),
                "doc_type": dtype,
                "rel_path": rel_path,
                "token": f"{item.get('ctime', '')}:{item.get('size', '')}",
            })
    return out


def download_file(kuid: str, dest: Path):
    """file-download(file_base64) -> 写入 dest。
    返回 True=成功 / _UNDOWNLOADABLE=文档类型不支持导出 / False=真失败。"""
    try:
        payload = run_cli("file-download", "--kuid", kuid,
                          "--response-type", "file_base64")
    except RuntimeError as e:
        msg = str(e)
        if any(code in msg for code in UNDOWNLOADABLE_CODES):
            print(f"    ⏭  在线文档 API 无法导出，跳过: {dest.name}")
            return _UNDOWNLOADABLE
        print(f"    ⚠️  下载失败 {dest.name}: {e}")
        return False

    b64 = _find_base64(payload)
    if not b64:
        print(f"    ⚠️  响应里没找到 base64 字段: {dest.name}")
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(b64))
    return True


def _find_base64(payload) -> str:
    """递归找 key 含 'base64'/'content' 且值像 base64 的字段。"""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, str) and len(v) > 100 and (
                "base64" in k.lower() or k.lower() in ("content", "data", "file")
            ):
                return v
            found = _find_base64(v)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_base64(item)
            if found:
                return found
    return ""


# ─────────────────────────────────────────────────────────────
# 目录映射 & 摄入分发
# ─────────────────────────────────────────────────────────────

_embed_ready = [False]


def _ensure_embed():
    """初始化本地 embedding 模型。
    注意：必须检查 Settings._embed_model（内部属性）——
    读 Settings.embed_model 属性本身会触发 LlamaIndex 的 OpenAI 默认解析并报错。
    与 mcp_server.py 的 _ensure_init 同一套路。"""
    if _embed_ready[0]:
        return
    from llama_index.core import Settings
    if Settings._embed_model is None:
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from src.device import get_device
        Settings.embed_model = HuggingFaceEmbedding(
            model_name=os.getenv("EMBED_MODEL_PATH", "./models/bge-m3"),
            max_length=512,
            device=get_device(),
        )
    Settings.llm = None
    _embed_ready[0] = True


def map_dir(rel_path: str):
    """顶层目录名 -> (doc_type, should_sync)"""
    top = rel_path.split("/")[0] if rel_path else ""
    for prefix, mapping in DIR_MAP:
        if top.startswith(prefix):
            return mapping
    return ("manual", True)  # 未知目录默认当 manual 同步


def ingest_dispatch(local_path: Path, doc_type: str):
    """按后缀 + doc_type 分发到现有摄入管线。"""
    _ensure_embed()
    suffix = local_path.suffix.lower()
    if suffix == ".docx":
        from src.ingest_docx import ingest_docx
        ingest_docx(str(local_path), doc_type=doc_type, machine_model=MACHINE_MODEL)
    elif suffix == ".pdf":
        from src.ingestor import ingest_pdf
        ingest_pdf(str(local_path), machine_model=MACHINE_MODEL, language="zh")
    elif suffix in (".xlsx", ".csv"):
        if doc_type == "fault_history":
            from src.ingestor import ingest_fault_history
            ingest_fault_history(str(local_path))
        else:
            from src.ingestor import ingest_excel
            ingest_excel(str(local_path), machine_model=MACHINE_MODEL)
    elif suffix in (".md", ".txt"):
        from src.fulltext import index_directory
        index_directory(str(local_path.parent), pattern=local_path.name,
                        doc_type=doc_type)
    else:
        print(f"    ⏭  不支持的格式，只镜像不摄入: {local_path.name}")
        return

    # 摄入成功 → 自动定级 + 写 Qdrant ACL payload（同步进来的新文档即时纳入权限）
    try:
        from src.acl_classify import apply_doc_acl
        apply_doc_acl(local_path.name)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="打印根目录 file-list 原始 JSON 后退出")
    ap.add_argument("--dry-run", action="store_true", help="只列出计划，不下载不摄入")
    ap.add_argument("--no-ingest", action="store_true", help="只镜像，不摄入")
    ap.add_argument("--reingest", action="store_true",
                    help="不下载，直接把本地镜像里的文件全部重新摄入"
                         "（摄入曾失败时用）")
    ap.add_argument("--force", action="store_true", help="忽略状态，全量重新下载")
    args = ap.parse_args()

    if args.reingest:
        if not MIRROR.exists():
            sys.exit(f"镜像目录不存在: {MIRROR}")
        stats = {"ingested": 0, "failed": 0, "skipped": 0}
        for p in sorted(MIRROR.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            if p.suffix.lower() not in INGESTABLE_SUFFIXES:
                stats["skipped"] += 1
                continue
            doc_type, _ = map_dir(str(p.relative_to(MIRROR)))
            print(f"  ⚙  {p.relative_to(MIRROR)}  [{doc_type}]")
            try:
                ingest_dispatch(p, doc_type)
                stats["ingested"] += 1
            except Exception as e:
                print(f"    ⚠️  摄入失败 {p.name}: {e}")
                stats["failed"] += 1
        print("\n" + "=" * 50)
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return

    if not KB_KUID:
        sys.exit("请先 export KWIKI_KB_KUID=<知识库kuid>"
                 "（kwiki-cli kwiki knowledge-view-list 可查）")

    if args.probe:
        payload = run_cli("file-list", "--kuid", KB_KUID, "--page-size", "10")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"遍历知识库 {KB_KUID} ...")
    files = walk_kb(KB_KUID)
    print(f"共 {len(files)} 个文件")

    state = {} if args.force else load_state()
    stats = {"synced": 0, "skipped_dir": 0, "skipped_undownloadable": 0,
             "unchanged": 0, "failed": 0, "ingested": 0}

    for f in files:
        doc_type, should_sync = map_dir(f["rel_path"])
        label = f"{f['rel_path']}/{f['title']}".strip("/")

        if not should_sync:
            stats["skipped_dir"] += 1
            continue
        if f["doc_type"] in UNDOWNLOADABLE:
            print(f"  ⏭  OTL 智能文档无法下载: {label}")
            stats["skipped_undownloadable"] += 1
            continue

        prev = state.get(f["kuid"], {})
        if f["token"] and prev.get("token") == f["token"] and not args.force:
            stats["unchanged"] += 1
            continue

        dest = MIRROR / f["rel_path"] / f["title"]
        print(f"  ⬇  {label}  [{doc_type}]")
        if args.dry_run:
            stats["synced"] += 1
            continue

        res = download_file(f["kuid"], dest)
        if res is _UNDOWNLOADABLE:
            stats["skipped_undownloadable"] += 1
            continue
        if not res:
            stats["failed"] += 1
            continue

        # 大文件（视频等）直接删掉不留镜像，也不摄入
        size_mb = dest.stat().st_size / 1024 / 1024
        if size_mb > MAX_FILE_MB:
            print(f"    ⏭  {size_mb:.0f}MB 超过上限 {MAX_FILE_MB:.0f}MB，跳过: {dest.name}")
            dest.unlink()
            state[f["kuid"]] = {"title": f["title"], "token": f["token"],
                                "sha": "oversize", "synced_at": time.strftime("%F %T")}
            save_state(state)
            stats["skipped_oversize"] = stats.get("skipped_oversize", 0) + 1
            continue

        # 无扩展名的未知类型：按魔数补名，让后缀分发能接住
        if not dest.suffix:
            ext = sniff_ext(dest)
            if ext:
                new_dest = dest.with_name(dest.name + ext)
                dest.rename(new_dest)
                dest = new_dest
                print(f"    ↳ 识别为 {ext}: {dest.name}")

        sha = hashlib.sha256(dest.read_bytes()).hexdigest()[:16]
        content_changed = sha != prev.get("sha")
        state[f["kuid"]] = {"title": f["title"], "token": f["token"],
                            "sha": sha, "synced_at": time.strftime("%F %T")}
        stats["synced"] += 1

        if content_changed and not args.no_ingest \
                and dest.suffix.lower() in INGESTABLE_SUFFIXES:
            try:
                ingest_dispatch(dest, doc_type)
                stats["ingested"] += 1
            except Exception as e:
                print(f"    ⚠️  摄入失败 {dest.name}: {e}")

        save_state(state)  # 每个文件存一次，天然断点续传

    print("\n" + "=" * 50)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"  镜像目录: {MIRROR.resolve()}")


if __name__ == "__main__":
    main()
