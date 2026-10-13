"""电路图上的 CAD 笔画字：直接从 PDF 矢量线条还原文字（不经过 OCR）。

Nikon 电路图上的接插件号、针脚号多是 CAD 单线笔画字，不在文字层里，但在 PDF 里是一串串矢量短线段，
同一个字符每次画出来的笔画形状一样（只是位置、大小、方向不同）。做法：
  1. 首尾相连的短线段拼成笔画，尺寸 ≤ 10 的才算字的一部分（长连线、方框自然排除）；碰在一起的笔画合成一个字；
  2. 大小相近、对齐、间距小的字拼成词，确定横排 / 竖排（竖排是逆时针转了 90°，从下往上读），
     传递拼接带进来的相邻一列的字再按位置切开；
  3. 每个字按词的字高转正、画到 18×18 小网格上，和模板逐个比（容许 1 格误差，两个方向覆盖率取小）；
  4. 只和"洞数"相同的模板比（E=0、6/0/P/A/4/9/D/R=1、B/8=2），B/8、D/0 再看左边是否有整根直竖线。
模板 src/glyph_templates.npz 用线表（Claude 读图）自动标注学出来（scripts/vector_glyph_train.py）。
在 S207D 已融合版 331 张没参与学习的配线图上：接插件号识别率 67%（OCR 57.5%），和 OCR 合并 83%。
"""
import collections
from pathlib import Path

import numpy as np

G = 16                                       # 网格：字高 16 格，外加一圈边
TEMPLATES = Path(__file__).with_name("glyph_templates.npz")
HOLES = {**{ch: 1 for ch in "0469ADOPRQ"}, "B": 2, "8": 2}


def expected_holes(ch: str) -> int:
    return HOLES.get(ch, 0)


# ── 线段 → 字 ──────────────────────────────────────────────────
def segments(page):
    out = []
    for g in page.get_drawings():
        if (g.get("width") or 0) > 0.6:
            continue
        for it in g["items"]:
            if it[0] == "l":
                out.append((it[1].x, it[1].y, it[2].x, it[2].y))
            elif it[0] == "c":                          # 贝塞尔曲线按首尾近似
                out.append((it[1].x, it[1].y, it[4].x, it[4].y))
    return out


class _DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def f(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def u(self, a, b):
        self.p[self.f(a)] = self.f(b)


def _bbox(a):
    return (min(a[:, 0].min(), a[:, 2].min()), min(a[:, 1].min(), a[:, 3].min()),
            max(a[:, 0].max(), a[:, 2].max()), max(a[:, 1].max(), a[:, 3].max()))


def glyphs(page, maxdim=10.0, tol=0.05):
    """返回 [{"box": (x0, y0, x1, y1), "segs": ndarray}]，每个是一个字（或符号）。"""
    segs = segments(page)
    n = len(segs)
    dsu = _DSU(n)
    ends = {}
    for i, (x0, y0, x1, y1) in enumerate(segs):
        for k in ((round(x0 / tol), round(y0 / tol)), (round(x1 / tol), round(y1 / tol))):
            if k in ends:
                dsu.u(i, ends[k])
            else:
                ends[k] = i
    comp = collections.defaultdict(list)
    for i in range(n):
        comp[dsu.f(i)].append(i)
    parts = []
    for ids in comp.values():
        b = _bbox(np.array([segs[i] for i in ids]))
        if max(b[2] - b[0], b[3] - b[1]) <= maxdim:
            parts.append([*b, ids])
    m = len(parts)
    d2 = _DSU(m)
    pad = 0.15
    order = sorted(range(m), key=lambda i: parts[i][0])
    for ii, i in enumerate(order):                        # 碰在一起（含交叉）的笔画合成一个字
        for j in order[ii + 1:]:
            if parts[j][0] > parts[i][2] + pad:
                break
            if parts[j][1] <= parts[i][3] + pad and parts[i][1] <= parts[j][3] + pad:
                d2.u(i, j)
    grp = collections.defaultdict(list)
    for i in range(m):
        grp[d2.f(i)].append(i)
    out = []
    for ids in grp.values():
        a = np.array([segs[s] for i in ids for s in parts[i][4]])
        b = _bbox(a)
        if 1.0 <= max(b[2] - b[0], b[3] - b[1]) <= maxdim:
            out.append({"box": b, "segs": a})
    return out


def words(gls, gap=0.75, align=0.35):
    """字拼成词：[{"dir": "h"/"v", "H": 字高, "chars": [字…按阅读顺序]}]。"""
    n = len(gls)
    dsu = _DSU(n)
    orient = {}
    D = [(*g["box"], (g["box"][0] + g["box"][2]) / 2, (g["box"][1] + g["box"][3]) / 2) for g in gls]
    for a in range(n):
        x0, y0, x1, y1, cx, cy = D[a]
        ha, wa = y1 - y0, x1 - x0
        for b in range(n):
            if b == a:
                continue
            bx0, by0, bx1, by1, bcx, bcy = D[b]
            hb, wb = by1 - by0, bx1 - bx0
            H = max(ha, hb)                               # 横排：b 在 a 右边
            if H > 1.5 and 0.6 < (ha + 1e-6) / (hb + 1e-6) < 1.7 and abs(cy - bcy) < align * H \
                    and 0 <= bx0 - x1 <= gap * H and H >= max(wa, wb) * 0.9:
                dsu.u(a, b)
                orient[a] = orient[b] = "h"
            W = max(wa, wb)                               # 竖排：b 在 a 上方（从下往上读）
            if W > 1.5 and 0.6 < (wa + 1e-6) / (wb + 1e-6) < 1.7 and abs(cx - bcx) < align * W \
                    and 0 <= y0 - by1 <= gap * W and W >= max(ha, hb) * 0.9:
                dsu.u(a, b)
                orient[a] = orient[b] = "v"
    grp = collections.defaultdict(list)
    for i in range(n):
        grp[dsu.f(i)].append(i)
    out = []
    for ids in grp.values():
        if len(ids) < 2:
            continue
        dr = collections.Counter(orient.get(i) for i in ids).most_common(1)[0][0]
        cs = [gls[i] for i in ids]
        if dr == "h":
            cs.sort(key=lambda g: g["box"][0])
            cross = lambda g: (g["box"][1] + g["box"][3]) / 2
            ext = lambda g: g["box"][3] - g["box"][1]
            gapf = lambda a, b: b["box"][0] - a["box"][2]
        else:
            cs.sort(key=lambda g: -g["box"][3])
            cross = lambda g: (g["box"][0] + g["box"][2]) / 2
            ext = lambda g: g["box"][2] - g["box"][0]
            gapf = lambda a, b: a["box"][1] - b["box"][3]
        H0 = max(ext(g) for g in cs)                       # 传递拼接带进来的相邻标签：按偏移和间距切开
        seg = [cs[0]]
        for a, b in zip(cs, cs[1:]):
            if abs(cross(a) - cross(b)) > 0.3 * H0 or not (-0.2 * H0 <= gapf(a, b) <= 0.75 * H0):
                if len(seg) >= 2 and max(ext(g) for g in seg) >= 1.0:
                    out.append({"dir": dr, "H": max(ext(g) for g in seg), "chars": seg})
                seg = [b]
            else:
                seg.append(b)
        if len(seg) >= 2 and max(ext(g) for g in seg) >= 1.0:
            out.append({"dir": dr, "H": max(ext(g) for g in seg), "chars": seg})
    return out


# ── 字 → 网格、特征 ──────────────────────────────────────────────
def upright(g, w):
    """按词的方向和字高把字转正，画到 (G+2)×(G+2) 网格。"""
    H = w["H"]
    x0, y0, x1, y1 = g["box"]
    if w["dir"] == "h":
        top = min(c["box"][1] for c in w["chars"])
        pts = [((sx - x0) / H, (sy - top) / H, (ex - x0) / H, (ey - top) / H) for sx, sy, ex, ey in g["segs"]]
    else:                                                  # 字顶朝左：屏幕往上 = 字的右，屏幕往右 = 字的下
        left = min(c["box"][0] for c in w["chars"])
        pts = [((y1 - sy) / H, (sx - left) / H, (y1 - ey) / H, (ex - left) / H) for sx, sy, ex, ey in g["segs"]]
    img = np.zeros((G + 2, G + 2), np.uint8)
    for sx, sy, ex, ey in pts:
        n = int(max(abs(ex - sx), abs(ey - sy)) * G * 2) + 2
        for t in np.linspace(0, 1, n):
            u, v = (sx + (ex - sx) * t) * G, (sy + (ey - sy) * t) * G
            if -1 <= u <= G and -1 <= v <= G:
                img[int(round(v)) + 1, int(round(u)) + 1] = 1
    return img


def dil(a):
    b = a.copy()
    b[1:, :] |= a[:-1, :]
    b[:-1, :] |= a[1:, :]
    b[:, 1:] |= a[:, :-1]
    b[:, :-1] |= a[:, 1:]
    return b


def dil_stack(X):
    Y = X.copy()
    Y[:, 1:, :] |= X[:, :-1, :]
    Y[:, :-1, :] |= X[:, 1:, :]
    Y[:, :, 1:] |= X[:, :, :-1]
    Y[:, :, :-1] |= X[:, :, 1:]
    return Y


def scores(a, R, DR, sR):
    """a 对模板组 R 的相似度：容许 1 格误差，两个方向的覆盖率取小（0~1）。"""
    sa = a.sum()
    if sa == 0 or len(R) == 0:
        return np.zeros(len(R))
    c1 = (DR & a).sum(axis=(1, 2)) / sa
    c2 = (R & dil(a)).sum(axis=(1, 2)) / np.maximum(sR, 1)
    return np.minimum(c1, c2)


def holes(g, n=48):
    """字里围出来的洞数。按字自身长边缩放到 n 格，笔画加粗一格防漏，从边上灌水，灌不到的空白就是洞。"""
    x0, y0, x1, y1 = g["box"]
    s = max(x1 - x0, y1 - y0) or 1
    img = np.zeros((n + 4, n + 4), bool)
    for sx, sy, ex, ey in g["segs"]:
        t = np.linspace(0, 1, int(max(abs(ex - sx), abs(ey - sy)) / s * n * 3) + 2)
        img[np.round((sy + (ey - sy) * t - y0) / s * n).astype(int) + 2,
            np.round((sx + (ex - sx) * t - x0) / s * n).astype(int) + 2] = True
    free = ~dil(img)

    def flood(seed):
        cur = seed & free
        while True:
            nxt = cur.copy()
            nxt[1:] |= cur[:-1]
            nxt[:-1] |= cur[1:]
            nxt[:, 1:] |= cur[:, :-1]
            nxt[:, :-1] |= cur[:, 1:]
            nxt &= free
            if (nxt == cur).all():
                return cur
            cur = nxt

    edge = np.zeros_like(free)
    edge[0] = edge[-1] = True
    edge[:, 0] = edge[:, -1] = True
    rest = free & ~flood(edge)
    cnt = 0
    while rest.any():
        seed = np.zeros_like(rest)
        seed[tuple(np.argwhere(rest)[0])] = True
        rest &= ~flood(seed)
        cnt += 1
    return cnt


def left_stem(a):
    """转正后左边是否有一整根直竖线（B/D 有，8/0 没有）。"""
    rows = np.where(a[1:-1].any(axis=1))[0]
    if len(rows) == 0:
        return False
    return a[1:-1, 1:3].any(axis=1)[rows[0]:rows[-1] + 1].mean() >= 0.9


# ── 识读 ────────────────────────────────────────────────────────
class Reader:
    def __init__(self, path=TEMPLATES, threshold=0.85):
        z = np.load(path)
        self.R = z["reps"].astype(np.uint8)
        self.DR = dil_stack(self.R)
        self.sR = self.R.sum(axis=(1, 2))
        self.L = [str(x) for x in z["labels"]]
        self.TH = z["holes"].astype(int)
        self.threshold = threshold

    def char(self, g, w) -> str:
        a = upright(g, w)
        sc = np.where(self.TH == holes(g), scores(a, self.R, self.DR, self.sR), 0)
        j = int(sc.argmax())
        if sc[j] < self.threshold:
            return "?"
        ch = self.L[j]
        if ch in "B8":
            ch = "B" if left_stem(a) else "8"
        elif ch in "D0":
            ch = "D" if left_stem(a) else "0"
        return ch

    def page_words(self, page) -> list:
        """一页上读出的所有词（认不出的字为 "?"）。"""
        return ["".join(self.char(c, w) for c in w["chars"]) for w in words(glyphs(page))]

    def page_tokens(self, page) -> set:
        """一页上读出的编号（按 diagram_config 的编号规则，和 OCR 同一套）。"""
        from src import diagram_config as C
        toks = set()
        for s in self.page_words(page):
            for _, pat in C.IDENTIFIER_PATTERNS:
                toks |= {m.upper() for m in pat.findall(s) if len(m) >= C.MIN_TOKEN_LEN and "?" not in m}
        return toks
