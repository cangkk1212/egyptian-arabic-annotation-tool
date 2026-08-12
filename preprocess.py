#!/usr/bin/env python3
"""
音频批量预处理：VAD断句 + Qwen3.5-Omni ASR预标注
500小时音频优化版 — 多线程并发ASR，断点续传，不保存中间音频文件
"""

import argparse, base64, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import soundfile as sf
import torch

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".webm", ".opus", ".wma", ".aac"}

# ============================================================
# 配置
# ============================================================
def load_config():
    c = {
        "audio_dir": "./audio", "annotations_dir": "./annotations", "vad": {"min_speech_duration_ms": 3000,
        "min_silence_duration_ms": 300, "threshold": 0.5, "max_segment_duration_s": 30,
        "speech_pad_ms": 100}, "asr": {"api_key": "", "model": "qwen3.5-omni-plus",
        "language": "ar", "workers": 10},
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            for k in loaded:
                if k in c and isinstance(c[k], dict) and isinstance(loaded[k], dict):
                    c[k].update(loaded[k])
                else:
                    c[k] = loaded[k]
    return c


# ============================================================
# VAD 模块
# ============================================================
_vad = None

def get_vad():
    global _vad
    if _vad is None:
        from silero_vad import load_silero_vad, get_speech_timestamps
        model = load_silero_vad()
        _vad = (model, get_speech_timestamps)
    return _vad


def run_vad(audio_path, vad_cfg):
    model, get_ts = get_vad()
    wav, orig_sr = sf.read(str(audio_path), dtype="float32")
    if wav.ndim > 1: wav = wav.mean(axis=1)
    peak = abs(wav).max()
    if peak > 0: wav = wav / peak * 0.9
    target_sr = 16000
    wav_t = torch.from_numpy(wav).unsqueeze(0)
    if orig_sr != target_sr:
        import torchaudio.transforms as T
        wav_t = T.Resample(orig_sr, target_sr)(wav_t)
    wav_mono = wav_t.squeeze()
    segs = get_ts(wav_mono, model, threshold=vad_cfg.get("threshold", 0.5),
        sampling_rate=target_sr, min_speech_duration_ms=vad_cfg.get("min_speech_duration_ms", 3000),
        min_silence_duration_ms=vad_cfg.get("min_silence_duration_ms", 300),
        speech_pad_ms=vad_cfg.get("speech_pad_ms", 100),
        max_speech_duration_s=vad_cfg.get("max_segment_duration_s", 30))
    return [{"id": i+1, "start": s["start"]/target_sr, "end": s["end"]/target_sr,
             "duration": (s["end"]-s["start"])/target_sr, "asr_text": "", "text": "",
             "exclude_from_training": False}
            for i, s in enumerate(segs)], wav, orig_sr, target_sr


# ============================================================
# ASR 模块（多线程 + Qwen-Omni）
# ============================================================
_asr_init = False

def init_asr(api_key):
    global _asr_init
    if not _asr_init:
        import dashscope
        dashscope.api_key = api_key
        _asr_init = True

_quota_exhausted = False

def call_asr_one(seg_id, audio_chunk, sr, asr_cfg):
    """单个segment的ASR调用。返回 (seg_id, text) 或 (seg_id, "QUOTA") 表示配额耗尽"""
    global _quota_exhausted
    if _quota_exhausted:
        return seg_id, "QUOTA"
    import tempfile, wave, struct
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            with wave.open(tmp.name, "w") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
                frames = [int(max(-32767, min(32767, x*32767))) for x in audio_chunk]
                w.writeframes(struct.pack("<"+"h"*len(frames), *frames))

        from dashscope import MultiModalConversation
        resp = MultiModalConversation.call(
            model=asr_cfg.get("model", "qwen3.5-omni-plus"),
            messages=[{"role": "user", "content": [
                {"audio": f"file://{tmp.name}"},
                {"text": "请转写这段阿拉伯语音频，只输出阿拉伯语转写文本"}
            ]}]
        )
        os.unlink(tmp.name)

        if resp.status_code == 200:
            choices = resp.output.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if isinstance(content, list) and len(content) > 0:
                    return seg_id, content[0].get("text", "") or ""
                return seg_id, str(content).strip()
        # 非200响应：速率限制重试，其余停止
        code = str(getattr(resp, 'code', '') or '')
        msg = str(getattr(resp, 'message', '') or '')
        if resp.status_code == 429 or 'rate' in msg.lower() or 'rate' in code.lower():
            import time as _t, random as _r
            wait = 1 + _r.random() * 2
            _t.sleep(wait)
            return seg_id, ""
        # 内容审核拦截 → 删除该音频
        if 'DataInspection' in code or 'inappropriate' in msg.lower():
            print(f"       🗑️  内容审核拦截，将删除此音频")
            return seg_id, "DELETE"
        # 其他所有错误 → 停止
        _quota_exhausted = True
        print(f"       ⚠️  API调用失败，停止ASR")
        print(f"       状态码: {resp.status_code}")
        print(f"       错误码: {code}")
        print(f"       错误信息: {msg}")
        return seg_id, "QUOTA"
    except Exception as e:
        emsg = str(e)
        if '429' in emsg or 'rate' in emsg.lower():
            import time as _t, random as _r
            wait = 1 + _r.random() * 2
            _t.sleep(wait)
            return seg_id, ""
        _quota_exhausted = True
        import traceback
        print(f"       ⚠️  异常，停止ASR")
        print(f"       类型: {type(e).__name__}")
        print(f"       信息: {emsg[:300]}")
        return seg_id, "QUOTA"


def process_audio_with_asr(audio_path, config):
    """单个音频完整处理：VAD + 多线程ASR"""
    fname = Path(audio_path).name
    audio_dir = Path(config["audio_dir"])
    rel_path = str(Path(audio_path).relative_to(audio_dir))
    name_no_ext = rel_path.rsplit(".", 1)[0].replace("|", "-")
    ann_dir = config.get("annotations_dir", str(Path(config["audio_dir"]) / "annotations"))
    out_dir = Path(ann_dir)
    # 镜像音频目录结构
    parts = name_no_ext.rsplit("/", 1)
    if len(parts) == 2:
        out_dir = out_dir / parts[0]
        fname = parts[1]
    else:
        fname = name_no_ext
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{fname}.json"

    # 跳过已有手动标注的（text != asr_text 说明人工修改过）
    if out_path.exists():
        try:
            existing = json.load(open(out_path, "r", encoding="utf-8"))
            for s in existing.get("segments", []):
                t = (s.get("text", "") or "").strip()
                a = (s.get("asr_text", "") or "").strip()
                if t and t != a:
                    print(f"  ⏭️  已有手动标注，跳过: {fname}")
                    return
        except: pass

    print(f"  🎙️  {fname}")
    t0 = time.time()

    # 1. VAD
    segs, wav_full, orig_sr, vad_sr = run_vad(audio_path, config.get("vad", {}))
    n_segs = len(segs)
    print(f"     VAD: {n_segs} 段 ({time.time()-t0:.1f}s)")

    if not segs:
        print(f"     ⚠️  未检测到语音，删除音频及标注")
        audio_path.unlink(missing_ok=True)
        if out_path.exists(): out_path.unlink()
        return

    # 2. 准备音频片段
    wav = wav_full.mean(axis=1) if wav_full.ndim > 1 else wav_full
    chunks = []
    for seg in segs:
        s_start = int(seg["start"] * orig_sr)
        s_end = int(seg["end"] * orig_sr)
        chunks.append(wav[s_start:s_end])

    # 3. 多线程ASR
    asr_cfg = config.get("asr", {})
    has_key = bool(asr_cfg.get("api_key", ""))
    workers = asr_cfg.get("workers", 10)

    if has_key:
        init_asr(asr_cfg["api_key"])
        global _quota_exhausted; _quota_exhausted = False
        # 只看没有asr_text的段
        todo = [(i, chunks[i]) for i in range(n_segs) if not segs[i].get("asr_text", "").strip()]
        print(f"     ASR: {len(todo)}/{n_segs} 段待转写 ({workers}线程)")

        done = 0; quota_stop = False; _delete_file = False
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(call_asr_one, i, chunks[i], orig_sr, asr_cfg): i for i, _ in todo}
            for future in as_completed(futures):
                seg_i, text = future.result()
                if text == "QUOTA":
                    quota_stop = True
                elif text == "DELETE":
                    _delete_file = True
                    break  # 该文件剩余段无需处理，直接删除
                elif text and not text.startswith("ERR:"):
                    segs[seg_i]["asr_text"] = text
                    segs[seg_i]["text"] = text
                done += 1
                if done % 20 == 0:
                    print(f"       ASR进度: {done}/{len(todo)}")
                if quota_stop:
                    print(f"     ⚠️  异常中断，已处理 {done}/{len(todo)} 段，已保存")
                    print(f"     ⛔ 停止整个预处理脚本")
                    import sys; sys.exit(1)
        if quota_stop and not _delete_file:
            print(f"     ⚠️  ASR中断，已保存 {done}/{len(todo)} 段 ({time.time()-t0:.1f}s)")
        elif not _delete_file:
            print(f"     ASR完成 ({time.time()-t0:.1f}s)")
    else:
        print(f"     ⚠️  未配置API Key，跳过ASR")

    # 内容审核拦截 → 删除音频及标注
    if _delete_file:
        print(f"     🗑️  删除音频: {fname}")
        audio_path.unlink(missing_ok=True)
        if out_path.exists(): out_path.unlink()
        return

    # 4. 生成波形缓存（base64 int16）
    wav_mono = wav_full.mean(axis=1) if wav_full.ndim > 1 else wav_full
    total_dur = len(wav_mono) / orig_sr
    target_pts = max(2000, int(total_dur * 5))
    step = max(1, len(wav_mono) // target_pts)
    raw = [int(max(-32767, min(32767, wav_mono[i]*32767))) for i in range(0, len(wav_mono), step)]
    import struct as _st
    wf_b64 = base64.b64encode(_st.pack("<"+"h"*len(raw), *raw)).decode()

    # 5. 保存
    data = {
        "audio": fname, "folder": str(Path(audio_path).parent.relative_to(audio_dir))
                  if str(Path(audio_path).parent) != str(audio_dir) else "",
        "duration": round(total_dur, 2), "status": "pending", "skip_reasons": [],
        "segments": segs, "waveform_b64": wf_b64,
        "preprocessed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(str(out_path)+".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(str(out_path)+".tmp", str(out_path))
    print(f"     ✅ 已保存: {out_path.name}")


# ============================================================
# 主流程
# ============================================================
def main():
    config = load_config()
    parser = argparse.ArgumentParser(description="音频批量预处理 VAD+ASR (500h优化版)")
    parser.add_argument("--audio-dir", "-d", default=config.get("audio_dir", "./audio"))
    parser.add_argument("--workers", "-w", type=int, default=config.get("asr", {}).get("workers", 10),
                        help="ASR并发线程数(默认10)")
    parser.add_argument("--force", "-f", action="store_true", help="强制重新处理所有文件")
    args = parser.parse_args()

    config["audio_dir"] = str(Path(args.audio_dir).resolve())
    config["asr"]["workers"] = args.workers

    audio_dir = Path(config["audio_dir"])
    ann_dir = Path(config.get("annotations_dir", str(audio_dir / "annotations")))
    ann_dir.mkdir(parents=True, exist_ok=True)

    # 收集音频文件
    audio_files = []
    for root, dirs, files in os.walk(audio_dir):
        dirs[:] = [d for d in dirs if d != "annotations" and not d.startswith(".")]
        for f in sorted(files):
            if Path(f).suffix.lower() in AUDIO_EXTENSIONS and not f.startswith("."):
                audio_files.append(Path(root) / f)

    if not audio_files:
        print("❌ 未找到音频文件")
        return

    # 过滤已处理的
    pending = []
    for af in audio_files:
        rel = str(af.relative_to(audio_dir))
        name = rel.rsplit(".", 1)[0].replace("|", "-")
        out_path = ann_dir / f"{name}.json"
        if not args.force and out_path.exists():
            try:
                d = json.load(open(out_path, "r", encoding="utf-8"))
                has_text = any(s.get("text", "").strip() for s in d.get("segments", []))
                if has_text:
                    print(f"⏭️  已标注，跳过: {af.name}")
                    continue
            except: pass
        pending.append(af)

    print(f"\n{'='*56}\n  🎙️  音频预处理 (VAD+ASR)  500h优化版\n{'='*56}")
    print(f"  目录: {config['audio_dir']}")
    print(f"  总文件: {len(audio_files)} | 待处理: {len(pending)}")
    print(f"  ASR: {'已配置' if config['asr']['api_key'] else '❌ 未配置'} | 线程: {args.workers}")
    print(f"  VAD: min_speech={config['vad']['min_speech_duration_ms']}ms\n")

    if not pending:
        print("✅ 全部处理完毕")
        return

    # 预热VAD
    print("⏳ 加载VAD模型...")
    get_vad()
    print("✅ 就绪\n")

    t_total = time.time()
    for i, af in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}]", end=" ")
        try:
            process_audio_with_asr(af, config)
        except Exception as e:
            print(f"  ❌ 失败: {e}")

    print(f"\n✅ 全部完成 (总耗时 {time.time()-t_total:.0f}s)")


if __name__ == "__main__":
    main()
