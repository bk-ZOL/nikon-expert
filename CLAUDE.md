# Nikon Expert · 交接说明（给 Claude Code）

> 写于 2026-10-12。这份文档记录了 2026-10-07 ~ 10-12 在 claude.ai 对话里做的改动、做这些改动的理由，以及还没做完的事。开工前先读完。

## 1. 环境

| 项 | 说明 |
|---|---|
| 本地仓库 | `/Users/jayce/nikon-expert`，开发分支 `feat/feishu-worklog`，改完开 PR 合并进 `main`（2026-10-17 PR #5 后两边一致） |
| 服务器 | Vultr，AlmaLinux 8，3.6G 内存，**纯 CPU**。经 Tailscale 访问：`root@100.93.22.104`，项目在 `/opt/nikon-expert`（**不是 git 仓库**） |
| ssh 写法 | `env -u ALL_PROXY -u all_proxy ssh root@100.93.22.104 ...`（本机有 Clash 代理，不去掉会连不上） |
| 服务 | `nikon-expert.service`（Gradio 网页，端口 7860）、`nikon-mcp.service`（MCP，streamable-http） |
| 定时器 | `nikon-wps-sync.timer`（每 30 分钟同步 WPS kwiki → 本地库）、`nikon-feishu-refresh.timer`（每天刷新飞书 token） |
| 向量化 | 硅基流动 `BAAI/bge-m3`（**免费**，余额为 0 也能用）。`EMBED_API_KEY` 在服务器 `.env` |
| 问答 LLM | deepseek-flash（DeepSeek-V4.1-Flash；旧名 deepseek-v4-flash 已下线，2026-10-18 改名） |
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
- 存量 304 份已按内容重打标签（2026-10-12，`retag_models.py --content --apply`），50 份有变化。LC-X4、SD1/SD3-EXX4-INTEL、レチクル設計ガイド 这 4 份内容判断不出 S207D，但现场可能在用，手工补上了 S207D（`S207D,S206D` / `S207D,SF100`）。**这 4 份以后如果在 WPS 里被修改、重新入库，补上的 S207D 会丢**，要再补一次。改之前的标签备份在服务器 `data/machine_model_backup_20261012.json`。

## 5. 电路图 OCR

- 电路图上竖排的接插件号、针脚号、基板名是 CAD 笔画字，不在 PDF 文字层里。`ingest_circuit_pdf` 现在每页额外做一遍旋转 270° 的 Tesseract OCR（`_ocr_page_tokens`），把识别出的编号追加到这一页的文字里（`【OCR标签】...`），再进倒排索引。
- `diagram_config.py` 加了 J/P 接插件规则（`J1394G`、`P1024`）。
- 已重做 S206D Vol.1~4、两份 SMIF Loader。重建索引：`scripts/build_diagram_index.py --full`。
- 服务器已装 tesseract（chi_sim/eng/jpn）和 LibreOffice，`.env` 里有 `TESSDATA_PREFIX`。
- **`net_trace.py`（网页的 `trace_connection`）2026-10-12 改过**：
  - 页面文字从全文索引读，OCR 编号能参与交叉引用，结果里标"（OCR）"。
  - 交叉引用范围是同机型的所有电路图（S206D 4 卷 + SMIF），所查编号的页排最前。
  - **Nikon 图纸在中继面板等处常把接插件号写成不带前缀的 `2613C`**（就是 P2613C）。现在 4 位数字 + 1 个字母的编号统一按 J/P 号处理；`7214A2` 这类网络号不变。
- **S207D 已融合版补 OCR**（2026-10-12 开跑，10-13 跑完并已重建编号索引，`scripts/ocr_circuit_pages.py`）：不重新入库，只往全文索引每页文字后追加 `【OCR标签】`。5180 页里新识别 2207 页、复用 2973 页，在服务器上用了 18.5 小时。效果以线表为准：630 页、8585 个接插件号，识别率从文字层的 0% 提到 61%。OCR 认出的 19690 个号里，64% 线表里有，908 个是形近字母认错（B→8、C→0），31% 对不上（板子图上的真号和噪声混在一起）。**10-13 已用 `scripts/fix_ocr_tokens.py` 纠正**：末位数字换成形近字母（8→B、0→C/D/O、4→A 等），能唯一对上已知号（线表 + 文字层 + 两张以上图上都认出过的 OCR 号；有多种可能时看同一页）才改，共改 1350 个号、506 页，原 OCR 段备份在 `data/ocr_tokens_backup_20261013_022622.json`。纠正后识别率 65%，OCR 号里线表能找到的 69%。还有 836 个 5 位纯数字定不下来（如 J22330：2233C 和 2233D 都可能），没改。**服务器只有 2 核，以后大批量 OCR 建议放到 Mac（M4 10 核）上跑**，同样设置单页 4~9 秒，比服务器快 20 倍以上。
  - 设置：300 dpi，270° psm 11（竖排）+ 0° psm 11 + 0° psm 6（横排）。实测 300 dpi 比 400 dpi 认得多、还快；只做 270° 时，第 70 页这种横排标注的页一个编号都认不出来。每页约 35 秒。
  - 按低分辨率渲染图的指纹去重，重复页复用结果；进度存在服务器 `data/ocr_progress_aefde4c1.json`，可以随时 kill，再跑会接着做。
  - 后台命令：`nohup nice -n 19 .venv/bin/python scripts/ocr_circuit_pages.py --doc 已融合 > /tmp/ocr_s207d.log 2>&1 &`，看进度：`grep -v Warning /tmp/ocr_s207d.log | tail`。
  - **跑完后要重建编号索引**：`.venv/bin/python scripts/build_diagram_index.py --full`。
  - OCR 会把 B 认成 8（`J75448` 实为 J7544B），结果里已标"（OCR）"提醒核对。
  - 入库时的默认 OCR 设置（`CIRCUIT_OCR_DPI=400`、`CIRCUIT_OCR_ROTATIONS=270`）没改，以后新入库的电路图可以考虑也换成这套。
- **矢量字形识读**（2026-10-13，`src/vector_glyph.py`）：图上的 CAD 笔画字在 PDF 里是矢量短线段，同一个字符每次画出来的笔画形状一样，所以不经过 OCR，直接从线条还原。做法：线段拼字 → 字拼词（横排/竖排；竖排是逆时针转 90°、从下往上读）→ 按字高转正成 18×18 网格 → 和模板比。只和"洞数"相同的模板比（E 没有洞、6 有 1 个、B/8 有 2 个），B 和 8、D 和 0 再看左边是否有整根竖线。
  - 模板 `src/glyph_templates.npz`（310 个，只是字母数字的形状）是用线表自动标注学出来的：`scripts/vector_glyph_train.py`。线表页按内容分组，一半学习、一半考试。
  - 效果：在 331 张没参与学习的考试页上，识别率 67%（OCR 57.5%），读出的号里线表能找到的 73%（OCR 70%）。
  - **已合并进全文索引**：`scripts/vector_glyph_read.py read`（在 Mac 上跑，整本 41 分钟）→ `apply`（在服务器上跑），和 OCR 结果取并集写进 `【OCR标签】` 段。原段备份在 `data/vector_glyph_backup_20261013_043309.json`。合并后，有线表的 648 页上识别率 **84%**（只用 OCR 时 65%），识图得到的号里线表能找到的 68%。追线结果里这类号标"（识图）"。
  - 线表不全（`J7405` 带分开标注的 A/B/C、只标了一侧的对插），所以按线表算的识别率和准确度都是偏低估计。
  - 接插件号规则改成允许字母后再带一位数字（`J7214A2`），`diagram_config` 和 `net_trace` 都改了。
  - `net_trace`（网页的 `trace_connection`）交叉引用已区分 S207D/S307E（10-13，规则和线表追线一样），结果标〔S207D〕/〔S307E〕，跳过的另一机型页数单独说明。注意：起点页按机型优先选 S207D，所以查两种机型都有的号（如 J7544E）时，只会看到 S207D 那一边。
  - **按接插件号格式校正**（10-13 第二版）：J/P 后 4 位一定是数字，形近字母换回数字（S206D 第 3 卷的 0 中间带一笔、左边是直的，会被当成 D）；开头认不出时按洞数定 J（无洞）/ P（一个洞）（第 3 卷的 J 没有顶上那一横）。
  - **7 本电路图都已合并**（10-13 第二版）：S207D 主图册（用 `--restore` 先去掉第一版结果）、S207D SMIF、S206D SMIF、S206D 4 卷。效果：
    - S207D 主图册：考试页识别率 83.6%；有线表的 648 页整体识别率 **90%**，识图得到的号里线表能找到的 67%；
    - S207D SMIF：和线表比 **96%**；
    - S206D 5 本没有线表，用 J/P 配对率检验：读出的号能在别处找到配对的占 73~87%，和 S207D（79~82%）相当。
  - **备份**：`data/vector_glyph_backup_20261013_071800.json`（S207D 主图册）、`_071802`（S206D SMIF）、`_071803`（S206D 第 4 卷）是原始备份。另外 4 本的备份因为文件名只精确到秒被覆盖了（脚本已修），按合并前导出的原始 OCR 接插件号重建为 `vector_glyph_backup_20261013_0718_重建_<指纹>.json`，其中线号等非接插件号有一部分分不清是不是 OCR 原有的，按原有保留。
- **S207D 已融合版在全文索引里记的 `file_path` 是 Mac 上的 `/Users/jayce/Downloads/…`**，服务器上不存在。这导致网页的"看图"和 `trace_connection` 以前在 S207D 主图册上一直查不到。现在 `figures._circuit_pages` 在路径不存在时按文件名到 `data/` 下找，已经能查到。库里的路径也已改正（2026-10-12，`scripts/fix_file_paths.py --apply`，全文索引和向量库一起改，旧路径备份在服务器 `data/file_path_backup_20261012_060920.json`）。同时改了"北京厂内 207调试履历.docx"：WPS 里文件夹从"装机调试履历"改名成了"装机调试报告"，同步按文件 id 判断没变，不会重新入库，路径就留在了旧文件夹。**以后 WPS 里文件夹改名都会出现这种情况**，可以定期跑一次这个脚本（默认只预览）。`CD-SEM 机台操作SOP-CG6300i _v2.md` 是 4 月在网页上传的，原文件已经不在了，路径没法改，内容照常能搜到，只是打不开原文。

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
- 规则：J 与 P 同号视为对插（`J7554A≡P7554A`）；J/P + 4 位数字的编号跨页追，CN/TB/PS 这类板上局部编号只在同一页内连接。**J/P 号只在同一机型内唯一**，见下面"机型区分"。
- **验证用例**（第 2853 页，已对过原图）：`J7544B` 应该追到 `4S095-884` → J7554A 45~47 针，再从 P7554A 经 `4S095-890` → IOP2 上部中继面板。
- **机型区分**（2026-10-12，对过第 4142、70 页原图）：已融合版里同时装着 S207D 和 S307E 的图，照明切换部两种机型的接插件编号不同。比如 S307E 第 4142 页的 J7544E 是光轴追尾快门传感器，S207D 第 70 页的 J7544E 是 SPURE 原点传感器，两页 AI 都没读错。现在每页按页上的 `(NSR-S307E)` / `(NSR-S207D)` 标记推断机型，没标的看前后最近的标记，推不出的算通用页。跨页时 S207D 页和 S307E 页不互连，结果里标〔S207D〕〔S307E〕。线表涉及的 859 页里约 500 页推不出机型，按通用页处理。
- 一个字段里写了一组接插件号的连接（如第 4142 页 `J7554A ─4S095-884─ J7544B / J7602H / J7544C …`），原图确认是一束线分出了 7 路，照常往外追。上午曾以为是读错而禁止往外追，后来查明串线的真正原因是跨机型；`NETLIST_BLOCK_LUMPED=1` 可改回禁止。
- **跨页和分支的处理**（2026-10-12 改）：
  - 同一条连接（同线缆号 + 同一组接插件号）在多页出现，合并成一行，列全部页码。8324 条合并后是 4083 条。
  - 结果按树状显示，每条下面缩进的是从它引出的下一跳，标明经哪个接头或线缆。
  - 线缆号是零件号，同一个号可能用在一整排相同单元上（如 `4S902-019`），所以**只有在所有页上都只连着同两个接插件的线缆号，才拿来跨页**。这类线缆在某页一头是"?"时，按别的页补上另一头（显示成 `?→J7406T/P7406T（据第 2831、4142 页）`），全库补上了 109 条。
  - 经过接了 3 条以上支路的接头时按针脚过滤：和上一段针脚不重叠的支路折叠成一行；针脚没写的照常展开，标"针脚未知"。注意 `wires` 字段没说针脚是哪一头的，所以只在这种多支路的接头上才过滤。
  - 一头是"?"、又补不上的连接，按全文索引给线索 `⇢ 另一头可能在第 N 页（依据）`：同线缆号且同设备名的页 > 同线缆号（不一号多用、≤3 页）> 这个设备的接线（≤4 页）。合并后一头为"?"的 1066 条里，补上 82 条，给出线索 196 条；其余大多是图上本来就只写了"去某设备"，没有接插件号。`A-xxx` 是板子的実装编号（全库 78 个），`H6/F6` 像图幅坐标，都还没用上。
- 网页智能体（`src/tools.py`）里的工具名是 `netlist_trace`，默认追 2 跳。有接插件号或线缆号时优先用它，原来的 `trace_connection` 留作后备。这个工具的结果**不进引用列表**（来源页码只写在文字里）。

## 8. 待办（按优先级）

1. ~~部署、验证 `nikon_trace`，接进网页界面~~（2026-10-12 完成）
2. ~~按内容判断机型~~（2026-10-12 完成，新入库和存量都已按内容打标签，见第 4 节）
3. ~~`net_trace.py` 改成从全文索引读文字，OCR 补出的编号也能用~~（2026-10-12 完成，见第 5 节）
4. ~~错误码格式换算~~（2026-10-12 完成，`src/error_code.py`）：现场写的 `STG-29-80` 换算成 e_st_0540 的 `#29H` 节 `80H` 行，加上《Error Code List》STG 章 Unit Error Code List 的主码 29 行、细分码 `FFFF**80` 行，两边原文都列出来。网页的 `error_code_lookup`、MCP 的 `nikon_search_error_code` 都会先给换算结果。e_st_0540 的 150 个主码，手册里能对上 148 个；1878 个细分码，手册能对上 1533 个（版本和措辞不同）。目前只做了 STG：其他控制器的原文是 8 位码（如 `#4000F007H`），现场没见过 XX-YY 这种写法。**注意：WPS「5.Error code」下的 `e_*` / `j_*` 原文文件没有扩展名，同步没把它们入库**，换算功能是直接读镜像目录里的文件。
5. ~~网页右上角的向量数改成实时查~~（2026-10-14 完成：每次打开网页现查，开着每分钟刷新）
6. ~~重排序~~（2026-10-14 评估完，**默认仍用 deepseek 精排**）：加了可选的硅基流动 `bge-reranker-v2-m3`（免费版，`.env` 设 `RERANK_PROVIDER=api` 启用，和原名次按 0.6 融合）。8 个现场问题对比：api 每次 0.4 秒、deepseek 8~22 秒，但 api 会被字面相近的词带偏（"reticle blind" 排出 "Reticle Resetting"），质量没有更好（deepseek 胜 2、api 胜 1），所以没换。旧的本地 reranker 初始化（从没加载成功、每次启动报警告）已删。以后要换，先用更多问题做正式评估。
7. ~~老格式文件~~（2026-10-14）：48 个（同名去重 43 个）在加转换逻辑前就被记成已同步，从没真正入库，已用 `sync_kwiki.py --reingest --legacy` 重跑：15 个有了内容，**19 个 0 段、9 个只有 1 段——内容是截图**（`S207 RETICLE BLIND PROCEDURE`、`S207_TFD_CALIBRATION_PROCEDURE`、`拆机SOP（S207）` 等），要对截图做 OCR 才能搜到，还没做。`S207_HOW_TO_OPTIMIZE_RETICLE_ND_FILTER.xlsx` 及其 `(2)` 副本文件本身损坏，转换时被当文本导入成 13005 段乱码（已删），转换现在指定 `--infilter`，坏文件会直接失败。
   - **顺带发现表格入库的大问题**：`ingest_excel` 以前只收"表头那一格有字"的单元格，没有表头的表整列内容都丢了（STC LENS调整方法 80 字 → 4549 字）。已修，并用 `--reingest --suffix .xlsx` 重跑全部 91 个 Excel（故障履历不动）。
8. ~~驱动说明书~~（2026-10-14 用户同意后删除）：`［Driver］` 目录下 20 份全删（全文 273 段、向量 660 块），清单在服务器 `data/deleted_driver_docs_20261013.json`；同步本来就跳过 `［Driver］`，不会再回来。
9. `7400-2.pdf` 文件损坏，需要用户在 WPS 里另存一份。
10. WPS 里的 SF100 文件夹被删了（state 里那 62 条已移除，库里的数据保留）。以后可能会入库其他机型，但**优先处理自家机型**。
11. 定时同步每 30 分钟都要完整遍历一遍 WPS（2600 多个文件），慢；向量化是串行请求，批量导入时可以考虑并发。

## 8.5 2026-10-14 新增的东西

- **错误码原文入库**（`src/ingest_error_text.py`）：「5.Error code」下 18 个 `e_*`/`j_*` 文件（没有扩展名，**EUC-JP 编码**）按错误码分条进全文索引，共 9946 条，`doc_type=error_code`；正文补上现场写法（`STG-29`、`STG-29-80`…），8 位码（`4000F007`）直接能搜到。同步程序已能识别这类文件，以后更新会自动重新入库。
- **新电路图自动识图**（`scripts/circuit_recognize.py`，服务 `nikon-circuit-recognize.service/.timer`，配置在 `deploy/systemd/`）：服务器 `.env` 设了 `CIRCUIT_OCR=0`，电路图入库只取文字层（以前入库时做 OCR，大图册会超过同步的 1 小时时限被杀）。同步入库电路图后立刻在后台启动识图任务：OCR → 矢量字形识读合并 → 纠错 → 重建编号索引；每天北京 03:30 兜底。现有 7 本已记为完成。`--list` 看状态。
- **连服务器偶尔超时**：Mac 到 Tailscale 地址时通时不通（`tailscale ping` 通、SSH 超时），过一会儿自己好；急用可以走公网 `root@149.28.19.161`（密钥登录可用）。
- `pgrep -f` 匹配到自己的坑又踩了一次：检查后台任务用 `pgrep -f 'reingest --suffi[x]'` 这种写法。

## 8.6 2026-10-14 网页（ui/app.py）改动和踩坑

- **智能体工具拿不到登录身份**：LlamaIndex 用 `loop.run_in_executor` 跑同步工具，线程池不带 contextvars；`tools._memo` 里的 `call_with_timeout` 又开了一层线程。两处都补了（`build_tools` 建工具时记下用户再绑回去，`util_timeout` 用 `copy_context`）。**以后新开线程跑检索，一律带上 contextvars。**
- **Gradio 6 聊天记录**：`content` 是 `[{"type":"text","text":…}]` 列表，多轮后还会嵌套，用 `agent.history_text()` 取文字。
- **Gradio 自定义 CSS 会被自动加前缀** `.gradio-container .contain`：`.contain` 外层的元素（如侧栏外面那层 `.wrap`）要写成 `.gradio-container …` 开头，且**不能写在 `@media` 里**（`@media` 内一律加前缀），手机/电脑区分用 `clamp(…, calc((100vw - 768px) * 1000), …)`。`.sidebar-parent` 类名不稳定，别依赖。
- **`demo.load(js=…)` 在 6.9 上不执行**，页面脚本改用 `launch(head=_HEAD_JS)`。
- 实时状态：`src/progress.py`，各阶段 `report()`，网页每秒刷新一行 `⏳ … · 已用 N 秒`。进度对象要作为参数交给检索线程再 `bind()`：Gradio 每推进一步生成器都复制一次上下文，在 `chat()` 里设 contextvar 下一步就丢。
- 多步排查实时显示：`agent.ReActStreamParser` 把 ReAct 原始流拆成 💭 思考（每步最多 150 字，遇「**【」收起——DeepSeek 常把整篇结论塞进 Thought）和逐字结论；DSML 工具调用标记截断。
- 客户脱敏：`src/desensitize.py`，名单复用 `acl_classify.CUSTOMER_DICT`（客户A/B/C…），只改网页显示，不改库。
- 参考来源按文件合并（`SourceCollector._groups`）。普通问答（非智能体）的引用编号对应上下文段落，没合并。
- 版面铺满浏览器；手机侧栏默认收起、展开为浮层；管理员「审计」入口已恢复。
- 从国内连 Vultr 很慢（往返约 290ms，首页 4~17 秒，偶尔 60 秒以上），浏览器端测试常超时；功能验证优先在服务器本机调 `ui/app.py` 的 `chat()`。

## 8.7 2026-10-16 SOP 速查（`src/sop_index.py`）

- **为什么要做**：手册在全文索引里按标题切段，英文手册每一步一个标题（`## **1. Click [Select].**`），检索只能命中零散的一两步。
- **做法**：按 `doc_fts` 的 rowid（就是入库顺序）把同一份资料的段落接回去，认出「标题 → 1. → 2. …」连续编号（英文编号、日文 ①②、xlsx 的 `STEP1: |`），拼成完整流程存进 `data/sop_index.db`（独立库）。每步要有动作词（click / 外す / 拆…）的占一半以上才算操作流程，用来排除带编号的原因列表、注意事项列表。自编 SOP（`doc_type=sop`、文件名带 SOP / 作业指导）和文件名像作业方法的表格整份收录、不拆；工作报告、履历、物料清单这类表格跳过。**不调大模型**，只给「标题 + 前几步」算 bge-m3 向量。
- **查询**：向量相似度 + 标题关键词 + 机型加权。算问题向量前先去掉机型名（不然 "S207D" 会把标题带 S207 的表格带上来）；文件名写明是别的机型的再降权（有的资料机型标签打错了，比如 `NSR-S206D S306C Maintenance Guide I` 标的是 S207D）。同一章节下的几段（如 `11.1 Replacing the Halogen Lamp` 的 (1)~(6)）合成一个作业返回。权限沿用 `doc_acl`。
- **入口**：网页「SOP 速查」页（选中流程后可点「整理成中文 SOP」，调 deepseek，只整理不补内容）；智能体工具 `procedure_lookup`（`router.is_procedure_query`：问题里有 步骤 / 流程 / SOP / 怎么调 / 怎么换 等说法就走智能体）；MCP `nikon_search_sop`。
- **更新**：WPS 同步有入库、删除或改名时，`sync_kwiki.py` 末尾自动跑 `sop_index.build()`（拆分几秒，只给内容变了的流程重算向量）。手动跑：`.venv/bin/python scripts/build_sop_index.py`，`--show "查询词"` 看检索结果。
- 现状：357 份资料，3692 个流程。已知不足：PDF 转文字时丢了标题的小节，步骤会挂在上一个小节下面；只有截图的资料（`拆机SOP（S207）` 等）还是拆不出内容（见待办 7）。

## 8.8 2026-10-17 飞书全库同步、缩写对照表、夜间本地提炼

- **飞书**：`scripts/sync_feishu.py` 同步整个知识库「NIKON NSR 205/206/207」（以前只同步一篇工作日志），由 `nikon-feishu-sync.timer` 每 30 分钟跑一次，WPS 同步在跑时这一轮跳过。用户定的分工：**自己写、持续更新的放飞书，其他文件放 WPS**。标题带履历 / 日志的按天入库，SOP 进 SOP 速查，「个人业务量化」排除。两篇履历在权限上是「待复核」（原来就是这样），对话检索不到，「工作日志」页面是实时读飞书。
- **缩写对照表**（`src/glossary.py`、`config/glossary.tsv`）：起因是「RR 怎么测」答不上来（RR = Reticle Rotation，Error Code List 里还有几百处机械手的 RR-axis）。自动挖「全称 (缩写)」和资料里现成的对照表；日文 / 中文只信对照表和人工表。全文检索、语义检索、SOP 速查都会补搜全称，智能体提问时附术语提示。`scripts/build_glossary.py --missing` 列出常用但没收录的缩写（TPR、RB、INC、SUP、LLTC3… 要用户补）。
- **SOP 目录**：第一个标题像 SOP 的文档也整份收录（PM 手顺 SOP Pack 的 `07-RR.pdf` 这类）。
- **夜间提炼改用本地模型**：deepseek 的条款里没有保密承诺（隐私政策写的是输入「可能」用于训练）；Anthropic 不对中国控股公司开放（Supported Countries 条款），所以都不用。改成**用户 Mac 上的 gemma4**：
  - Mac：Ollama 0.40.4 在 `~/Applications/ollama-0.40.4`（brew 被墙，是从 GitHub 官方 release 下载并核对 sha256 的），由 LaunchAgent `com.jayce.ollama-tailnet` 启动，**只监听 Tailscale 地址 100.89.146.17:11434**。
  - 服务器：`src/local_llm.py` 先检查 Mac 在线、空闲 10 分钟以上（经 ssh mac 读 HIDIdleTime）才调用；`src/kb_extract.py` 做三件事：手册 / 日文 checksheet 的异常处理 → 原厂条目（待确认）、现场履历 → 现场案例、SOP 流程分模块。由 `nikon-kb-extract.timer` 在北京时间 00:00~06:30 每半小时试一次，可以中断，下次接着做。
  - `.env` 设了 `TS_LLM=local`，原来每天用 deepseek 抽取的 `nikon-ts-extract.timer` 已停用。本地模型不可用时直接报错，**不会退回 deepseek**。
  - gemma4 不太听细规则，所以在程序里硬处理：「Countermeasure 8」这类引用按同一份资料的对策表换成具体内容，空壳条目丢掉，模型给「其他」时按资料名定模块。
  - 量：4364 个窗口（S207D 3039 个优先），每个 10 秒到 2 分钟不等。
- **用云端模型的规矩**（用户 2026-10-17 定）：硅基流动算向量继续用（它的条款写了不训练、算完即删）；deepseek **只用于网页问答**这类单次使用（问答、整理一篇 SOP）；**大批量任务（提炼、抽取、批量整理）一律不用 deepseek**，走 Mac 本地模型（`ts_table._llm(bulk=True)` + `TS_LLM=local`）。Anthropic 的服务不对中国控股公司开放，**线表抽取脚本 netlist_* 用的 Claude API 以后不再用**。

## 8.9 2026-10-17 OVL 分析页（集成 ovl-analyzer）

- 网页新增「OVL 分析」：上传 MESR → 晶圆矢量图 / 曲线、数据表、文件头、原文；叠对类（`is_vector_type`）做 10 参数拆分 + 原始 vs 残差图。用户定的范围：先只做网页上传，**不标超标、不接智能体建议**（以后可能做：智能体按超标项对应 SOP / 现场经验）。
- **代码只有一份**：解析、画图、拟合都在 `~/ovl-analyzer`（另一个仓库）。为此把画图函数从桌面版界面里拆到了 `ovl_analyzer/plots.py`（不依赖 Qt），桌面版改成调用它；拆前拆后 12 张图逐像素一致。nikon-expert 的 `src/ovl_view.py` 只负责调用这些函数。
- 服务器上 ovl-analyzer 放在 `/opt/ovl-analyzer`（rsync 同步，见 README 第八节）；虚拟环境新装了 matplotlib 3.10.8（只新增了包，没有升级 numpy 等已有的库）。服务器上 pip 的启动脚本指向旧路径，要用 `.venv/bin/python -m pip`。

## 9. 用户的习惯

- 中文交流，不喜欢"AI 味"，回答要口语、直白。
- 用户是 S207 平台的技术负责人，不是专职程序员：命令要能直接复制执行，结果要说清楚意味着什么。
- 删除数据、重启服务、改 state 这类操作，先说明再做。
- Nikon 图纸是 Confidential。只有线表这件事经用户同意发给了 Claude API；其他数据外发要先问。
