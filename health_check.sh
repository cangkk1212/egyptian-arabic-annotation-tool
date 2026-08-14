#!/bin/bash
# 标注服务健康检查：建议通过 crontab 每分钟运行一次，仅在异常时记录日志
# 日志文件：/home/cjg/annotation_tool/health.log
# 判断逻辑：
#   local  != 200  → 本机 gunicorn 服务有问题（可查 gunicorn.log）
#   public != 200  → 公网访问有问题：隧道断了 / Cloudflare 问题 / 服务重启窗口
LOG=/home/cjg/annotation_tool/health.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

LOCAL=$(curl -s -m 10 -o /dev/null -w '%{http_code}' http://localhost:8080/api/health)
PUBLIC=$(curl -s -m 15 -o /dev/null -w '%{http_code}' https://arabic-annotation.top/api/health)

if [ "$LOCAL" != "200" ] || [ "$PUBLIC" != "200" ]; then
  echo "$TS local=$LOCAL public=$PUBLIC" >> "$LOG"
fi
