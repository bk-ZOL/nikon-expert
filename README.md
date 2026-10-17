# Nikon Expert — 光刻机现场知识系统

**简体中文** | [English](README.en.md)

> 给 Nikon NSR 光刻机（以 S207D / S307E 为主，兼顾 S205C / S206D / S308F / SF 系列）现场工程师用的知识系统：
> 问故障、查报警、找操作步骤、追接线，回答都带出处。资料来自 WPS、飞书和本地文件，自动同步、自动提炼。

更新：2026-10-17。改动的来龙去脉见 [CLAUDE.md](CLAUDE.md)（交接说明），后续计划见 [ROADMAP.md](ROADMAP.md)。

---

## 一、能做什么

| 入口 | 用来干什么 |
|---|---|
| **对话** | 直接问。简单的问题走快速检索；问故障、问「怎么做 / 步骤 / SOP」的会走**多步排查智能体**，自己决定查哪些资料、查几轮，回答每一步都标出处 |
| **SOP 速查** | 输入要做的作业（「AIS 电压调整」「wafer loader 机械手清洁」），列出手册、检查表、自编 SOP 里的完整操作流程（按原文顺序），可以一键整理成中文 SOP |
| **错误码速查** | 精确查报警码；现场写法 `STG-29-80` 会自动换算成手册里的主码 / 细分码 |
| **OVL 分析** | 上传 MESR 测量文件：晶圆矢量图 / 曲线、数据表、文件头；叠对类数据做 10 参数拆分（晶圆级 Tx Ty Sx Sy Rw Ow + Shot 内 tx ty sx sy rs os）和残差图。文件只在这次使用，不入库 |
| **Troubleshooting 表** | 按机型 → Unit 归类的故障经验：原厂对策、现场验证有效的措施、发生次数、走过的弯路。提炼出来的条目先「待确认」，管理员确认后才给其他人看、才被智能体引用 |
| **工作日志** | 实时读飞书上各现场的装机调试履历 |
| **知识库** | 上传、删除资料，看入库进度 |
| **Claude（MCP）** | 13 个工具，在 Claude 里也能查库、看电路图、追线（见第六节） |

数据规模（2026-10-17）：资料 700 份（手册 443、检查表 119、自编 SOP 93、电路图 16 本、错误码原文 18、故障履历、工作日志），向量 22.9 万条，SOP 目录 3709 个流程，缩写对照 219 个，Troubleshooting 表 2375 条，S207D 线表 8324 条连接。

---

## 二、总体流程

```
资料来源                         入库（自动）                       索引 / 提炼结果                      使用
──────────                     ─────────────                     ──────────────                     ──────
WPS 知识库 ──每 30 分钟──┐                                     ┌→ 全文索引（关键词、报警码）
 （手册、表格、电路图）    │                                     ├→ 向量库（语义检索，bge-m3）        ┌→ 网页：对话 / SOP 速查 /
飞书知识库 ──每 30 分钟──┼→ 按格式解析 → 定机型 → 定权限 ──→ ├→ SOP 目录（完整操作流程）  ──→   │   错误码 / TS 表 / 工作日志
 （履历、SOP、知识整理）  │   （PDF/Word/Excel/md，             ├→ 缩写对照表                      ├→ Claude（MCP 工具）
本地文件 ──手动一次性────┘    截图 OCR，电路图识图）            ├→ 电路图编号索引 + 线表           └→ 智能体（多步排查）
                                                               └→ Troubleshooting 表 ←─ 夜间提炼（Mac 本地模型）
```

给别人介绍用的版本（全景思维导图、资料流转、提问流程、保密规矩、每天自动做的事）见 [`docs/system_overview.html`](docs/system_overview.html)，用浏览器打开即可。

---

## 三、资料从哪来、怎么进库

**分工（用户定）：自己写、持续更新的放飞书；手册、PDF、表格、电路图等文件放 WPS。**

| 来源 | 同步 | 说明 |
|---|---|---|
| WPS 知识库 | `nikon-wps-sync.timer` 每 30 分钟 | 新增全部入库，改过的整篇替换旧版（不新旧并存），WPS 删掉的同步删除。脚本 `scripts/sync_wps.py` → `sync_kwiki.py` |
| 飞书知识库「NIKON NSR 205/206/207」 | `nikon-feishu-sync.timer` 每 30 分钟 | 整个知识库：标题带「履历 / 日志」的按天入库，SOP 进 SOP 速查，其他按资料入库；只重读最后编辑时间变了的。脚本 `scripts/sync_feishu.py` |
| 本地文件 | 手动 | `scripts/ingest_local.py`：先按内容指纹、文件名去重，再直接入库（2026-10-17 把 Mac 上 `02_设备技术资料` 里库里没有的 297 份入了库） |

入库时做的事：
- **解析**：PDF（含截图 OCR）、Word、Excel、markdown；老格式 `.xls/.doc` 先用 LibreOffice 转换
- **定机型**：先读封面和正文（Nikon 文档编号、机型字样），判断不出再看目录和文件名
- **定权限**：按类别、密级、机型、客户自动定级（`src/acl_classify.py`），客户资料只给有该客户权限的人看
- **电路图**：只写全文索引，后台再做 OCR 加矢量字形识读，把接插件号补进索引（`nikon-circuit-recognize`）
- **入库后自动刷新** SOP 目录和缩写对照表

---

## 四、问答是怎么工作的

```
提问 ─→ 路由：问步骤 / 故障 / 复合问题？
         ├─ 否 → 快速检索：全文 + 语义双路召回 → 机型加权 → 精排 → 生成回答
         └─ 是 → 多步排查智能体（ReAct）：想一步、查一步，直到信息够了再回答
                  工具：Troubleshooting 表 / 错误码 / 操作步骤目录 / 关键词 / 语义 /
                        故障履历 / 看电路图 / 线表追线 / 按线号交叉引用
```

- **缩写先展开**：问题里的缩写按对照表补上全称一起查（RR → Reticle Rotation / レチクルローテーション / 掩模旋转）。对照表从资料里自动挖（「全称 (缩写)」写法和现成的对照表），人工补充写在 `config/glossary.tsv`，改了下次提问就生效
- **机型**：问题里点名了机型就优先该机型的资料；没点名时 S207D 优先
- **多人同时提问**：同一时间最多 3 个问题一起处理（`CHAT_CONCURRENCY`）
- **出处**：每个结论标来源文件 / 章节 / 页码；客户名在显示时脱敏

---

## 五、夜间提炼（资料不出门）

`nikon-kb-extract.timer`：北京时间 00:00～06:30，每半小时检查一次，**用户 Mac 在线并且空闲 10 分钟以上**才开始，用 Mac 上的本地模型（gemma4，经 Tailscale 调用）提炼：

1. **现场履历 / 工作日志** → Troubleshooting 表的现场案例（根因、真正解决问题的措施、走过的弯路）
2. **手册 / 日文检查表** → 原厂条目（异常 → 原因 / 处理 / 判定标准）。专门讲故障的资料（エラー要因分析、REPAIR MANUAL 等）最先做
3. **SOP 目录** → 按模块（WS / RS / WL / AIS / LC …）分类

可以中断、接着做；**同样内容只提炼一次**，持续更新的履历按天认，只提炼新增和改过的天。结果全部先「待确认」。

---

## 六、Claude 接入（MCP）

`nikon-mcp.service`（streamable-http）；本地 Claude Code 也可以用 stdio 版 `mcp_server.py`。

| 工具 | 用途 |
|---|---|
| `nikon_query` / `nikon_search` / `nikon_search_error_code` | 问答 / 关键词搜索 / 精确查报警码 |
| `nikon_search_sop` | 查完整操作流程（可选整理成中文） |
| `nikon_view_diagram` / `nikon_reindex_diagrams` | 电路图按线号、板号、页号取页，高清渲染 |
| `nikon_trace` | S207D 线表追线（接插件 → 线缆 → 对端，可追几跳） |
| `nikon_query_worklog` | 实时读飞书履历（可按现场筛） |
| `nikon_ask_wps` | 直接问 WPS 自带的知识库问答 |
| `nikon_ingest` / `nikon_list_documents` / `nikon_delete_document` / `nikon_get_status` | 资料管理 |

---

## 七、用云端模型的规矩（涉及资料保密）

| 环节 | 用什么 | 说明 |
|---|---|---|
| 网页问答、整理一篇 SOP | deepseek-v4-flash（云端） | 每次只发检索到的几段原文；默认关掉后台思考（`DEEPSEEK_THINKING`） |
| 向量化 | 硅基流动 bge-m3（云端，免费） | 条款写明不用于训练、推理完立即销毁 |
| **大批量提炼、抽取** | **用户 Mac 上的 gemma4（本地）** | 一律不用云端；Mac 不可用就停，不会退回云端（`TS_LLM=local`） |
| Anthropic API | 不用 | 条款不允许中国控股公司使用（以前的线表抽取脚本 `netlist_*` 不再用） |

---

## 八、部署与运维

服务器（Vultr，AlmaLinux 8，2 核 3.6G，纯 CPU），项目在 `/opt/nikon-expert`。连接方式、各种坑见 CLAUDE.md 第 1、2 节。

| systemd | 作用 |
|---|---|
| `nikon-expert.service` | 网页（Gradio，端口 7860，个人账号登录、权限分层） |
| `nikon-mcp.service` | MCP 服务 |
| `nikon-wps-sync.timer` / `nikon-feishu-sync.timer` | WPS / 飞书同步，各 30 分钟 |
| `nikon-feishu-refresh.timer` | 每天刷新飞书 token |
| `nikon-circuit-recognize.timer` | 新电路图识图（入库时触发，每天 03:30 兜底） |
| `nikon-kb-extract.timer` | 夜间本地提炼 |
| ~~`nikon-ts-extract.timer`~~ | 已停用（原来用 deepseek 抽取，改由 kb-extract 做） |

存储：Qdrant（`nikon_expert_v1`）、全文索引 `data/fts.db`、SOP 目录 `data/sop_index.db`、Troubleshooting 表 `data/ts_table.db`、线表 `data/netlist.db`、缩写 `data/glossary.json`。`.env`、`data/` 不进仓库。

Mac 端：Ollama 0.40.4（`~/Applications/ollama-0.40.4`，LaunchAgent `com.jayce.ollama-tailnet`，只监听 Tailscale 地址），模型 gemma4。

**OVL 分析**用的是另一个仓库 [ovl-analyzer](https://github.com/bk-ZOL/ovl-analyzer) 的代码，服务器上放在 `/opt/ovl-analyzer`（也可以用 `OVL_ANALYZER_PATH` 指定）。改了 ovl-analyzer 要同步过去：`rsync -a --delete --exclude __pycache__ ~/ovl-analyzer/ovl_analyzer/ root@服务器:/opt/ovl-analyzer/ovl_analyzer/`。

**改代码的规矩**：先在开发分支 `feat/feishu-worklog` 改、提交、推送，再传到服务器，改完开 PR 合并进 `main`；不直接在服务器上改。重启服务尽量放在夜里（2:00～5:00）。

常用命令（在服务器 `/opt/nikon-expert` 下）：

```bash
.venv/bin/python scripts/build_sop_index.py --show "AIS 电压调整"   # 看 SOP 速查会查到什么
.venv/bin/python scripts/build_glossary.py --show RR                # 看某个缩写怎么展开
.venv/bin/python scripts/build_glossary.py --missing                # 常用但对照表里没有的缩写
.venv/bin/python scripts/kb_extract.py --status                     # 夜间提炼进度
journalctl -u nikon-feishu-sync -n 50                               # 最近一次飞书同步
```

---

## 九、主要代码

```
src/
  engine.py          问答主流程（路由、双路检索、精排、生成；智能体入口）
  agent.py / tools.py 多步排查智能体及其工具
  router.py          路由、机型、意图判断
  fulltext.py        全文索引（SQLite FTS5 trigram）
  sop_index.py       SOP 目录（拆流程、检索、整理成中文）
  glossary.py        缩写对照表
  ts_table.py        Troubleshooting 表（抽取、归并、视图）
  kb_extract.py      夜间提炼；local_llm.py 调 Mac 本地模型
  ingestor.py / ingest_docx.py / ingest_worklog.py   入库
  machine_model.py   机型判断；acl*.py 权限
  net_trace.py / netlist_trace.py / vector_glyph.py   电路图追线、矢量字形识读
  ovl_view.py        OVL 分析页（解析、画图、10 参数都调用 ovl-analyzer 的代码）
ui/app.py            网页
mcp_server.py / mcp_tools_extra.py / mcp_http_server.py   MCP
scripts/             同步、入库、建索引、提炼等脚本
deploy/systemd/      部分 systemd 配置
config/glossary.tsv  人工维护的缩写对照
```
