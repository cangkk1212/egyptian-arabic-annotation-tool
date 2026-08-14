#!/bin/bash
# ============================================================
# 标注服务 24 小时守护脚本（monitor.sh）
# ------------------------------------------------------------
# 部署方式：由 huawei 用户的 crontab 每分钟执行一次
#     * * * * * /home/cjg/annotation_tool/monitor.sh
#
# 监控与自动拉起逻辑：
#   1) 本地健康检查 http://localhost:8080/api/health 失败
#      → 重启 audio-annotator（Gunicorn 后端）；
#        后端重启会连带停止 cloudflared-tunnel（systemd Requires 依赖），
#        因此随后会把隧道也拉起来
#   2) 本地正常但公网 https://arabic-annotation.top/api/health 失败
#      → 重启 cloudflared-tunnel
#   3) 熔断保护：同一服务 30 分钟内最多自动重启 5 次，
#      超过后只记录 CRITICAL 日志、停止自动拉起，避免无限重启循环；
#      窗口滑过后自动恢复尝试
#   4) 同一服务两次重启最小间隔 90 秒（防止启动过程中被反复重启）
#
# 输出：
#   日志    /home/cjg/annotation_tool/monitor.log（仅状态变化/动作时记录）
#   状态    .monitor_state    （每个服务当前状态 ok/down/critical）
#   重启记录 .monitor_restarts（窗口内重启时间戳，用于熔断统计）
#
# 权限要求（一次性配置，见 DEPLOY.md「24小时监控」章节）：
#   echo 'huawei ALL=(root) NOPASSWD: /usr/bin/systemctl * audio-annotator, /usr/bin/systemctl * cloudflared-tunnel' \
#     | sudo tee /etc/sudoers.d/annotator-monitor
#   sudo chmod 440 /etc/sudoers.d/annotator-monitor && sudo visudo -c
#
# 调试：
#   --dry-run     只演练不执行任何重启
#   --verbose     输出调试信息到 stderr
#   MON_LOCAL_URL / MON_PUBLIC_URL 环境变量可覆盖检查地址（用于测试）
# ============================================================
set -u

# cron 环境 PATH 极简，这里显式补齐（含 anaconda 下的 curl）
PATH="/usr/bin:/bin:/usr/local/bin:/home/huawei/anaconda3/bin:$PATH"

DIR=/home/cjg/annotation_tool
LOG="$DIR/monitor.log"
STATE="$DIR/.monitor_state"
RESTARTS="$DIR/.monitor_restarts"
LOCK="$DIR/.monitor.lock"

LOCAL_URL="${MON_LOCAL_URL:-http://localhost:8080/api/health}"
PUBLIC_URL="${MON_PUBLIC_URL:-https://arabic-annotation.top/api/health}"

WINDOW_SEC=1800       # 熔断统计窗口（秒）
MAX_RESTARTS=5        # 窗口内最多自动重启次数
MIN_RESTART_GAP=90    # 同一服务两次重启的最小间隔（秒）

DRY=false; VERBOSE=false
for a in "$@"; do
  case "$a" in
    --dry-run|-n) DRY=true ;;
    --verbose|-v) VERBOSE=true ;;
    *) echo "用法: $0 [--dry-run] [--verbose]" >&2; exit 2 ;;
  esac
done

now=$(date +%s)
log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
dbg(){ $VERBOSE && echo "[dbg] $*" >&2; }

# 防止上一轮未结束时并发执行
exec 9>"$LOCK"
flock -n 9 || { log "上一轮监控尚未结束，本轮跳过"; exit 0; }

# ---- 状态文件读写 ----
prev_state(){ awk -v s="$1" '$1==s{print $2}' "$STATE" 2>/dev/null; }
set_state(){  # svc ok|down|critical
  grep -v "^$1 " "$STATE" 2>/dev/null > "$STATE.tmp"
  echo "$1 $2" >> "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"
}
restart_count(){ # svc → 窗口内重启次数
  cat "$RESTARTS" 2>/dev/null | awk -v s="$1" -v w="$WINDOW_SEC" -v n="$now" '$1==s && $2>=n-w{c++} END{print c+0}'
}
last_restart(){ # svc → 最近一次重启时间戳（无则 0）
  cat "$RESTARTS" 2>/dev/null | awk -v s="$1" '$1==s && $2>m{m=$2} END{print m+0}'
}
record_restart(){ # svc：追加时间戳并顺带清理窗口外的旧记录
  awk -v w="$WINDOW_SEC" -v n="$now" '$2>=n-w' "$RESTARTS" 2>/dev/null > "$RESTARTS.tmp"
  echo "$1 $now" >> "$RESTARTS.tmp"
  mv "$RESTARTS.tmp" "$RESTARTS"
}

# ---- 健康检查 ----
http_ok(){ # url timeout_sec
  local code
  code=$(curl -s -m "$2" -o /dev/null -w '%{http_code}' "$1" 2>/dev/null)
  [ "$code" = "200" ]
}

# ---- 重启动作 ----
do_restart(){ # svc
  local svc=$1
  if $DRY; then log "DRY-RUN 计划重启 $svc"; return 0; fi
  if ! sudo -n systemctl reset-failed "$svc" >/dev/null 2>&1; then
    log "ERROR sudo 无法管理 $svc：请先按 DEPLOY.md「24小时监控」章节配置 sudoers，否则服务挂掉后无法自动拉起"
    return 1
  fi
  if sudo -n systemctl restart "$svc" >/dev/null 2>&1; then
    record_restart "$svc"
    log "ACTION 已重启 $svc（30 分钟内第 $(restart_count "$svc") 次）"
  else
    log "ERROR systemctl restart $svc 失败，请运行 systemctl status $svc 排查"
  fi
}

# ---- 单个服务守护 ----
handle_svc(){ # svc ok_bool
  local svc=$1 ok=$2
  local prev; prev=$(prev_state "$svc")
  if $ok; then
    if [ "$prev" = "down" ] || [ "$prev" = "critical" ]; then log "RECOVER $svc 已恢复正常"; fi
    if [ "$prev" != "ok" ]; then set_state "$svc" ok; fi
    return 0
  fi
  # —— 不健康 ——
  if [ "$prev" = "critical" ]; then
    dbg "$svc 熔断中，跳过本轮（窗口滑过后自动恢复）"
    return 1
  fi
  if [ "$prev" != "down" ]; then log "DOWN $svc 健康检查失败"; set_state "$svc" down; fi
  if [ "$(restart_count "$svc")" -ge "$MAX_RESTARTS" ]; then
    log "CRITICAL $svc 30 分钟内已自动重启 $MAX_RESTARTS 次仍未恢复，停止自动拉起，请人工排查"
    set_state "$svc" critical
    return 1
  fi
  local gap=$(( now - $(last_restart "$svc") ))
  if [ "$gap" -lt "$MIN_RESTART_GAP" ]; then
    dbg "$svc 距上次重启仅 ${gap} 秒，跳过本轮"
    return 1
  fi
  do_restart "$svc"
}

# 后端被重启时，Requires 依赖会连带停掉隧道；此函数确保隧道仍在运行
ensure_tunnel(){
  if systemctl is-active --quiet cloudflared-tunnel 2>/dev/null; then return 0; fi
  log "ACTION audio-annotator 重启连带停止了隧道，正在拉起 cloudflared-tunnel"
  if $DRY; then return 0; fi
  if sudo -n systemctl start cloudflared-tunnel >/dev/null 2>&1; then
    record_restart cloudflared-tunnel
  else
    log "ERROR sudo 拉起 cloudflared-tunnel 失败"
  fi
}

# ============================================================
# 主流程
# ============================================================
LOCAL_OK=false
http_ok "$LOCAL_URL" 10 && LOCAL_OK=true
dbg "本地检查 $LOCAL_URL → $LOCAL_OK"

if $LOCAL_OK; then
  handle_svc audio-annotator true
  if http_ok "$PUBLIC_URL" 20; then
    handle_svc cloudflared-tunnel true
  else
    handle_svc cloudflared-tunnel false
  fi
else
  handle_svc audio-annotator false
  ensure_tunnel
fi

# 每日心跳（约 00:00），证明守护脚本本身存活
if [ "$(date +%H%M)" = "0000" ]; then
  log "HEARTBEAT 监控脚本运行正常 local=$($LOCAL_OK && echo ok || echo down)"
fi
