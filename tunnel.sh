#!/bin/bash
# Cloudflare Tunnel 自动重连脚本
# 保持隧道常驻，URL 变化时更新记录

URL_FILE="/home/cjg/annotation_tool/tunnel_url.txt"
PORT=8080
CLOUDFLARED="/home/huawei/bin/cloudflared"

while true; do
    echo "[$(date '+%H:%M:%S')] 启动隧道..."

    $CLOUDFLARED tunnel --url http://localhost:$PORT --no-autoupdate 2>&1 | while read line; do
        echo "$line"

        # 提取新 URL
        if echo "$line" | grep -q "trycloudflare.com"; then
            URL=$(echo "$line" | grep -oP 'https://[a-z0-9.-]+\.trycloudflare\.com')
            echo "$URL" > "$URL_FILE"
            echo ""
            echo "============================================"
            echo "  📱 手机访问地址："
            echo "  $URL"
            echo "============================================"
            echo ""
        fi
    done

    echo "[$(date '+%H:%M:%S')] 隧道断开，5秒后重连..."
    sleep 5
done
