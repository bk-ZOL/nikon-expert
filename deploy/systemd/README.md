# systemd 服务（服务器 /etc/systemd/system/）

| 文件 | 作用 |
|---|---|
| nikon-circuit-recognize.service / .timer | 电路图识图后台任务（`scripts/circuit_recognize.py`）。同步入库新电路图时立刻触发，定时器每天 19:30 UTC（北京 03:30）兜底 |
| nikon-kb-extract.service / .timer | 夜间提炼（`scripts/kb_extract.py`）。北京 00:00～06:30 每半小时试一次，Mac 在线且空闲才用本地模型。这是现在的提炼任务 |
| ~~nikon-ts-extract.service / .timer~~ | 已停用。原来用 deepseek 抽 Troubleshooting 表，改由上面的 kb-extract 做。不要再 enable |

安装：
```
cp deploy/systemd/nikon-circuit-recognize.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now nikon-circuit-recognize.timer
```
配套：服务器 `.env` 里要有 `CIRCUIT_OCR=0`（电路图入库时只取文字层，OCR 交给这个任务；否则同步会被大图册拖过 1 小时时限）。

其他服务（nikon-expert、nikon-mcp、nikon-wps-sync、nikon-feishu-refresh）的配置目前只在服务器上，见 CLAUDE.md 第 1 节。

## nikon-kb-extract（夜间提炼，现行）

北京时间 00:00～06:30（UTC 16:00～22:30）每半小时试一次。Mac 不在线或正在用就跳过，做过的内容不重做。大批量提炼走本地模型，不用 deepseek。

    cp deploy/systemd/nikon-kb-extract.* /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now nikon-kb-extract.timer
    journalctl -u nikon-kb-extract -n 50

## nikon-ts-extract（已停用，2026-10-17 起）

原来每天北京 04:30 用 deepseek 抽 Troubleshooting 表。这条定时器已停，工作改由 `nikon-kb-extract` 做。单元文件还在仓库里，是为了对照旧机器上的配置，**不要再 enable**。

网页「Troubleshooting 表」里的「立即抽取新资料」仍会去启动 `nikon-ts-extract.service`（`ui/app.py` 的 `ts_run_extract`）。夜里自动跑的不是这个按钮。

## nikon-feishu-sync（飞书知识库同步，2026-10-16）

每 30 分钟同步整个飞书知识库「NIKON NSR 205/206/207」（`scripts/sync_feishu.py`）：装机履历按天入库，SOP 进 SOP 速查，其余文档、附件按普通资料入库；只重读最后编辑时间变了的文档。WPS 同步正在跑时这一轮跳过。

    cp deploy/systemd/nikon-feishu-sync.* /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now nikon-feishu-sync.timer
    journalctl -u nikon-feishu-sync -n 50     # 看最近一次同步
