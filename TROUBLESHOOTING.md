# 标注工具故障排查手册

> 面向非技术用户：出现问题时，按「症状」找到对应小节，复制粘贴命令即可。
> 所有命令都在 SSH 登录服务器后执行。

## 一、日志文件一览（都在 /home/cjg/annotation_tool/ 目录下）

| 文件 | 内容 | 什么时候看 |
|------|------|-----------|
| `gunicorn.log` | 服务端请求日志 + 报错堆栈 | Save failed、页面空白、服务器错误 |
| `tunnel.log` | Cloudflare 隧道日志 | ERROR 1033、连接问题 |
| `client_errors.log` | **浏览器端自动上报的错误**（新增） | 任何远端用户报错后 |
| `health.log` | 每分钟健康检查记录（仅异常时写一行） | 确定故障时间点 |

## 二、通用排查顺序（任何问题都先做这三步）

```bash
cd /home/cjg/annotation_tool
tail -20 health.log        # 1. 有没有故障记录？什么时间？
tail -50 gunicorn.log      # 2. 服务端当时发生了什么？
tail -30 client_errors.log # 3. 用户浏览器端报了什么错？
```

## 三、按症状排查

### 1. 用户提示「Save failed」

```bash
# 浏览器端的具体错误（type=network 是网络问题；http_error/bad_response 是服务端或隧道问题）
grep -n "SAVE FAILED\|Traceback" gunicorn.log | tail -5   # 服务端保存是否真的报错
tail -20 client_errors.log                                 # 用户端上报的错误详情
grep "Shutting down\|Booting" gunicorn.log | tail -10      # 当时是否在重启服务
```

**常见结论**：
- `type=network` + health.log 有记录 → 当时服务/隧道断了，用户稍后重试即可
- `SAVE FAILED` 出现在 gunicorn.log → 服务端真实错误，把该行发给开发人员

### 2. 用户提示「Audio load failed」

```bash
grep audio_error client_errors.log | tail -5                # 哪些音频、什么时间加载失败
grep "stream.*canceled" tunnel.log | tail -5                # 隧道流是否被中断
grep "Shutting down\|Booting" gunicorn.log | tail -10      # 是否赶上服务重启
```

**说明**：现在前端会自动重试一次。单个音频偶发失败一般无害；若大量出现且时间集中，通常是服务重启窗口，等 1 分钟即可恢复。

### 3. 标注页面空白 / Load failed

```bash
grep js_error client_errors.log | tail -5          # 浏览器 JS 报错（自动上报）
grep -n "Traceback" gunicorn.log | tail -5         # 服务端 500 错误
```

### 4. ERROR 1033（Cloudflare 隧道错误）

**含义**：Cloudflare 隧道连不上本机服务。常见于服务重启的几秒钟窗口，或隧道断线。

```bash
systemctl status audio-annotator cloudflared-tunnel   # 两个服务是否都 active (running)
tail -5 health.log                                    # local 和 public 哪个不正常
```

**判断**：
- `local=200` 但 `public` 异常 → 隧道问题，执行 `sudo systemctl restart cloudflared-tunnel`
- `local` 也异常 → 服务问题，执行 `sudo systemctl restart audio-annotator`
- 两者都正常且持续出现 1033 → 用户网络到 Cloudflare 的链路问题，等几分钟再试

### 5. 重启服务的正确姿势（部署新代码后）

```bash
sudo systemctl restart audio-annotator
sudo systemctl restart cloudflared-tunnel
```

> 注意：重启有约 5~10 秒的断线窗口，此时任何人访问都会短暂报错。
> 建议在标注人员休息时段重启；重启前先 `tail -20 gunicorn.log` 确认没有正在保存的请求。

## 四、日志维护

三个日志文件会不断增长，建议每月清理一次（保留最近 20000 行）：

```bash
cd /home/cjg/annotation_tool
for f in gunicorn.log tunnel.log client_errors.log health.log; do
  [ -f "$f" ] && tail -n 20000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done
```

## 五、健康监测（可选但推荐）

`health_check.sh` 每分钟检查本机服务和公网域名是否正常，异常时写入 `health.log`。
安装方法（在服务器上执行一次）：

```bash
crontab -l 2>/dev/null; echo "* * * * * /home/cjg/annotation_tool/health_check.sh" | crontab -
```

之后故障发生时，`health.log` 会精确记录故障时间点，便于对照 gunicorn.log 找原因。
