# Nikon Expert — 现状与后续

> 写于 2026-11-03。这份文件告诉后来的人：**现在网页上已经有什么**，以及**还可以考虑、但没有排期的想法**。
> 它不是施工单。2026-04-26 那一版把「引用一键跳原文」写成 P0，那件事早做完了，不要再按那张清单开工。

能力以仓库为准：[README.md](README.md)（2026-10-17）、[CLAUDE.md](CLAUDE.md)、`ui/app.py` 和 `src/`。下面每条「已经在用」都能在这些地方对上。

规模是 README 当天的盘点（资料 700 份、向量 22.9 万、SOP 流程 3709、缩写 219、Troubleshooting 2375 条、S207D 线表 8324 条连接）。之后没有重新数过。2026-11-02 的 PR #11 改的是并发和提炼质量，不是这组数字。

---

## 现在跑的是什么

代码里没有单独的产品版本号。文件 `Nikon_Expert_系统实现方案_V2.3.docx` 是 2026-03 的设计稿（封面仍写 V1.0、2026-03-13），里面的本地 Qwen、数据不出厂，和现在的跑法对不上。**不要拿那份稿子当现状。** 给人介绍用 [README.md](README.md) 和 [`docs/system_overview.html`](docs/system_overview.html)。

网页（`ui/app.py`，导航 `_VIEWS`）现在有这些入口：

| 入口 | 做什么 |
|---|---|
| 对话 | 简单问题走全文 + 语义，再精排、生成。问故障、问步骤 / SOP 走多步排查智能体 |
| SOP 速查 | 手册、检查表、自编 SOP 里的完整流程，按原文顺序；可整理成一篇中文 SOP |
| 错误码速查 | 精确查报警码。现场写法 `STG-29-80` 换算成手册里的主码 / 细分码（目前只做 STG） |
| OVL 分析 | 上传 MESR：矢量图 / 曲线、数据表、文件头；叠对类做 10 参数拆分。文件只在这次使用，不入库 |
| Troubleshooting 表 | 按机型 → Unit 的故障经验。提炼出来的先「待确认」，管理员确认后才给其他人看、才被智能体引用 |
| 工作日志 | 实时读飞书上各现场的装机调试履历 |
| 知识库 | 上传、删除、看入库进度 |
| 审计 | 管理员：账号、资料权限、使用记录 |

另外 Claude 可以通过 MCP 的 13 个工具查库、看电路图、追线（README 第六节）。

问答模型是 **deepseek-flash**（DeepSeek-V4.1-Flash，`src/providers.py`）。旧名 `deepseek-v4-flash` 已下线，PR #10（2026-10-17）改成现在的名字。默认关掉后台思考（`DEEPSEEK_THINKING`）。大批量提炼不走 deepseek，走用户 Mac 上的 gemma4。

---

## 已经在用

### 引用能打开原文

2026-04-30 就进了网页（提交 `a768457`）。对话答完后，下面一条「参考来源」（`ui/app.py` 的 `_build_citation_buttons`）：

- **PDF**：新标签打开 `/serve-pdf?path=…#page=页码`，浏览器跳到引用那一页
- **Markdown / txt**：新标签打开 `/serve-md`，渲染成网页（OKF 的 frontmatter 做成元数据卡片）
- **Word、Excel 等其它格式**：只显示出处文字，点不开文件

这就是旧路线图里「工程师要能核对原文」要解决的事。左右分栏、页内嵌 PDF.js，没有做进现在的对话页，见文末的想法。

`ui/pdf_viewer.html`、`ui/pdf_renderer_page.html` 和 API 上的 `/pdf-viewer-renderer` 是当时留下的阅读器。Gradio 对话页没有把它们嵌进右侧栏（`ui/app.py` 读了渲染器模板，变量没有再用）。

### 多步排查智能体

`src/agent.py` 是 ReAct 智能体：自己决定查什么、查几轮（`AGENT_MAX_ITERATIONS`，默认 6 步），思考过程在对话里逐段显示（`ReActStreamParser`）。路由在 `src/router.py` 的 `should_use_agent`：问步骤一定走智能体；其余默认用语义判断，语义不可用时退回关键词。

工具（`src/tools.py`）：Troubleshooting 表、错误码、故障履历、操作步骤、关键词、语义、看电路图、线表追线（`netlist_trace`）、按线号交叉引用（`trace_connection`）。

文件头还写着「P1 骨架」，那是旧注释。这个智能体已经接在网页对话和 MCP 上。

旧路线图 V2.2 画的是另一回事：每一步给置信度、让人点选下一步、底部画「已经走过的路径」。那个点选向导没有做。现在是智能体查完，给一份带出处的结论；想继续问，就再发一条。

### 电路图和线表

已经在用的是「按编号找页、按接插件追线」，不是旧 V3.0 那张「上传照片 → 视觉模型 → 拓扑推理」的分层表。

- 电路图入库只写全文索引。后台 `nikon-circuit-recognize` 做 OCR 和矢量字形识读，把接插件号补进索引（`scripts/circuit_recognize.py`）
- S207D「已融合」版加 SMIF：905 页、8324 条连接，在 `data/netlist.db`（不进仓库）。抽线脚本 `scripts/netlist_*.py` 以后不再调用 Anthropic
- 网页智能体优先用 `netlist_trace`（`src/netlist_trace.py`，默认 2 跳）；没有编号时用 `trace_connection`（`src/net_trace.py`）
- MCP：`nikon_view_diagram` 按线号 / 板号 / 页号取页并渲染，`nikon_trace` 追线
- 看图（`src/figures.py` 的 `inspect_diagram`）对电路图返回文字层和原图链接。密集电路图不用小视觉模型整页认元件（会编元件号）

### 资料从哪来、夜里做什么

| 事 | 在哪 |
|---|---|
| WPS 每 30 分钟同步 | `scripts/sync_kwiki.py`（由 `sync_wps.py` 调用），`nikon-wps-sync.timer`。新增入库，改过的整篇替换，删掉的同步删 |
| 飞书知识库每 30 分钟同步 | `scripts/sync_feishu.py`，`nikon-feishu-sync.timer`。履历 / 日志按天入库 |
| 入库时定机型、定权限 | `src/machine_model.py`、`src/acl_classify.py` |
| 缩写先展开再检索 | `src/glossary.py`，人工表在 `config/glossary.tsv` |
| SOP 目录 | `src/sop_index.py`，库 `data/sop_index.db`。同步有增删改名时会重建 |
| 错误码原文 | `src/ingest_error_text.py`、`src/error_code.py`。现场 `STG-29-80` 换算；其它控制器的 8 位码可以搜原文，没有做成 XX-YY 换算 |
| 夜间提炼 | `src/kb_extract.py`、`src/local_llm.py`，定时器 `nikon-kb-extract.timer`（北京 00:00～06:30，每半小时试一次）。Mac 在线且空闲超过 10 分钟才跑 gemma4；Mac 不在就停，不退回 deepseek。结果先进 Troubleshooting 表，状态是待确认 |
| 原来的 deepseek 抽取 | `nikon-ts-extract.timer` 已停用。见 README 第八节 |

OVL 的解析和画图在另一个仓库 [ovl-analyzer](https://github.com/bk-ZOL/ovl-analyzer)。本仓库只有 `src/ovl_view.py` 去调用它。

### 多人同时提问

- PR #5（2026-10-17）：同一时间最多 3 个问题（`CHAT_CONCURRENCY`，对话入口共用这个名额）。全文库每个线程一条 SQLite 连接（`src/fulltext.py`）
- 智能体工具跑在别的线程里时，带上登录身份（`contextvars`：`src/agent.py`、`src/tools.py`、`src/util_timeout.py`）。不带的话检索会报没有身份
- PR #11（2026-11-02）：建 SQLite 连接时不再把没建好的连接放进全局，避免 `database is locked`。同一次还改了提炼：模型把字符串和列表给反了要能收、过了早上 6:30 不再顺延到第二天、讲故障的资料里 S308F 临时排到最前（`src/kb_extract.py` 的 `_TROUBLE_FIRST`，注释写明做完可删）、抽出的故障码去掉 HTML 换行

精排默认仍是 deepseek（`RERANK_PROVIDER=llm`）。2026-10-14 对比过硅基流动 `bge-reranker-v2-m3`，更快，但会被字面相近的词带偏，没有换。要试的话在 `.env` 设 `RERANK_PROVIDER=api`。

---

## 旧路线图里不再当任务的

| 旧条目（2026-04-26） | 现在 |
|---|---|
| **V2.1 引用一键跳原文，P0** | 已做。PDF 新标签打开并跳到页码，Markdown 渲染后打开。不要再立项 |
| V2.1 里的左右分栏、页内 PDF.js | 没进现在的对话页。阅读器文件还在，见下面的想法 |
| **V2.2 带置信度的逐步点选** | 没做。多步排查用的是上面的 ReAct 智能体，不按那张点选草图做 |
| **V3.0 上传故障照片，视觉模型匹配电路图** | 没做。电路图走编号索引、OCR / 矢量字形和线表 |
| **V3.0 按电路拓扑做故障传播** | 没做。已有的是线表追线（接插件 → 线缆 → 对端） |
| 做成通用半导体文档平台、Open Core 开源 | 没有开工，也没有排期 |

---

## 想法（没有排期，没有人在做）

下面只是以后可以考虑的方向，**不是承诺，也不是当前迭代**。没写进这里的，默认不做。

1. **对话旁边并排看原文。** 旧 V2.1 草图是左边对话、右边嵌 PDF。现在是新标签打开。Word、Excel 的引用还点不开文件。页内预览如果要做，得单独定，不要把它和「引用已经能打开」混成一件未完成的 P0。
2. **OVL 接进智能体。** 现在只做网页上传：不标超标，不按超标项推荐 SOP 或现场经验（CLAUDE.md 8.9，用户当时定的范围）。
3. **Troubleshooting 待确认更好审。** 管理员现在可以按「待确认」筛选，在折叠表单里改一条再保存（`ui/app.py` 的编辑 / 确认）。没有单独的待审队列，也不能一批确认。网页上「立即抽取新资料」仍去启动已停用的 `nikon-ts-extract.service`；夜里实际跑的是 `nikon-kb-extract`。按钮和定时任务不是一条路。
4. **SOP 速查按模块筛**（WS / RS / WL / AIS / LC）。夜间提炼会给流程分模块，速查页只有机型和关键词。系统介绍稿的「下一步」里写过，页面上没有。
5. **补缩写。** `scripts/build_glossary.py --missing` 能列出常用但没收录的（介绍稿点过 TPR、RB、INC）。这是往 `config/glossary.tsv` 里补行，不是新功能。
6. **只有截图的资料。** `拆机SOP（S207）` 这类几乎没有文字层，检索和 SOP 拆分都拿不到步骤（CLAUDE.md 待办 7）。要对截图做 OCR 才搜得到。
7. **同步和向量化偏慢。** WPS 每 30 分钟把两千多个文件走一遍；向量化是一条一条请求（CLAUDE.md 待办 11）。
8. **`7400-2.pdf` 文件是坏的**，需要在 WPS 里另存一份再入库。这是资料问题，不是代码任务。

---

## 相关文档怎么读

| 文档 | 当什么用 |
|---|---|
| [README.md](README.md) / [README.en.md](README.en.md) | 给人看的现状，2026-10-17。中文是正文 |
| [CLAUDE.md](CLAUDE.md) | 2026-10 前后每次改动的原因、服务器上的坑。待办 1～8 已勾完；9～11 仍是资料或运维上的缺口 |
| [`docs/system_overview.html`](docs/system_overview.html)、`docs/Nikon Expert 系统介绍.docx` | 对外介绍。规模数字和 README 同一天 |
| `Nikon_Expert_系统实现方案_V2.3.docx` | 2026-03 的设计稿。和现在的云端问答、Mac 本地提炼不一致 |
| [`deploy/systemd/README.md`](deploy/systemd/README.md) | 仓库里有单元文件的定时任务。夜间提炼用 `nikon-kb-extract`，不要再启用 `nikon-ts-extract` |
