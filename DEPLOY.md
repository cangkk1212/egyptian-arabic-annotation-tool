# 埃及方言音频标注工具 — 部署与运维指南

## 访问地址

> 🔗 **https://arabic-annotation.top**

## 架构

```
用户浏览器
    │
    ▼ HTTPS (全球 CDN)
Cloudflare Tunnel ──── cloudflared-tunnel.service (systemd)
    │
    ▼ localhost:8080
Gunicorn (4 workers × 4 threads) ──── audio-annotator.service (systemd)
    │
    ▼
Flask (server.py) ─── JSON标注文件 ─── 音频文件
```

## 项目结构

```
annotation_tool/
├── server.py                    # Flask HTTP 服务器
├── index.html                   # 标注前端页面
├── preprocess.py                # 音频预处理（VAD断句 + ASR预标注）
├── classify.py                  # 长音频内容分类
├── gunicorn_config.py           # Gunicorn 生产配置
├── config.json                  # 配置文件（含API Key）
├── config.example.json          # 配置模板
├── requirements.txt             # Python 依赖
├── audio-annotator.service      # Gunicorn systemd 服务文件
├── cloudflared-tunnel.service   # Cloudflare隧道 systemd 服务文件
└── DEPLOY.md                    # 本文件
```

## 依赖服务

### 1. Gunicorn 服务 (audio-annotator.service)

```
端口: 8080
工作进程: 4 workers × 4 threads = 16 并发
超时: 300 秒
配置: gunicorn_config.py
```

### 2. Cloudflare 隧道 (cloudflared-tunnel.service)

```
域名: arabic-annotation.top
隧道ID: a5956d30-f32d-4e03-bb94-ae1d530099bd
配置: ~/.cloudflared/config.yml
证书: ~/.cloudflared/cert.pem
```

## 常用运维命令

### 查看服务状态

```bash
sudo systemctl status audio-annotator
sudo systemctl status cloudflared-tunnel
```

### 重启服务

```bash
sudo systemctl restart audio-annotator
sudo systemctl restart cloudflared-tunnel
```

### 查看日志

```bash
# Gunicorn 日志
tail -100 /home/cjg/annotation_tool/gunicorn.log

# Cloudflare 隧道日志
tail -100 /home/cjg/annotation_tool/tunnel.log

# 或者用 journalctl
journalctl -u audio-annotator -n 50 --no-pager
journalctl -u cloudflared-tunnel -n 50 --no-pager
```

### 停机 / 启动

```bash
sudo systemctl stop audio-annotator cloudflared-tunnel
sudo systemctl start audio-annotator cloudflared-tunnel
```

### 重启后自动恢复

两个服务均已设为 `enable`，服务器重启后自动启动，无需手动干预。

## 24小时监控（自动拉起）

`monitor.sh` 由 crontab 每分钟执行一次，检测两个健康地址并自动拉起故障服务。systemd 的 `Restart=always` 只能处理进程崩溃，本脚本补充覆盖**进程假死**（进程在但接口无响应）和**隧道断连**场景。

| 检查项 | 地址 | 失败时动作 |
|--------|------|-----------|
| 本地后端 | `http://localhost:8080/api/health` | 重启 `audio-annotator`（并确保隧道仍在运行） |
| 公网入口 | `https://arabic-annotation.top/api/health` | 重启 `cloudflared-tunnel` |

### 一次性配置（root 权限）

1. 授权 huawei 无密码管理这两个服务（监控脚本重启服务需要）：

```bash
echo 'huawei ALL=(root) NOPASSWD: /usr/bin/systemctl * audio-annotator, /usr/bin/systemctl * cloudflared-tunnel' \
  | sudo tee /etc/sudoers.d/annotator-monitor
sudo chmod 440 /etc/sudoers.d/annotator-monitor
sudo visudo -c   # 校验语法
```

2. 安装 crontab 定时任务（每分钟）：

```bash
( crontab -l 2>/dev/null | grep -v 'annotation_tool/monitor.sh'; \
  echo '* * * * * /home/cjg/annotation_tool/monitor.sh' ) | crontab -
crontab -l   # 确认
```

### 保护机制

- **熔断**：同一服务 30 分钟内最多自动重启 5 次，超过后记录 `CRITICAL` 并停止拉起（窗口滑过后自动恢复尝试），避免坏配置导致无限重启循环
- **最小间隔**：同一服务两次重启至少间隔 90 秒，避免启动期间被反复重启
- **并发锁**：flock 防止上一轮未结束时并发执行

### 日志与状态

```bash
tail -f /home/cjg/annotation_tool/monitor.log        # 动作日志（DOWN/ACTION/RECOVER/CRITICAL）
cat /home/cjg/annotation_tool/.monitor_state         # 当前状态（ok/down/critical）
cat /home/cjg/annotation_tool/.monitor_restarts      # 30 分钟内的重启记录
```

日志只在状态变化或执行动作时记录，不会刷屏；每天约 00:00 写一行 `HEARTBEAT` 证明守护脚本存活。收到 `CRITICAL` 日志说明自动拉起已连续失败 5 次，需人工排查（`gunicorn.log` / `tunnel.log`）。

### 验证

```bash
# 演练（模拟本地/公网故障，不真正重启）
MON_LOCAL_URL=http://localhost:9999/x /home/cjg/annotation_tool/monitor.sh --dry-run --verbose
MON_PUBLIC_URL=http://localhost:9999/x /home/cjg/annotation_tool/monitor.sh --dry-run --verbose

# 正常巡检（服务健康时无任何输出、无日志）
/home/cjg/annotation_tool/monitor.sh --verbose
```

> 注意：原 `health_check.sh`（仅记录异常、不自动拉起）已被 monitor.sh 取代，可不再使用。

## 数据路径

| 数据 | 路径 |
|------|------|
| 音频文件 | `/home/ck/ar_audios/` |
| 标注文件 (JSON) | `/home/ck/annotations/` |
| 备份 | `/home/ck/annotations_copy/` |
| 配置 | `/home/cjg/annotation_tool/config.json` |

## 预处理（VAD断句 + ASR预标注）

```bash
cd /home/cjg/annotation_tool

# 断点续传（跳过已处理）
python3 preprocess.py -w 10

# 强制全部重新处理
python3 preprocess.py -w 10 --force

# 修改VAD参数（临时覆盖config.json）
python3 preprocess.py -w 10 --min-speech 2000 --min-silence 500
```

## 音频分类

```bash
cd /home/cjg/annotation_tool

# 断点续传
python3 classify.py

# 强制全部重新分类
python3 classify.py --force

# 仅统计不执行
python3 classify.py --dry-run
```

## 修改配置

编辑 `config.json` 后重启 Gunicorn：

```bash
sudo systemctl restart audio-annotator
```

## 域名续费

- 域名：`arabic-annotation.top`
- 注册商：阿里云（万网）
- 到期前续费，否则域名会被释放

## 故障排查

### 网页无法访问

```bash
# 1. 确认本地服务正常
curl http://localhost:8080/api/stats

# 2. 确认隧道运行
sudo systemctl status cloudflared-tunnel

# 3. 确认域名解析
dig arabic-annotation.top

# 4. 查看隧道日志
tail -50 /home/cjg/annotation_tool/tunnel.log
```

### 音频加载失败

```bash
# 检查音频文件是否存在
ls "/home/ck/ar_audios/..."

# 增加超时（已设300s，一般够用）
# 编辑 gunicorn_config.py 调整 timeout 值，然后重启
sudo systemctl restart audio-annotator
```

### Cloudflare 隧道重连

```bash
sudo systemctl restart cloudflared-tunnel
```
