# 埃及方言音频标注工具

基于 Web 的阿拉伯语（埃及方言）音频标注平台，集成 VAD 自动断句、ASR 预标注、内容分类、波形可视化、多用户登录与任务自动分配。

## 功能概览

| 功能 | 说明 |
|------|------|
| **VAD 自动断句** | silero-vad 6.x 语音活动检测，自动切分语音段 |
| **ASR 预标注** | 阿里云 DashScope Qwen3.5-Omni 多模态大模型阿拉伯语转写 |
| **内容分类** | 基于转写文本自动分类（DashScope Qwen-Turbo，10 类 + Other） |
| **用户登录** | 姓名登录 + Flask 签名会话，同名互斥，30 分钟会话超时 |
| **任务自动分配** | 登录后随机分配待标注文件，文件锁防并发，分配超时自动释放 |
| **排行榜** | 按标注员统计已完成文件数与标注时长，30 秒自动刷新 |
| **波形可视化** | Canvas 对称波形图，点击跳转，播放自动滚动 |
| **标注编辑** | 在线修改转写文本、调整时间戳、标记质量 |
| **跳过标记** | Noisy / Not Egyptian / Poor Quality 三种原因整文件跳过 |
| **历史导航** | Back / Next 在最近 10 个处理过的文件间往返修改 |
| **自动保存** | 输入 3 秒 / 失焦 / 提交前自动保存，失败自动重试 |
| **标注导出** | export.py 按用户汇总已标注/跳过的文件，导出 Excel |
| **Cloudflare 部署** | 命名隧道 + 自有域名 arabic-annotation.top，全球 CDN 加速，HTTPS 自动证书 |
| **24 小时监控** | monitor.sh 每分钟巡检，自动拉起故障服务（带熔断保护） |

## 访问地址

> 🔗 **https://arabic-annotation.top**

- 标注员操作手册：[标注工具使用说明.md](标注工具使用说明.md)
- 部署运维指南：[DEPLOY.md](DEPLOY.md)

## 项目结构

```
annotation_tool/
├── server.py                    # Flask HTTP 服务器（标注 API + 登录/分配）
├── index.html                   # 标注前端单页应用
├── login.html                   # 登录页（含排行榜）
├── preprocess.py                # 音频预处理（VAD 断句 + ASR 预标注）
├── classify.py                  # 长音频内容分类
├── export.py                    # 标注统计导出 Excel
├── gunicorn_config.py           # Gunicorn 生产配置
├── config.json                  # 运行配置（路径、VAD、ASR、密钥）
├── config.example.json          # 配置模板
├── requirements.txt             # Python 依赖
├── audio-annotator.service      # Gunicorn systemd 服务
├── cloudflared-tunnel.service   # Cloudflare 命名隧道 systemd 服务
├── monitor.sh                   # 24 小时监控脚本（每分钟巡检，自动拉起）
├── health_check.sh              # 旧巡检脚本（已被 monitor.sh 取代）
├── tunnel.sh                    # 旧临时隧道脚本（已被命名隧道取代）
├── 标注工具使用说明.md           # 标注员操作手册
├── vad_tool/                    # 独立 VAD 切分工具
│   ├── vad_splitter.py           # 音频断句，输出 WAV 片段
│   └── config.json               # VAD 参数配置
├── DEPLOY.md                    # 部署运维指南
└── README.md                    # 本文件
```

## 环境要求

- Python 3.10+
- PyTorch ≥ 2.0（VAD 模型推理）
- 阿里云 DashScope API Key（ASR 预标注 + 分类，可选）

## 安装

```bash
pip install -r requirements.txt
```

首次运行时，silero-vad 会自动从 PyTorch Hub 下载 VAD 模型（约 3MB），缓存在 `~/.cache/torch/hub/`。

## 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| VAD 断句 | silero-vad 6.x | 预训练语音活动检测，CPU/GPU 均可 |
| ASR 预标注 | Qwen3.5-Omni (DashScope) | 阿里云多模态大模型，阿拉伯语转写 |
| 音频分类 | Qwen-Turbo (DashScope) | 文本分类，10 类 + Other |
| 登录认证 | Flask session | 签名 cookie + secret_key，30 分钟会话超时 |
| 后端 | Flask + Gunicorn | HTTP API + 4 workers × 4 threads |
| 前端 | 原生 HTML/CSS/JS | 单页应用，Canvas 波形图 |
| 导出 | openpyxl | 标注统计导出 Excel |
| 隧道 | Cloudflare Tunnel | 命名隧道 + 自有域名，全球 CDN |

## 配置

复制 `config.example.json` 为 `config.json`，修改以下参数：

```json
{
  "audio_dir": "/path/to/audio",
  "annotations_dir": "/path/to/annotations",
  "port": 8080,
  "backup_dir": "/path/to/backup",
  "secret_key": "随机字符串（登录会话签名，可用 python3 -c 'import secrets;print(secrets.token_hex(32))' 生成）",
  "session_timeout_minutes": 30,
  "vad": {
    "min_speech_duration_ms": 3000,
    "min_silence_duration_ms": 300,
    "threshold": 0.5,
    "max_segment_duration_s": 30,
    "speech_pad_ms": 100
  },
  "asr": {
    "api_key": "sk-xxx",
    "model": "qwen3.5-omni-plus",
    "language": "ar",
    "workers": 10
  }
}
```

- `asr.api_key` 留空或缺失时，预处理只做 VAD 断句、跳过 ASR 预标注。
- VAD 参数只从 `config.json` 读取，修改后重新运行预处理即可。

## 工作流程

### 1. 准备音频

将音频文件（.wav / .mp3 / .flac / .ogg / .m4a / .webm / .opus / .wma / .aac）放入 `audio_dir` 目录，支持子文件夹。目录结构镜像到标注目录。

### 2. 预处理（VAD 断句 + ASR 预标注）

```bash
# VAD 断句；配置了 asr.api_key 时同时做 ASR 预标注
python3 preprocess.py

# 指定 ASR 并发线程数（默认取 config.json 的 asr.workers）
python3 preprocess.py -w 10

# 强制重新处理所有文件
python3 preprocess.py --force
```

**断点续传**：预处理自动跳过已有标注的文件和段，中断后直接重新运行即可。

### 3. 内容分类

```bash
# 断点续传（跳过已分类）
python3 classify.py

# 强制重新分类
python3 classify.py --force

# 查看统计
python3 classify.py --dry-run
```

分类结果写入标注 JSON 的 `category` 字段。10 个预定义类别 + Other 自动分类。

### 4. 启动标注服务

```bash
# 开发模式
python3 server.py --port 8080

# 生产模式（Gunicorn，4 workers × 4 threads，timeout 300s）
gunicorn -c gunicorn_config.py server:app
```

### 5. 登录标注

浏览器打开 `http://localhost:8080`（生产环境为 https://arabic-annotation.top），输入姓名登录：

- 登录后系统**随机自动分配**一个待标注文件（无需手动挑选），分配期间文件对其他标注员锁定；
- 同名互斥：一个名字同一时间只能一人使用（30 分钟无活动自动释放）；
- 提交（Mark Done / Skip）后自动加载下一个文件；Back / Next 可在最近 10 个历史文件间往返修改；
- 关闭浏览器超过 72 小时后，分配的文件自动释放回待分配池，已保存内容不受影响。

### 6. 导出标注统计

```bash
python3 export.py   # 按用户汇总已标注/跳过的文件 → annotation_export.xlsx
```

### 7. 部署到公网与监控

参见 [DEPLOY.md](DEPLOY.md)，包含 systemd 配置、Cloudflare 命名隧道、域名配置，以及 monitor.sh 24 小时自动拉起监控。

## 独立 VAD 切分工具

`vad_tool/` 提供纯 VAD 断句工具，将音频切割为 WAV 片段文件，独立于标注管线。

```bash
cd vad_tool
python3 vad_splitter.py -i ./input -o ./output
python3 vad_splitter.py -t 0.7 --min-speech 800 --min-silence 300
```

## 标注功能

| 功能 | 说明 |
|------|------|
| 登录与排行榜 | 姓名登录，右侧可折叠排行榜实时显示各标注员贡献 |
| 任务自动分配 | 登录即随机分配待标注文件，文件锁防并发 |
| 段播放 | 每段独立播放按钮，播完自动停止 |
| 连续播放 | 顶部音频播放器连续播放全部 |
| VAD 区间编辑 | 手动调整起止时间（毫秒精度），约束不重叠 |
| 跳过标记 | 整文件跳过：Noisy（噪声）/ Not Egyptian（非埃及方言）/ Poor Quality（音质差），三选一 |
| 质量标记 | 每段可标记 Bad Quality（不参与训练），无需填文本即可提交 |
| 标注完成 | ✓ Mark Done 提交，自动校验所有段均有文本 |
| 历史导航 | ↩ Back / Next ▶ 在最近 10 个文件间往返，可重新编辑已提交文件 |
| ASR 预标注 | 自动填入转写结果（灰色参考文字），标注者手动输入确认 |
| 自动保存 | 输入 3 秒 / 失焦 / Bad Quality / 提交前即时保存 |
| 波形图 | 对称波形，点击跳转，播放自动滚动 |
| 备份 | 每次保存同步写入备份目录 |

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Enter` | 登录页提交姓名 |
| `Tab` / `Shift + Tab` | 文本框跳到下一段 / 上一段 |
| `Ctrl + S`（Mac 为 `Cmd + S`） | 保存当前文件所有修改 |

## 标注 JSON 格式

```json
{
  "audio": "recording_01.mp3",
  "folder": "开罗方言",
  "duration": 120.5,
  "status": "annotated",
  "skip_reasons": [],
  "category": "Other-Politics",
  "annotated_by": "张三",
  "skipped_by": null,
  "last_modified": "2026-08-05T17:24:18",
  "segments": [
    {
      "id": 1,
      "start": 0.5,
      "end": 4.2,
      "duration": 3.7,
      "asr_text": "مرحبا بكم في حلقة اليوم",
      "text": "مرحبا بكم في حلقة اليوم",
      "exclude_from_training": false
    }
  ]
}
```

## VAD 参数调优

| 参数 | 默认 | 说明 |
|------|------|------|
| `min_speech_duration_ms` | 3000 | 最短语音段（ms） |
| `min_silence_duration_ms` | 300 | 最短静音间隔（ms） |
| `threshold` | 0.5 | VAD 置信度，噪声大时调高 |
| `max_segment_duration_s` | 30 | 单段最长时长（秒） |
| `speech_pad_ms` | 100 | 段前后填充（ms） |

参数在 `config.json` 的 `vad` 段中修改，保存后重新运行预处理即可（未处理的文件生效）。

## 24 小时监控（monitor.sh）

`monitor.sh` 由 crontab 每分钟执行，检测本地后端（`http://localhost:8080/api/health`）与公网入口（`https://arabic-annotation.top/api/health`）两个健康地址，故障时自动重启对应服务，并带熔断保护（30 分钟内最多重启 5 次）。配置方法见 [DEPLOY.md](DEPLOY.md) 的「24小时监控」一节。旧脚本 `health_check.sh` 已被取代。
