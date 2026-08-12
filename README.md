# 埃及方言音频标注工具

基于 Web 的阿拉伯语（埃及方言）音频标注平台，集成 VAD 自动断句、ASR 预标注、内容分类和波形可视化。

## 功能概览

| 功能 | 说明 |
|------|------|
| **VAD 自动断句** | silero-vad 6.x 语音活动检测，自动切分语音段 |
| **ASR 预标注** | 阿里云 DashScope Qwen3.5-Omni 多模态大模型阿拉伯语转写 |
| **内容分类** | 基于转写文本自动分类（DashScope Qwen-Turbo） |
| **波形可视化** | Canvas 对称波形图，点击跳转，播放自动滚动 |
| **标注编辑** | 在线修改转写文本、调整时间戳、标记质量 |
| **文件夹管理** | 树形文件浏览器，进度统计 |
| **自动保存** | 切换音频即时保存，文件锁防并发冲突 |
| **Cloudflare 部署** | 命名隧道 + 自有域名，全球 CDN 加速，HTTPS 自动证书 |

## 项目结构

```
annotation_tool/
├── server.py                    # Flask HTTP 服务器（标注 API）
├── index.html                   # 标注前端单页应用
├── preprocess.py                # 音频预处理（VAD 断句 + ASR 预标注）
├── classify.py                  # 长音频内容分类
├── gunicorn_config.py           # Gunicorn 生产配置
├── config.example.json          # 配置模板
├── requirements.txt             # Python 依赖
├── audio-annotator.service      # Gunicorn systemd 服务
├── cloudflared-tunnel.service   # Cloudflare 隧道 systemd 服务
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
| 后端 | Flask + Gunicorn | HTTP API + 多进程并发 |
| 前端 | 原生 HTML/CSS/JS | 单页应用，Canvas 波形图 |
| 隧道 | Cloudflare Tunnel | 命名隧道 + 自有域名，全球 CDN |

## 配置

复制 `config.example.json` 为 `config.json`，修改以下参数：

```json
{
  "audio_dir": "/path/to/audio",
  "annotations_dir": "/path/to/annotations",
  "port": 8080,
  "backup_dir": "/path/to/backup",
  "vad": {
    "min_speech_duration_ms": 3000,
    "min_silence_duration_ms": 300,
    "threshold": 0.5,
    "max_segment_duration_s": 30,
    "speech_pad_ms": 100
  },
  "asr": {
    "api_key": "sk-xxx",
    "model": "qwen3.5-omni-plus-2026-03-15",
    "language": "ar",
    "workers": 10
  }
}
```

## 工作流程

### 1. 准备音频

将音频文件（.mp3/.wav/.flac 等）放入 `audio_dir` 目录，支持子文件夹。目录结构镜像到标注目录。

### 2. 预处理（VAD 断句 + ASR 预标注）

```bash
# 仅 VAD 断句（无 ASR）
python3 preprocess.py

# VAD + ASR 预标注（需配置 api_key）
python3 preprocess.py -w 10

# 强制重新处理所有文件
python3 preprocess.py -w 10 --force
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

# 生产模式（Gunicorn，4 workers × 4 threads）
gunicorn -c gunicorn_config.py server:app
```

### 5. 部署到公网

参见 [DEPLOY.md](DEPLOY.md)，包含 systemd 配置、Cloudflare 命名隧道设置、域名配置等。

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
| 文件夹视图 | 左侧树形展示，显示各文件夹标注进度 |
| 段播放 | 每段独立播放按钮，播完自动停止 |
| 连续播放 | 顶部音频播放器连续播放全部 |
| VAD 区间编辑 | 手动调整起止时间（毫秒精度），约束不重叠 |
| 跳过标记 | 噪声过大 / 非埃及方言 / 音质太差 |
| 质量标记 | 每段可标记「质量差」，不适合训练 |
| 标注完成 | 确认整段音频标注完成 |
| ASR 预标注 | 自动填入转写结果，标注者修改确认 |
| 自动保存 | 切换音频、标注完成、跳过标记均即时保存 |
| 波形图 | 对称波形，点击跳转，播放自动滚动 |
| 备份 | 每次保存同步写入备份目录 |

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Tab` | 下一段 |
| `Shift + Tab` | 上一段 |
| `Ctrl + S` | 保存 |

## 标注 JSON 格式

```json
{
  "audio": "recording_01.mp3",
  "folder": "开罗方言",
  "duration": 120.5,
  "status": "annotated",
  "skip_reasons": [],
  "category": "Other-Politics",
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
  ],
  "last_modified": "2026-08-05T17:24:18"
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

```bash
# 命令行临时覆盖
python3 preprocess.py --min-speech 2000 --min-silence 500
```
