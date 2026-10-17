#!/bin/sh
# 重启网页和 MCP 服务，并检查起来没有。结果追加到 /var/log/nikon-restart.log。
# 定时执行（一次性）：systemd-run --on-calendar="YYYY-MM-DD HH:MM:SS" --unit=nikon-restart-once /opt/nikon-expert/deploy/restart_services.sh
LOG=/var/log/nikon-restart.log
{
  echo "==== $(date -u '+%F %T UTC')（北京 $(TZ=Asia/Shanghai date '+%F %T')）重启 nikon-expert nikon-mcp"
  systemctl restart nikon-expert nikon-mcp
  ok=0
  for i in $(seq 1 60); do                      # 网页启动要 1 分钟左右（加载引擎）
    code=$(curl -s --noproxy '*' -m 10 -o /dev/null -w '%{http_code}' http://100.93.22.104:7860/ || true)
    [ "$code" = "200" ] && ok=1 && break
    sleep 5
  done
  echo "nikon-expert: $(systemctl is-active nikon-expert)，网页 $([ $ok = 1 ] && echo 正常 || echo "打不开（最后状态码 $code）")"
  echo "nikon-mcp: $(systemctl is-active nikon-mcp)"
  journalctl -u nikon-expert --since "-6 min" --no-pager | grep -E "Traceback|Error|运行中" | tail -5
} >> "$LOG" 2>&1
