# Nikon Expert · 交接说明（给 Claude Code）

> 写于 2026-10-12。这份文档记录了 2026-10-07 ~ 10-12 在 claude.ai 对话里做的改动、做这些改动的理由，以及还没做完的事。开工前先读完。

## 1. 环境

| 项 | 说明 |
|---|---|
| 本地仓库 | `/Users/jayce/nikon-expert`，分支 `feat/feishu-worklog`（main 落后很多，还没合并） |
| 服务器 | Vultr，AlmaLinux 8，3.6G 内存，**纯 CPU**。经 Tailscale 访问：`root@100.93.22.104`，项目在 `/opt/nikon-expert`（**不是 git 仓库**） |
| ssh 写法 | `env -u ALL_PROXY -u all_proxy ssh root@100.93.22.104 ...`（本机有 Clash 代理，不去掉会连不上） |
| 服务 | `nikon-expert.service`（Gradio 网页，端口 7860）、`nikon-mcp.service`（MCP，streamable-http） |
| 定时器 | `nikon-wps-sync.timer`（每 30 分钟同步 WPS kwiki → 本地库）、`nikon-feishu-refresh.timer`（每天刷新飞书 token） |
| 向量化 | 硅基流动 `BAAI/bge-m3`（**免费**，余额为 0 也能用）。`EMBED_API_KEY` 在服务器 `.env` |
| 问答 LLM | deepseek-v4-flash |
| 存储 | Qdrant `nikon_expert_v1`（本机 6333）、全文索引 `data/fts.db`、线表 `data/netlist.db` |

## 2. 工作规矩（这几天踩过的坑）

- **先改本地、提交推送，再 scp 到服务器。** 不要直接在服务器上改代码。之前就是服务器和仓库各改各的，差点被一次部署整个覆盖掉。
- 服务器上 `.env`、`data/`、`*.bak*` 不进仓库（已在 `.gitignore`）。
- **`pgrep -f` / `pkill -f` 会匹配到自己。** ssh 远程执行的命令行里如果带着要找的字符串，就会把自己也算进去。用 `'netlist_pilo[t]'` 这种写法，或者用 `^` 锚定行首。
- **`cd 目录 && 任务A & 任务B &`：任务B 不在那个目录里执行。** 要么先单独 `cd`，要么每个任务都写绝对路径。
- zsh 下 `--include=*.py` 要加引号。
- 后台任务用 `nohup ... > /tmp/xxx.log 2>&1 &`，Python 加 `PYTHONUNBUFFERED=1`，不然日志要等进程结束才写。
- systemd 日志时间是 **UTC**（北京时间减 8 小时）。
- 同步进程运行中**不要改 `.sync_state.json`**：它每处理一个文件就把内存里的整份 state 写回去，会覆盖你的修改。要改就先 `systemctl stop nikon-wps-sync.timer nikon-wps-sync.service`。

## 3. WPS → 本地库同步（`scripts/sync_kwiki.py`，由 `sync_wps.py` 调用）

用户定的方针：
- WPS 里有、库里没有的**全部入库**；正在修改中的资料也入库，但**改了以后必须替换旧版，不能新旧并存**。
- 驱动、视频这类非文档文件不入库。

这次修好的问题：
1. **标题里带"."的文件一直没入库**（`Rev. 2.0` 被当成扩展名，166 份手册受影响，错误码列表也在其中）。现在按 `KNOWN_EXTS` 判断，不认识的扩展名就按文件头补上。
2. 摄入失败或入库后 0 chunk，不再记成已同步；同一版本连续失败 3 次（`SYNC_MAX_FAILS`）才放弃。
3. 可入库文档的大小上限是 300MB（`MAX_DOC_MB`），50MB（`MAX_FILE_MB`）只管视频等不入库的文件。
4. 改名：先入新名字，成功后再删旧名字。WPS 里删掉的文件同步删库（单轮上限 `SYNC_MAX_DELETE`，**同名文件还有别的副本时不删**）。
5. 跳过规则：`［Driver］` 路径、`SE306(FSE &Training+Manual)` 原件（已手工切成 3 份入库）、`S207DS307E Electrical Circuit Diagram Vol.1~5` 单册版和 `S307E-RF-ELCIRC-R10-B1-*`（库里保留"已融合"版，内容是同一套图）。
6. 老 Office 格式（改了扩展名的 .xls/.doc，OLE 文件头）入库前用 LibreOffice 转换（`_convert_legacy_office`）。
7. 入库改走云端 bge-m3（原来在服务器 CPU 上本地算，一块要 5~7 秒）。
8. `_delete_existing` 现在同时清 FTS（以前只清 Qdrant，FTS 会残留旧版本）。
9. 电路图 PDF 走 `ingest_circuit_pdf`，**只写全文索引，不写向量库**（这是原本就有的设计），所以 0 chunk 检查要跳过电路图。

## 4. 机型标签（`src/machine_model.py`）

- 标签按"族"：`S207D`（S207D/S307E）、`S206D`（S205C/S206D/S305B/S306C/S306D）、`SF100`，以及 `COMMON`（"光刻原理"等通用资料，`COMMON_DIRS` 可配置）。
- 入库时按 WPS 路径上的机型目录推断，同名文件在多个机型目录里就合并标签；推断不出来再看文件名，最后默认 `S207D`。
- 检索时加权（`router.merge_results` 外面包了一层）：问题里点名了机型就优先它；没点名时 S207D ×1.0、S206D ×0.8、其他 ×0.5。**只降权，不过滤**；COMMON 和多机型文档不降权。
- 存量数据用 `scripts/retag_models.py --apply` 补过一遍（304 份）。Qdrant 的 `doc_name` 字段已建索引。
- **入库时先读内容判断机型**（2026-10-12 上线，`infer_model_for_file`）：封面（PDF 前 2 页 / 文档开头 3000 字）写的机型和 Nikon 文档编号（如 `S307E-RF-…`）优先；没有就数正文抽样里各机型出现的次数，取最多的，达到最多者 30% 的也一起标上。抽不出字（扫描件）或证据太少，就退回路径/文件名规则。通用资料目录仍标 COMMON。同步日志里会打印每份文件的判断依据。
- 存量文档**还没按内容重新打标签**。`scripts/retag_models.py --content` 预览显示 304 份里有 50 份会变：大多数是 MCSV/GOCS 这类封面列了多个机型的通用文档，从单机型变成多机型。有 7 份会去掉 S207D：SE306 培训手册 3 份 → S206D；LC-X4、SD1/SD3-EXX4-INTEL → S206D；レチクル設計ガイド → SF100。**等用户确认后再加 `--apply`。**

## 5. 电路图 OCR

- 电路图上竖排的接插件号、针脚号、基板名是 CAD 笔画字，不在 PDF 文字层里。`ingest_circuit_pdf` 现在每页额外做一遍旋转 270° 的 Tesseract OCR（`_ocr_page_tokens`），把识别出的编号追加到这一页的文字里（`【OCR标签】...`），再进倒排索引。
- `diagram_config.py` 加了 J/P 接插件规则（`J1394G`、`P1024`）。
- 已重做 S206D Vol.1~4、两份 SMIF Loader。重建索引：`scripts/build_diagram_index.py --full`。
- 服务器已装 tesseract（chi_sim/eng/jpn）和 LibreOffice，`.env` 里有 `TESSDATA_PREFIX`。

## 6. 线表（核心成果）

- 脚本：`scripts/netlist_build.py`（正式批量）、`netlist_pilot.py`（提示词、渲染、解析）、`netlist_claude.py`（Claude 对比）。
- 范围：S207D 的"已融合"版电路图（`data/` 下手工放置）+ S207D S307E SMIF Loader，共 905 页（`diagram_pages.kind` 为 `wiring` 或 `general_wiring`）。
- 结果在 `data/netlist.db`：`pages`（每页状态、原始输出、token）、`edges`（每条连接：线缆号，两端各自的接插件、对插件、设备、型号、单元号，针脚）。**目前 905 页全部成功，共 8324 条连接。**
- **模型选型（对着原图核对过后定的）：**

  | 页面 | 模型 | 理由 |
  |---|---|---|
  | `wiring`（631 页） | Claude Haiku（Batch） | 第 2853 页 8/8 全对；Qwen3.5-122B 只对六成 |
  | `general_wiring`（274 页） | Claude Sonnet（Batch） | 接地图、筐体内部图这类复杂页只有它靠得住（第 4142 页约 85~90%） |

  Qwen3-VL-32B、Qwen3.5-122B 都测过，复杂页面上配错得多，不用。
- 注意：新 Claude 模型**不支持 `temperature` 参数**；硅基流动余额不足返回 402，频率限制返回 429。
- 用户对**费用敏感**。抽线表是一次性的，以后只对新入库的电路图跑增量。

## 7. 追线工具 `nikon_trace`（2026-10-12 已部署、验证，已接进网页智能体）

- 逻辑在 `src/netlist_trace.py`，在 `mcp_tools_extra.py` 里注册成 MCP 工具。部署脚本是 `ne_task2_trace.sh`（在用户的 Downloads 里），也可以照它手动做：scp 两个文件 → 服务器上实测 → `systemctl restart nikon-mcp`。
- 规则：J 与 P 同号视为对插（`J7554A≡P7554A`）；只有 J/P + 4 位数字这种全局唯一的编号才跨页追，CN/TB/PS 这类板上局部编号只在同一页内连接。
- **验证用例**（第 2853 页，已对过原图）：`J7544B` 应该追到 `4S095-884` → J7554A 45~47 针，再从 P7554A 经 `4S095-890` → IOP2 上部中继面板。
- 一个字段里写了一组全局接插件号的连接（如第 4142 页 `J7544B / J7602H / J7544C …`，共 233 条），照常显示，但**不从这些号往外追**，不然会串到 SPURE Turret 等无关单元。conn 和 mate 分开判断，因为 `P1028A` 插 `J1113A` 这种不同号的对插是正常的。
- 网页智能体（`src/tools.py`）里的工具名是 `netlist_trace`，默认追 2 跳。有接插件号或线缆号时优先用它，原来的 `trace_connection` 留作后备。这个工具的结果**不进引用列表**（来源页码只写在文字里）。

## 8. 待办（按优先级）

1. ~~部署、验证 `nikon_trace`，接进网页界面~~（2026-10-12 完成）
2. ~~按内容判断机型~~（2026-10-12 新入库已生效；存量要不要重新打标签，等用户确认，见第 4 节）
3. `net_trace.py` 改成从全文索引读文字，OCR 补出的编号也能用。
4. 错误码格式换算：现场写的 `STG-29-80` 要能对上手册里的 `#29H` / `Detail 80H`（STG 错误码原文在 WPS 的 `e_st_0540.txt`）。
5. 网页右上角的向量数只在启动时算一次，改成实时查。
6. 重排序：现在用 deepseek 做（只看每段前 180 个字）。可以换成硅基流动的 rerank 接口（如 `Qwen3-VL-Reranker-8B` / `bge-reranker-v2-m3`）。`engine.py` 里旧的本地 reranker 初始化会报一条无害的警告，可以清掉。
7. 38 个老格式文件（.xls/.doc）：转换逻辑已加，要确认它们重跑后真的入了库。很多是截图，可能抽不出几个字。
8. 库里还有 18 份驱动说明书（Windows/Mac/Linux 安装指南、PL2303 等），**等用户确认后再删**。
9. `7400-2.pdf` 文件损坏，需要用户在 WPS 里另存一份。
10. WPS 里的 SF100 文件夹被删了（state 里那 62 条已移除，库里的数据保留）。以后可能会入库其他机型，但**优先处理自家机型**。
11. 定时同步每 30 分钟都要完整遍历一遍 WPS（2600 多个文件），慢；向量化是串行请求，批量导入时可以考虑并发。

## 9. 用户的习惯

- 中文交流，不喜欢"AI 味"，回答要口语、直白。
- 用户是 S207 平台的技术负责人，不是专职程序员：命令要能直接复制执行，结果要说清楚意味着什么。
- 删除数据、重启服务、改 state 这类操作，先说明再做。
- Nikon 图纸是 Confidential。只有线表这件事经用户同意发给了 Claude API；其他数据外发要先问。
