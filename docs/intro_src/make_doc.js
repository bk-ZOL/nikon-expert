// 生成《Nikon Expert 系统介绍》Word 文档
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell,
  WidthType, ShadingType, BorderStyle, ImageRun, LevelFormat, PageBreak, Footer, PageNumber, PageOrientation,
} = require("docx");

const IMG = path.join(__dirname, "img");
const OUT = process.argv[2];

const FONT = { ascii: "Arial", hAnsi: "Arial", eastAsia: "Microsoft YaHei", cs: "Arial" };
const ACCENT = "5A3FD1";
const MUTED = "5D5A70";
const LINE = "D9D6E6";

// ── 小工具 ──
const run = (t, o = {}) => new TextRun({ text: t, font: FONT, ...o });
function rich(s, o = {}) {           // **加粗** 语法
  return s.split(/(\*\*[^*]+\*\*)/).filter(Boolean).map(part =>
    part.startsWith("**") ? run(part.slice(2, -2), { bold: true, ...o }) : run(part, o));
}
const p = (s, o = {}) => new Paragraph({ children: rich(s, o.run || {}), spacing: { after: 120, line: 360 }, ...o.para });
const h1 = t => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [run(t)], spacing: { before: 360, after: 160 } });
const h2 = t => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [run(t)], spacing: { before: 240, after: 120 } });
const bullet = (s, level = 0) => new Paragraph({ numbering: { reference: "bul", level }, children: rich(s), spacing: { after: 80, line: 340 } });
const num = (s, ref = "num") => new Paragraph({ numbering: { reference: ref, level: 0 }, children: rich(s), spacing: { after: 80, line: 340 } });
const caption = t => new Paragraph({ alignment: AlignmentType.CENTER, children: [run(t, { size: 18, color: MUTED })], spacing: { before: 60, after: 240 } });

function image(file, widthPx) {
  const buf = fs.readFileSync(path.join(IMG, file));
  const w = buf.readUInt32BE(16), h = buf.readUInt32BE(20);   // PNG IHDR
  return new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { before: 120, after: 60 },
    children: [new ImageRun({ type: "png", data: buf, transformation: { width: widthPx, height: Math.round(widthPx * h / w) } })],
  });
}

// 表格：列宽 DXA，表宽 = 列宽之和；表头浅紫底
const TEXT_W = 9638;     // A4 正文宽（左右边距 1134 DXA = 2cm）
function table(headers, rows, widths) {
  const border = { style: BorderStyle.SINGLE, size: 4, color: LINE };
  const borders = { top: border, bottom: border, left: border, right: border };
  const cell = (s, w, head) => new TableCell({
    width: { size: w, type: WidthType.DXA }, borders,
    shading: head ? { type: ShadingType.CLEAR, color: "auto", fill: "ECE8FB" } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: String(s).split("\n").map(line => new Paragraph({ children: rich(line, head ? { bold: true, size: 20 } : { size: 20 }), spacing: { after: 40, line: 300 } })),
  });
  return new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA }, columnWidths: widths,
    rows: [new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, widths[i], true)) }),
           ...rows.map(r => new TableRow({ children: r.map((c, i) => cell(c, widths[i], false)) }))],
  });
}
const gap = () => new Paragraph({ children: [], spacing: { after: 120 } });

// 一个问答例子：灰底框
function example(q, lines) {
  const border = { style: BorderStyle.SINGLE, size: 4, color: LINE };
  return new Table({
    width: { size: TEXT_W, type: WidthType.DXA }, columnWidths: [TEXT_W],
    rows: [new TableRow({ children: [new TableCell({
      width: { size: TEXT_W, type: WidthType.DXA },
      borders: { top: border, bottom: border, right: border, left: { style: BorderStyle.SINGLE, size: 24, color: ACCENT } },
      shading: { type: ShadingType.CLEAR, color: "auto", fill: "F7F6FB" },
      margins: { top: 120, bottom: 120, left: 200, right: 200 },
      children: [
        new Paragraph({ children: [run("问：", { bold: true, color: ACCENT }), run(q, { bold: true })], spacing: { after: 80 } }),
        ...lines.map(l => new Paragraph({ children: rich(l, { size: 20 }), spacing: { after: 50, line: 320 } })),
      ],
    })] })],
  });
}

// ── 正文 ──
const children = [
  // 封面
  new Paragraph({ children: [run("NIKON NSR · 现场知识系统", { size: 20, color: ACCENT, bold: true })], spacing: { before: 1800, after: 200 } }),
  new Paragraph({ children: [run("Nikon Expert 系统介绍", { size: 56, bold: true })], spacing: { after: 240 } }),
  new Paragraph({ children: [run("把散在手册、检查表、日报里的知识，变成现场工程师一问就能用的答案", { size: 26, color: MUTED })], spacing: { after: 600 } }),
  new Paragraph({ children: [run("2026 年 10 月", { size: 22, color: MUTED })] }),
  new Paragraph({ children: [new PageBreak()] }),

  // 一、为什么
  h1("一、为什么需要这个系统"),
  p("光刻机现场排故、装机、调整，靠的是两样东西：原厂资料和现场经验。这两样我们都有，问题是**用的时候找不到、找到了也慢**。具体有这几件难处："),
  h2("1. 现场的难处"),
  num("**资料多、散，还是三种语言。**手头的资料有 700 份、几万页，分散在 WPS、飞书和个人电脑里：英文的原厂手册、日文的检查表、中文的自编 SOP。查一件事要翻好几本，日文的还要会看。"),
  num("**操作步骤是散的。**手册按章节写，一项作业常常拆在好几个小节里。比如 S207D 换卤素灯，手册 11.1 节分成 6 个部分、23 步，翻到其中一页只能看到一两步。"),
  num("**经验在日报和人的脑子里。**某个报警现场处理过几次、真正起作用的是哪一步、哪些尝试是白费功夫，都记在每天的调试履历里。新人不知道去哪找，同样的坑会反复踩。"),
  num("**报警码要换算，缩写各叫各的。**现场记录写 STG-29-80，手册里是主码 #29H、细分码 80H，要自己对表。RR、FIA、AVIS 这类缩写，手册里写的是全称，按缩写去搜往往搜不到。"),
  num("**机型之间的差异容易混。**同一项作业，S207D 和 S206D 的做法可能不同。比如换卤素灯，S207D 要锁断路器 CB221 (HL)，S206D 是 CB221 (AOM)，看错手册就会出错。"),
  num("**停机等不起。**设备停一小时就是一小时的产能，原厂支持往往要等，现场需要马上有可执行的步骤。"),
  h2("2. 系统怎么解决"),
  table(["现场的难处", "系统的做法", "实际效果"], [
    ["资料多、散、三种语言", "WPS、飞书、本地文件每 30 分钟自动同步进一个库；中英日资料一起检索，用中文问就能找到英文、日文原文", "700 份资料，22.9 万条语义索引，一个入口查全部"],
    ["操作步骤是散的", "把手册、检查表、自编 SOP 里的操作流程按原文顺序拼完整，建成 SOP 目录；同一章节的几部分合成一个作业", "3709 个完整流程；换卤素灯一问就给出全部 23 步"],
    ["经验在日报里", "每晚自动从履历、日报里提炼「报警 → 根因 → 真正有效的措施 → 走过的弯路」，归到 Troubleshooting 表，经确认后供大家查", "2375 条经验，回答里注明「现场处理过几次」"],
    ["报警码要换算、缩写不统一", "现场写法自动换算成手册编码；建缩写对照表，提问时自动补上全称", "STG-29-80 直接对上手册 #29H/80H；RR 自动展开为 Reticle Rotation"],
    ["机型差异容易混", "每份资料自动标机型，问题里点名机型就优先该机型资料，不同机型的差异会提醒", "回答会指出 S207D 和 S206D 断路器不同"],
    ["停机等不起", "多步排查智能体自己决定查什么、查几轮，给出带出处的步骤；多人可同时使用", "一般 10～30 秒给出完整答复，每步可追溯到原文"],
  ], [2300, 4338, 3000]),
  gap(),

  // 二、用起来是什么样
  h1("二、用起来是什么样"),
  p("下面是系统实际回答的几个例子（内容有删节）。"),
  example("207 的 AIS FC 怎么测？", [
    "**作业说明**：AIS Focus Calibration 计测，出自自编 SOP《AIS FC 计测》，原理见 MCSV Function Explanation 7.9.1。",
    "**步骤**：① 用任意光罩对位，确认 Sensor Calibration 显示 Alignment checked；② DIAGNOSIS → CALIBRATE Machine → Focus Sensor Calibration → Do Start；③ 读取 Focus Calibration Result；④ 结果取反，补到 ADJUST machine parameters → Wafer Focusing → Calib.offset (AIS)。",
    "**注意**：XY 扫描和 Z 扫描的结果略有差异，切换计测方式时确认 focus offset。　用时约 12 秒，每步标出处。",
  ]),
  gap(),
  example("STG-29-80 报警怎么处理？", [
    "**判断**：手册含义是主码 #29H（对准结果异常结束）、细分码 80H（ALG 单元不可重试错误）。但**现场这个报警多出现在光强均匀性测试或光轴调整之后**，根因是镭射与 BMU 位置水平、光轴偏移（现场处理过 2 次）。",
    "**步骤**：先确认是否刚做过光轴调整 → 调镭射和 BMU 水平 → 重做 BMU 光轴调整 → 重测光强均匀性；光路确认正常后仍报警，才按手册方向查对准信号线和参数。",
    "现场经验和原厂手册放在一起给，先给现场验证有效的做法。",
  ]),
  gap(),
  example("S207D 卤素灯怎么更换？", [
    "按手册 11.1 节给出 4 个阶段 17 步：显示 ITV 监视器 → 关灯并冷却 30 分钟 → 锁定 CB221 (HL) → 更换灯泡（只能用 NT12V100WL/NT，不能裸手接触）→ 复位计时器 → 点灯。",
    "**并主动提醒**：S206D 手册里锁的是 CB221 (AOM)，不要混用。",
  ]),
  gap(),
  example("RR 怎么测的？", [
    "系统先把 RR 展开成 Reticle Rotation（レチクルローテーション、掩模旋转），避开手册里几百处同名的「RR-axis」（机械手的轴），找到 RR 测量 SOP 和测试光罩说明书，给出测量步骤和合格标准。",
  ]),

];
const landscape = [
  h1("三、系统全景"),
  p("中间是系统，左边是资料从哪来、进库时做什么、守什么规矩；右边是变成了哪些知识、在哪里用、夜里自动做什么。"),
  image("mindmap_c.png", 900),
  caption("图 1　系统全景"),
];
const rest = [
  h1("四、资料怎么流转"),
  p("**分工**：自己写、持续更新的内容（装机履历、自编 SOP、知识整理）放飞书；原厂手册、PDF、表格、电路图放 WPS。系统每 30 分钟同步一次，改过的文件整篇替换旧版，删掉的同步删除，不会新旧两份并存。"),
  image("dataflow_c.png", 620),
  caption("图 2　资料流转"),
  bullet("**入库时**：自动解析 PDF、Word、Excel；截图做文字识别；按封面、文档编号判断机型；按类别、密级、机型、客户定权限，客户资料只给有权限的人看。"),
  bullet("**电路图**：除了文字，还会识别图上竖排的接插件号；S207D 已整理出 8324 条接线连接，可以按接插件号一路追到对端。"),
  bullet("**夜间提炼**：每晚在工程师的电脑空闲时，用本地模型提炼故障经验，提炼结果经管理员确认后才生效。持续更新的履历只处理新增和改过的那几天。"),

  h1("五、一次提问是怎么回答的"),
  image("qa_c.png", 520),
  caption("图 3　一次提问的处理流程"),
  bullet("简单的问题（参数、概念、规格）直接检索回答。"),
  bullet("问故障、问步骤的，交给**多步排查智能体**：它会先查现场经验，再查报警码含义、操作步骤、手册原文，信息够了才回答。"),
  bullet("每个结论都标出处（文件、章节、页码），工程师可以回到原文核对。"),

  h1("六、保密与合规"),
  p("资料里有原厂机密资料和客户信息，哪些环节能用外部服务是事先定好的："),
  table(["环节", "用什么", "理由"], [
    ["大批量提炼、抽取", "**本地**：工程师电脑上的模型", "整批资料不出自己的设备；电脑不可用就暂停，不会改用外部服务"],
    ["日常问答、整理一篇 SOP", "**云端**：deepseek", "每次只发送检索到的几段原文"],
    ["建立语义索引（向量化）", "**云端**：硅基流动 bge-m3", "服务条款写明不用于训练、计算完立即销毁"],
    ["Anthropic（Claude）API", "**不使用**", "其服务条款不对中国控股企业开放"],
  ], [2600, 2800, 4238]),
  gap(),
  bullet("**账号与权限**：每人用个人账号登录，按类别、密级、机型、客户分级；审计员只看访问记录。"),
  bullet("**显示脱敏**：回答里的客户名显示为代号。"),
  bullet("**人工把关**：自动提炼的经验先标「待确认」，管理员确认后才给其他人看、才会被引用。"),

  h1("七、规模与日常运行"),
  table(["项目", "数量（2026-10-17）"], [
    ["资料", "700 份（原厂手册 443、检查表 119、自编 SOP 93、电路图 16 册、错误码原文 18 份，另有故障履历、装机履历）"],
    ["语义索引", "22.9 万条"],
    ["完整操作流程（SOP 目录）", "3709 个"],
    ["故障排查经验（Troubleshooting 表）", "2375 条"],
    ["S207D 接线连接（线表）", "8324 条"],
    ["缩写对照", "219 个，可人工补充"],
  ], [4200, 5438]),
  gap(),
  table(["时间", "自动做的事"], [
    ["每 30 分钟", "同步 WPS 和飞书：新增入库、改过的替换、删掉的同步删除；刷新 SOP 目录和缩写对照表"],
    ["每天夜里 00:00～06:30", "工程师电脑空闲时，用本地模型提炼故障经验、给操作流程分模块"],
    ["每天 03:30", "新入库的电路图补识接插件号（入库时也会立即触发）"],
    ["白天", "管理员确认提炼出的经验，确认后生效"],
  ], [2600, 7038]),
  gap(),
  p("**使用入口**：网页（对话、SOP 速查、错误码速查、Troubleshooting 表、工作日志、知识库管理），以及在 Claude 里通过 13 个工具直接调用。网页最多可同时处理 3 个提问。"),

  h1("八、下一步"),
  bullet("补充现场常用缩写（TPR、RB、INC 等）的对照，让这类问题一问就准。"),
  bullet("逐步确认夜间提炼出的经验条目，扩充 Troubleshooting 表。"),
  bullet("上传 S308F 等机型的现场处理记录，让这些机型也有现场经验可查。"),
  bullet("SOP 速查增加按模块（WS、RS、WL、AIS、LC 等）筛选。"),
];

const PORTRAIT = { size: { width: 11906, height: 16838 }, margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 } };
const footer = () => new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
  children: [run("Nikon Expert 系统介绍　·　", { size: 16, color: MUTED }), new TextRun({ children: [PageNumber.CURRENT], size: 16, color: MUTED, font: FONT })] })] });

const doc = new Document({
  creator: "Nikon Expert",
  title: "Nikon Expert 系统介绍",
  styles: {
    default: { document: { run: { font: FONT, size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 32, bold: true, color: ACCENT, font: FONT }, paragraph: { spacing: { before: 360, after: 160 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: FONT }, paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 } },
    ],
  },
  numbering: { config: [
    { reference: "bul", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 420, hanging: 260 } } } }] },
    { reference: "num", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 420, hanging: 320 } } } }] },
  ] },
  sections: [
    { properties: { page: PORTRAIT }, footers: { default: footer() }, children },
    { properties: { page: { ...PORTRAIT, size: { ...PORTRAIT.size, orientation: PageOrientation.LANDSCAPE } } }, footers: { default: footer() }, children: landscape },
    { properties: { page: PORTRAIT }, footers: { default: footer() }, children: rest },
  ],
});

Packer.toBuffer(doc).then(b => { fs.writeFileSync(OUT, b); console.log("written", OUT, b.length); });
