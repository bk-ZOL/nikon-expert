# systemd 服务（服务器 /etc/systemd/system/）

| 文件 | 作用 |
|---|---|
| nikon-circuit-recognize.service / .timer | 电路图识图后台任务（`scripts/circuit_recognize.py`）。同步入库新电路图时立刻触发，定时器每天 19:30 UTC（北京 03:30）兜底 |

安装：
```
cp deploy/systemd/nikon-circuit-recognize.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now nikon-circuit-recognize.timer
```
配套：服务器 `.env` 里要有 `CIRCUIT_OCR=0`（电路图入库时只取文字层，OCR 交给这个任务；否则同步会被大图册拖过 1 小时时限）。

其他服务（nikon-expert、nikon-mcp、nikon-wps-sync、nikon-feishu-refresh）的配置目前只在服务器上，见 CLAUDE.md 第 1 节。

## nikon-ts-extract（Troubleshooting 表，2026-10-15）

每天北京 04:30（UTC 20:30）从故障履历 / 工作日志 / 客户报告里抽取新案例、归并成条目；网页「Troubleshooting 表」页管理员也能点「立即抽取新资料」手动触发。

    cp deploy/systemd/nikon-ts-extract.* /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now nikon-ts-extract.timer

## nikon-feishu-sync（飞书知识库同步，2026-10-16）

每 30 分钟同步整个飞书知识库「NIKON NSR 205/206/207」（`scripts/sync_feishu.py`）：装机履历按天入库，SOP 进 SOP 速查，其余文档、附件按普通资料入库；只重读最后编辑时间变了的文档。WPS 同步正在跑时这一轮跳过。

    cp deploy/systemd/nikon-feishu-sync.* /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now nikon-feishu-sync.timer
    journalctl -u nikon-feishu-sync -n 50     # 看最近一次同步
