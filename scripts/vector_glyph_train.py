"""重新学习矢量字形模板（src/glyph_templates.npz），换图册 / 字体时用。在 Mac 上跑。

做法：从有线表的页里取出所有字，按"洞数相同、形状相近"严格聚类；每类先用现有模板给个起始标签，
再拿线表（Claude 读图抽的接插件号）逐位对齐投票：一个词只投给唯一最匹配的号（J7405A/J7405B 并列时不投），
迭代 3 轮。票数 ≥3、多数 ≥75%、且与该字符应有的洞数一致才改标签。线表页按内容分组，一半学习、一半考试。

用法：
  1) 服务器上导出线表每页的接插件号：
     .venv/bin/python -c "import sqlite3,re,json,collections; ..."  （见 CLAUDE.md 第 5 节）> /tmp/netlist_tokens.json
  2) Mac：python3 scripts/vector_glyph_train.py --pdf ~/Downloads/<图册>.pdf --netlist /tmp/netlist_tokens.json \\
          [--out src/glyph_templates.npz] [--workers 8]
     会打印考试页上的识别率，满意再提交新的模板文件。
"""
import argparse
import collections
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.vector_glyph import (G, Reader, TEMPLATES, dil, dil_stack, expected_holes, glyphs, holes,  # noqa: E402
                              scores, upright, words)

_PDF = None


def _init(pdf):
    global _PDF
    import fitz
    _PDF = fitz.open(pdf)


def _chars(p):
    out = []
    for wi, w in enumerate(words(glyphs(_PDF[p - 1]))):
        for ci, c in enumerate(w["chars"]):
            out.append((p, wi, ci, upright(c, w), holes(c)))
    return out


def _read(p):
    return p, Reader(_TEMPL).page_tokens(_PDF[p - 1])


_TEMPL = None


def _init_read(pdf, templ):
    global _TEMPL
    _init(pdf)
    _TEMPL = templ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--netlist", required=True, help="{页码: [接插件号…]} 的 JSON")
    ap.add_argument("--out", default=str(TEMPLATES))
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    pdf = str(Path(a.pdf).expanduser())
    NL = {int(k): set(v) for k, v in json.loads(Path(a.netlist).read_text()).items()}
    groups = collections.defaultdict(list)
    for p, toks in NL.items():
        groups[frozenset(toks)].append(p)
    gl = sorted(groups.values(), key=min)
    train = [min(g) for i, g in enumerate(gl) if i % 2 == 0]
    test = [p for i, g in enumerate(gl) if i % 2 == 1 for p in g]
    t0 = time.time()
    print(f"学习页 {len(train)}（去重后），考试页 {len(test)}", flush=True)
    with ProcessPoolExecutor(a.workers, initializer=_init, initargs=(pdf,)) as ex:
        chars = [c for r in ex.map(_chars, train, chunksize=4) for c in r]
    print(f"取字 {len(chars)} 个，用时 {time.time() - t0:.0f}s", flush=True)

    # 严格聚类（只在洞数相同的字里）
    reps, cnt, members, rh, byh = [], [], [], [], {}
    for k, (p, wi, ci, x, h) in enumerate(chars):
        ids, R, DR, sR = byh.get(h, ([], np.zeros((0, G + 2, G + 2), np.uint8),
                                     np.zeros((0, G + 2, G + 2), np.uint8), np.zeros(0)))
        if ids:
            s = scores(x, R, DR, sR)
            j = int(s.argmax())
            if s[j] >= 0.9:
                cnt[ids[j]] += 1
                members[ids[j]].append(k)
                continue
        reps.append(x); cnt.append(1); members.append([k]); rh.append(h)
        byh[h] = (ids + [len(reps) - 1], np.concatenate([R, x[None]]), np.concatenate([DR, dil(x)[None]]),
                  np.append(sR, x.sum()))
    print(f"聚类 {len(reps)} 类，用时 {time.time() - t0:.0f}s", flush=True)

    # 起始标签：现有模板
    old = Reader()
    final = []
    for x, h in zip(reps, rh):
        s = np.where(old.TH == h, scores(x, old.R, old.DR, old.sR), 0)
        j = int(s.argmax())
        final.append(old.L[j] if s[j] >= 0.8 else "?")
    cl_of = {k: j for j, ms in enumerate(members) for k in ms}
    byword = collections.defaultdict(list)
    for k, (p, wi, ci, x, h) in enumerate(chars):
        byword[(p, wi)].append((ci, k))
    for rnd in range(3):                                  # 线表投票
        votes = collections.defaultdict(collections.Counter)
        for (p, wi), cs in byword.items():
            cs.sort()
            s = "".join(final[cl_of[k]] for _, k in cs)
            cand = [(sum(x == y for x, y in zip(s, t)), t) for t in NL.get(p, ()) if len(t) == len(s) and s[0] == t[0]]
            if not cand:
                continue
            best = max(m for m, _ in cand)
            top = [t for m, t in cand if m == best]
            if len(top) == 1 and best >= len(s) - 2:
                for (ci, k), ch in zip(cs, top[0]):
                    votes[cl_of[k]][ch] += 1
        new = []
        for j in range(len(reps)):
            v = votes.get(j)
            if v and sum(v.values()) >= 3 and v.most_common(1)[0][1] / sum(v.values()) >= 0.75 \
                    and expected_holes(v.most_common(1)[0][0]) == rh[j]:
                new.append(v.most_common(1)[0][0])
            else:
                new.append(final[j])
        print(f"  第 {rnd + 1} 轮投票：改了 {sum(x != y for x, y in zip(final, new))} 类", flush=True)
        final = new
    keep = [j for j in range(len(reps)) if final[j] != "?" and cnt[j] >= 2]
    out = Path(a.out)
    np.savez_compressed(out, reps=np.array([reps[j] for j in keep], dtype=np.uint8),
                        labels=np.array([final[j] for j in keep]), holes=np.array([rh[j] for j in keep], dtype=np.int16))
    print(f"模板 {len(keep)} 个 → {out}，覆盖：{''.join(sorted(set(final[j] for j in keep)))}", flush=True)

    # 考试
    JP = re.compile(r"^[JP]\d{4}[A-Z]?\d?$")
    with ProcessPoolExecutor(a.workers, initializer=_init_read, initargs=(pdf, out)) as ex:
        res = dict(ex.map(_read, test, chunksize=4))
    tot = sum(len(NL[p]) for p in test)
    hit = sum(len(NL[p] & res[p]) for p in test)
    got = sum(len({t for t in res[p] if JP.match(t)}) for p in test)
    print(f"考试页 {len(test)}：识别率 {hit / tot:.1%}，读出接插件号 {got} 个，其中线表里有的 {hit / max(got, 1):.1%}")


if __name__ == "__main__":
    main()
