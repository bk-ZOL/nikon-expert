import os
S = os.path.dirname(os.path.abspath(__file__))
CSS = """
*{box-sizing:border-box} body{margin:0;background:#fff;font-family:"PingFang SC","Hiragino Sans GB",sans-serif;color:#1d1b2a}
.c{position:relative}
.box{position:absolute;background:#fff;border:2px solid var(--k);border-radius:14px;padding:14px 18px 16px}
.box h3{margin:0 0 8px;font-size:23px;color:var(--k);display:flex;align-items:center;gap:8px}
.box h3 i{font-style:normal;font-size:14px;font-weight:600;background:var(--k);color:#fff;border-radius:999px;padding:1px 9px}
.box ul{margin:0;padding-left:20px;font-size:17px;line-height:1.65;color:#2b2938}
.box li b{font-weight:600}
.hub{position:absolute;border-radius:50%;background:#5a3fd1;color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;box-shadow:0 0 0 10px #ece8fb}
.hub b{font-size:34px;letter-spacing:.02em} .hub span{font-size:18px;opacity:.92;margin-top:4px}
"""
# ── 1. 思维导图：中心 + 左右各 3 个分支 ──
W, H = 1700, 1080
cx, cy, r = W // 2, H // 2, 150
branches = [
  ("left", 30, "#1f7a55", "资料来源", "3 处", ["<b>WPS 知识库</b>：原厂手册、检查表、电路图、表格", "<b>飞书知识库</b>：装机调试履历、自编 SOP、知识整理", "<b>本地文件</b>：一次性去重入库", "每 30 分钟自动同步，改了替换、删了同步删"]),
  ("left", 380, "#2f6db0", "入库处理", "自动", ["按格式解析：PDF / Word / Excel / md", "截图做 OCR，老格式自动转换", "<b>判断机型</b>：封面、文档编号、正文", "<b>权限定级</b>：类别 / 密级 / 机型 / 客户", "电路图：OCR + 矢量字形识读接插件号"]),
  ("left", 760, "#9a5b12", "保密规矩", "定死", ["<b>大批量提炼</b>只用 Mac 本地模型", "网页问答用 deepseek（每次只发几段）", "向量化用硅基流动（写明不训练、即删）", "不用 Anthropic API"]),
  ("right", 30, "#5a3fd1", "知识库", "6 类", ["<b>全文索引</b>：关键词、报警码", "<b>语义向量</b>：中英日跨语言检索", "<b>SOP 目录</b>：3709 个完整流程", "<b>缩写对照表</b>：RR → Reticle Rotation", "<b>线表</b>：接插件 → 线缆 → 对端", "<b>Troubleshooting 表</b>：原厂 + 现场经验"]),
  ("right", 430, "#b0336b", "使用入口", "3 个", ["<b>网页</b>：对话 / SOP 速查 / 错误码 / TS 表 / 工作日志", "<b>多步排查智能体</b>：想一步、查一步，每步标出处", "<b>Claude（MCP）</b>：13 个工具", "多人同时提问，最多 3 个一起处理"]),
  ("right", 780, "#1f7a55", "夜间提炼", "00:00–06:30", ["Mac 空闲时用本地 gemma4 跑", "履历 → 现场案例；手册 / 日文检查表 → 原厂对策", "SOP 按模块分类", "同样内容只做一次，人工确认后生效"]),
]
bw = 560
svg, boxes = [], []
for side, top, color, title, tag, items in branches:
    x = 30 if side == "left" else W - 30 - bw
    h = 52 + 30 * len(items) + 14
    boxes.append(f'<div class="box" style="--k:{color};left:{x}px;top:{top}px;width:{bw}px;height:{h}px"><h3>{title}<i>{tag}</i></h3><ul>' + "".join(f"<li>{i}</li>" for i in items) + "</ul></div>")
    ex = x + bw if side == "left" else x
    ey = top + h / 2
    mx = (cx + ex) / 2
    svg.append(f'<path d="M{cx},{cy} C{mx},{cy} {mx},{ey} {ex},{ey}" stroke="{color}" stroke-width="5" fill="none" stroke-linecap="round" opacity=".85"/>')
html = f"""<!doctype html><meta charset="utf-8"><style>{CSS}</style><div class="c" style="width:{W}px;height:{H}px">
<svg width="{W}" height="{H}" style="position:absolute;left:0;top:0">{''.join(svg)}</svg>
<div class="hub" style="left:{cx-r}px;top:{cy-r}px;width:{2*r}px;height:{2*r}px"><b>Nikon Expert</b><span>光刻机现场知识系统</span></div>
{''.join(boxes)}</div>"""
open(f"{S}/mindmap.html", "w", encoding="utf-8").write(html)

# ── 2. 资料流转：四列 + 底部夜间提炼回路 ──
def col(title, color, items, w):
    return (f'<div class="col" style="--k:{color};width:{w}px"><div class="ct">{title}</div>' +
            "".join(f'<div class="it">{i}</div>' for i in items) + "</div>")
CSS2 = CSS + """
.row{display:flex;align-items:stretch;gap:0;padding:30px}
.col{border:2px solid var(--k);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:10px;background:#fff}
.ct{font-size:22px;font-weight:700;color:var(--k);margin-bottom:2px}
.it{background:color-mix(in srgb,var(--k) 10%,#fff);border-radius:8px;padding:9px 12px;font-size:17px;line-height:1.45}
.ar{width:62px;display:flex;align-items:center;justify-content:center;font-size:44px;color:#9a97ad}
.loop{margin:0 30px 30px;border:2px dashed #1f7a55;border-radius:14px;padding:14px 18px;font-size:18px;display:flex;gap:14px;align-items:center;background:#f2faf6}
.loop b{color:#1f7a55;font-size:21px;white-space:nowrap}
"""
html2 = f"""<!doctype html><meta charset="utf-8"><style>{CSS2}</style><div style="width:1640px">
<div class="row">
{col("① 资料来源", "#1f7a55", ["WPS 知识库<br><small>手册 / 检查表 / 电路图</small>", "飞书知识库<br><small>履历 / 自编 SOP</small>", "本地文件<br><small>手动、去重后入库</small>"], 300)}
<div class="ar">→</div>
{col("② 入库（每 30 分钟）", "#2f6db0", ["解析 + 截图 OCR", "判断机型", "权限定级", "改了替换旧版、删了同步删"], 300)}
<div class="ar">→</div>
{col("③ 知识库", "#5a3fd1", ["全文索引 · 语义向量", "SOP 目录 · 缩写对照表", "电路图编号索引 · 线表", "Troubleshooting 表"], 340)}
<div class="ar">→</div>
{col("④ 使用", "#b0336b", ["网页：对话 / SOP 速查 / 错误码 / TS 表", "多步排查智能体", "Claude（MCP 工具）"], 360)}
</div>
<div class="loop"><b>⟲ 夜间提炼</b><span>每晚 00:00–06:30，Mac 空闲时用本地模型把 ③ 里的履历、手册、日文检查表提炼成故障经验，写回 Troubleshooting 表（待确认 → 管理员确认后生效）；同样内容只处理一次，履历只处理新增和改过的天。</span></div>
</div>"""
open(f"{S}/dataflow.html", "w", encoding="utf-8").write(html2)

# ── 3. 一次提问怎么回答：纵向 ──
CSS3 = CSS + """
.wrap{width:1100px;padding:30px;display:flex;flex-direction:column;align-items:center;gap:0}
.st{border:2px solid var(--k);border-radius:12px;padding:12px 20px;font-size:19px;text-align:center;background:#fff;max-width:760px}
.st small{display:block;color:#5d5a70;font-size:15px;margin-top:2px}
.dn{font-size:30px;color:#9a97ad;line-height:1.2}
.dec{border:2px solid #9a5b12;background:#fff7ec;border-radius:12px;padding:12px 22px;font-size:19px}
.lanes{display:flex;gap:28px;width:100%;align-items:stretch}
.lane{flex:1;border:2px solid var(--k);border-radius:14px;padding:14px 16px;display:flex;flex-direction:column;gap:8px;background:#fff}
.lane h4{margin:0;font-size:20px;color:var(--k)}
.lane p{margin:0;font-size:16px;color:#3b3950;line-height:1.55}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:#ece8fb;color:#3d2a9a;border-radius:999px;padding:4px 12px;font-size:15px}
"""
html3 = f"""<!doctype html><meta charset="utf-8"><style>{CSS3}</style><div class="wrap">
<div class="st" style="--k:#1d1b2a">工程师提问<small>例：「207 的 AIS FC 怎么测」「STG-29-80 报警怎么处理」</small></div>
<div class="dn">↓</div>
<div class="st" style="--k:#5a3fd1">缩写展开、识别机型<small>RR → Reticle Rotation / レチクルローテーション / 掩模旋转；点名机型优先该机型资料</small></div>
<div class="dn">↓</div>
<div class="dec">问故障、问「怎么做 / 步骤」，或者复合问题？</div>
<div class="dn">↓</div>
<div class="lanes">
  <div class="lane" style="--k:#2f6db0"><h4>否 → 快速检索</h4><p>全文 + 语义两路同时找 → 按机型加权 → 精排挑出最相关的几段 → 生成回答。适合查参数、概念、规格。</p></div>
  <div class="lane" style="--k:#b0336b"><h4>是 → 多步排查智能体</h4><p>想一步、查一步，信息够了再回答，每一步标出处：</p>
    <div class="chips"><span class="chip">Troubleshooting 表</span><span class="chip">错误码换算</span><span class="chip">操作步骤目录</span><span class="chip">手册检索</span><span class="chip">故障履历</span><span class="chip">电路图</span><span class="chip">线表追线</span></div></div>
</div>
<div class="dn">↓</div>
<div class="st" style="--k:#1f7a55">回答 + 出处<small>每个结论标来源文件、章节、页码；现场经验注明处理过几次</small></div>
</div>"""
open(f"{S}/qa.html", "w", encoding="utf-8").write(html3)
print("ok")
