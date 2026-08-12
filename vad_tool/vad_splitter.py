#!/usr/bin/env python3
"""
基于 silero-vad 的音频自动断句工具
遍历文件夹，检测语音段，按「原文件名_序号.wav」保存。
"""

import argparse
import json
import os
from pathlib import Path

import soundfile as sf
import torch

# ============================================================
# 默认配置
# ============================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"

DEFAULTS = {
    "min_speech_duration_ms": 500,    # 最短语音段 (低于此忽略)
    "min_silence_duration_ms": 400,   # 最短静音间隔 (用于切分)
    "threshold": 0.5,                 # VAD 置信度阈值 (0~1, 越高越严格)
    "max_segment_duration_s": 30,     # 单段最长时长 (超过则自动切分)
    "speech_pad_ms": 100,             # 语音段前后填充 (避免截断)
    "sample_rate": 16000,             # 处理采样率
}


def load_config():
    config = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config.update(json.load(f))
    return config


# ============================================================
# VAD 模型加载（单例）
# ============================================================

_vad_model = None
_get_speech_timestamps = None


def get_vad_model():
    """加载 silero-vad 模型和工具函数（仅首次调用时加载）"""
    global _vad_model, _get_speech_timestamps

    if _vad_model is None:
        # 使用 silero-vad pip 包
        from silero_vad import load_silero_vad, get_speech_timestamps

        _vad_model = load_silero_vad()
        _get_speech_timestamps = get_speech_timestamps

    return _vad_model, _get_speech_timestamps


# ============================================================
# 核心：单文件断句
# ============================================================

def split_audio(input_path, output_dir, config):
    """
    对单个音频文件执行 VAD 断句。

    参数:
        input_path:   输入音频路径
        output_dir:   输出目录
        config:       配置字典

    返回:
        生成的片段文件路径列表
    """
    model, get_speech_timestamps = get_vad_model()

    # --- 加载音频，统一重采样到目标采样率 ---
    try:
        waveform, orig_sr = sf.read(str(input_path), dtype="float32")
    except Exception as e:
        print(f"  ⚠️  加载失败: {e}")
        return []

    # 转 mono
    if waveform.ndim > 1:
        waveform = waveform.mean(axis=1)

    # 归一化（提升不同录音音量下的检测鲁棒性）
    peak = abs(waveform).max()
    if peak > 0:
        waveform = waveform / peak * 0.9

    # 转 torch tensor
    wav_tensor = torch.from_numpy(waveform).unsqueeze(0)  # (1, samples)

    # 重采样
    target_sr = config["sample_rate"]
    if orig_sr != target_sr:
        import torchaudio.transforms as T
        resampler = T.Resample(orig_sr, target_sr)
        wav_tensor = resampler(wav_tensor)

    sample_rate = target_sr
    wav = wav_tensor.squeeze()

    # --- VAD 检测 ---
    speech_timestamps = get_speech_timestamps(
        wav,
        model,
        threshold=config["threshold"],
        sampling_rate=sample_rate,
        min_speech_duration_ms=config["min_speech_duration_ms"],
        min_silence_duration_ms=config["min_silence_duration_ms"],
        speech_pad_ms=config["speech_pad_ms"],
        max_speech_duration_s=config["max_segment_duration_s"],
    )

    if not speech_timestamps:
        print(f"  ℹ️  未检测到语音段")
        return []

    # --- 导出片段 ---
    base_name = Path(input_path).stem
    saved_files = []

    for i, seg in enumerate(speech_timestamps, 1):
        start = int(seg["start"])
        end = int(seg["end"])
        chunk = wav[start:end]
        segment_dur = float(end - start) / sample_rate

        out_name = f"{base_name}_{i:03d}.wav"
        out_path = os.path.join(output_dir, out_name)

        try:
            # 保存为 WAV
            chunk_np = chunk.numpy()
            sf.write(out_path, chunk_np, sample_rate)
            saved_files.append(out_path)
            print(f"    → {out_name}  ({segment_dur:.1f}s)")
        except Exception as e:
            print(f"    ⚠️  保存失败 {out_name}: {e}")

    return saved_files


# ============================================================
# 批量处理
# ============================================================

def process_directory(input_dir, output_dir, config):
    """遍历目录，处理所有音频文件"""
    input_path = Path(input_dir)
    if not input_path.exists():
        print(f"❌ 输入目录不存在: {input_dir}")
        return

    os.makedirs(output_dir, exist_ok=True)

    exts = set(config.get("extensions", [".wav", ".mp3", ".flac", ".ogg", ".m4a"]))
    output_abs = str(Path(output_dir).resolve())

    # 收集所有音频文件
    audio_files = []
    for root, dirs, files in os.walk(input_path):
        # 跳过输出目录
        dirs[:] = [d for d in dirs if os.path.join(root, d) != output_abs]
        for f in sorted(files):
            if Path(f).suffix.lower() in exts and not f.startswith("."):
                audio_files.append(os.path.join(root, f))

    if not audio_files:
        print("❌ 未找到音频文件")
        print(f"   目录: {input_dir}")
        print(f"   支持格式: {', '.join(exts)}")
        return

    print(f"📁 找到 {len(audio_files)} 个音频文件")
    print(f"📂 输出目录: {output_dir}")
    print(f"⚙️  阈值: {config['threshold']}  |  最短语音: {config['min_speech_duration_ms']}ms  |  最短静音: {config['min_silence_duration_ms']}ms  |  最长段: {config['max_segment_duration_s']}s")
    print()

    total_segments = 0

    for i, audio_path in enumerate(audio_files, 1):
        fname = os.path.basename(audio_path)
        print(f"[{i}/{len(audio_files)}] {fname}")

        segments = split_audio(audio_path, output_dir, config)
        total_segments += len(segments)

        if not segments:
            print(f"    (无语音段)")

    print()
    print(f"✅ 完成！共生成 {total_segments} 个语音片段")


# ============================================================
# 命令行入口
# ============================================================

def main():
    config = load_config()

    parser = argparse.ArgumentParser(
        description="基于 silero-vad 的音频自动断句工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 vad_splitter.py
  python3 vad_splitter.py -i ./raw_audio -o ./segments
  python3 vad_splitter.py -t 0.7 --min-speech 800 --min-silence 300
        """,
    )
    parser.add_argument(
        "--input", "-i",
        default=config.get("input_dir", "./input"),
        help=f"输入音频目录 (默认: {config.get('input_dir', './input')})",
    )
    parser.add_argument(
        "--output", "-o",
        default=config.get("output_dir", "./output"),
        help=f"输出目录 (默认: {config.get('output_dir', './output')})",
    )
    parser.add_argument(
        "--threshold", "-t",
        type=float,
        default=config.get("threshold", 0.5),
        help=f"VAD 置信度阈值 0~1, 越高越严格 (默认: {config.get('threshold', 0.5)})",
    )
    parser.add_argument(
        "--min-speech",
        type=int,
        default=config.get("min_speech_duration_ms", 500),
        help=f"最短语音段 ms (默认: {config.get('min_speech_duration_ms', 500)})",
    )
    parser.add_argument(
        "--min-silence",
        type=int,
        default=config.get("min_silence_duration_ms", 400),
        help=f"最短静音间隔 ms (默认: {config.get('min_silence_duration_ms', 400)})",
    )
    parser.add_argument(
        "--max-dur",
        type=int,
        default=config.get("max_segment_duration_s", 30),
        help=f"单段最长时长 s, 超过自动切分 (默认: {config.get('max_segment_duration_s', 30)})",
    )
    parser.add_argument(
        "--pad",
        type=int,
        default=config.get("speech_pad_ms", 100),
        help=f"语音段前后填充 ms (默认: {config.get('speech_pad_ms', 100)})",
    )
    args = parser.parse_args()

    # 合并配置
    run_config = {
        **config,
        "threshold": args.threshold,
        "min_speech_duration_ms": args.min_speech,
        "min_silence_duration_ms": args.min_silence,
        "max_segment_duration_s": args.max_dur,
        "speech_pad_ms": args.pad,
    }

    print("=" * 56)
    print("  🎙️  VAD 音频断句工具 (silero-vad)")
    print("=" * 56)

    # 预热模型
    print("  ⏳ 加载 VAD 模型...")
    get_vad_model()
    print("  ✅ 模型就绪")
    print()

    process_directory(args.input, args.output, run_config)


if __name__ == "__main__":
    main()
