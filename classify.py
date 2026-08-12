#!/usr/bin/env python3
"""
音频分类脚本：基于 ASR 转写文本，使用 DashScope 文本模型进行分类。
10 个预定义类别 + "Other-<实际分类>"。
结果写入标注 JSON 的 category 字段，支持断点续传。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"

CATEGORIES = [
    "Restaurant",
    "Hotel",
    "Taxi",
    "Airport",
    "Clinic",
    "Tourism information",
    "Emergencies",
    "Spoken languages",
    "Business negotiation",
    "Shopping",
]

SYSTEM_PROMPT = f"""You are an audio content classifier for Egyptian Arabic conversational audio.

Given the Arabic transcription of an audio conversation, classify it into exactly ONE of these 10 categories:

{chr(10).join(f'- {c}' for c in CATEGORIES)}

If the audio does NOT fit any of the 10 categories, output: Other-<category>
where <category> is a concise English label for the actual topic (e.g., Other-Politics, Other-Sports, Other-Religion, Other-Entertainment, Other-Education).

Rules:
- Output ONLY the category name, nothing else.
- Do NOT include any number, bullet, dash, or prefix — just the bare category text.
- No explanation, no punctuation, no extra text.
- Match case exactly for the 10 categories.
- For Other, use Title-Case with a hyphen (e.g., Other-Traffic, Other-Family)."""


def load_config():
    c = {"annotations_dir": "./annotations", "asr": {"api_key": ""}}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            c.update(json.load(f))
    return c


def call_classify(text, api_key, model):
    """调用 DashScope 文本模型分类，返回类别名"""
    import dashscope
    from dashscope import Generation

    dashscope.api_key = api_key

    resp = Generation.call(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
        max_tokens=20,
    )

    if resp.status_code == 200:
        result = resp.output.get("text", "").strip()
        # 清理可能的多余输出
        for line in result.split("\n"):
            line = line.strip().strip('"').strip("'").strip(".")
            if line:
                return line
        return result
    else:
        code = str(getattr(resp, "code", "") or "")
        msg = str(getattr(resp, "message", "") or "")
        # 内容审核拦截 → 返回特殊标记
        if "DataInspection" in code or "inappropriate" in msg.lower():
            return "REJECTED"
        raise RuntimeError(f"API error {resp.status_code}: {code} - {msg}")


def get_transcription(data):
    """拼接所有 segment 的转写文本"""
    segs = data.get("segments", [])
    parts = []
    for s in segs:
        t = (s.get("text", "") or s.get("asr_text", "") or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts)


def main():
    config = load_config()
    api_key = config.get("asr", {}).get("api_key", "")
    if not api_key:
        print("❌ 未配置 api_key")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="音频分类脚本")
    parser.add_argument("--ann-dir", "-d", default=config.get("annotations_dir", "./annotations"))
    parser.add_argument("--model", "-m", default="qwen-turbo", help="DashScope 模型 (默认 qwen-turbo)")
    parser.add_argument("--force", "-f", action="store_true", help="强制重新分类")
    parser.add_argument("--dry-run", "-n", action="store_true", help="仅统计，不实际分类")
    parser.add_argument("--limit", "-l", type=int, default=0, help="限制处理数量")
    args = parser.parse_args()

    ann_dir = Path(args.ann_dir)
    if not ann_dir.exists():
        print(f"❌ 标注目录不存在: {ann_dir}")
        sys.exit(1)

    # 收集待分类文件
    json_files = sorted(ann_dir.rglob("*.json"))
    pending = []
    skipped = []
    no_text = []

    for jf in json_files:
        if jf.name.startswith("."):
            continue
        try:
            data = json.load(open(jf, "r", encoding="utf-8"))
        except Exception:
            continue

        text = get_transcription(data)
        if not text:
            no_text.append(jf)
            continue

        if not args.force and "category" in data:
            skipped.append(jf)
            continue

        pending.append((jf, data, text))

    print(f"\n{'='*56}")
    print(f"  🏷️  音频分类 (模型: {args.model})")
    print(f"{'='*56}")
    print(f"  标注目录: {ann_dir}")
    print(f"  总文件: {len(json_files)}")
    print(f"  已分类: {len(skipped)}")
    print(f"  无文本: {len(no_text)}")
    print(f"  待分类: {len(pending)}")
    print()

    if args.dry_run:
        if pending:
            print("  待分类样本 (前5条):")
            for jf, _, text in pending[:5]:
                preview = text[:120].replace("\n", " ")
                print(f"    {jf.name}: {preview}...")
        sys.exit(0)

    if not pending:
        print("✅ 全部已分类")
        sys.exit(0)

    if args.limit:
        pending = pending[:args.limit]
        print(f"  ⚠️ 限制处理 {args.limit} 条")

    # 初始化 DashScope
    import dashscope
    dashscope.api_key = api_key

    # 分类
    stats = {}
    success = 0
    fail = 0
    t0 = time.time()

    for i, (jf, data, text) in enumerate(pending, 1):
        fname = jf.name
        print(f"[{i}/{len(pending)}] {fname} ...", end=" ", flush=True)

        try:
            # 截断过长文本（qwen-turbo 上下文 8K tokens，保守取 6000 字符）
            if len(text) > 6000:
                text = text[:6000]
            category = call_classify(text, api_key, args.model)

            if category == "REJECTED":
                print(f"⛔ 内容审核拦截")
                data["category"] = "Rejected"
                stats["Rejected"] = stats.get("Rejected", 0) + 1
            else:
                print(f"→ {category}")
                data["category"] = category
                stats[category] = stats.get(category, 0) + 1

            tmp = str(jf) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(jf))
            success += 1

        except Exception as e:
            print(f"❌ {e}")
            fail += 1
            # 错误后等一会
            time.sleep(2)

        # 进度报告
        if i % 50 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  --- 进度: {i}/{len(pending)} | 速度: {rate:.1f}个/分钟 | 耗时: {elapsed:.0f}s ---")

    elapsed = time.time() - t0
    print(f"\n{'='*56}")
    print(f"  完成: {success} 成功 / {fail} 失败")
    print(f"  耗时: {elapsed:.0f}s ({elapsed/60:.1f}分钟)")
    print(f"\n  类别分布:")
    for cat in sorted(stats.keys()):
        bar = "█" * max(1, stats[cat] // max(1, success // 30))
        print(f"    {cat:30s} {stats[cat]:5d}  {bar}")
    print(f"{'='*56}")


if __name__ == "__main__":
    main()
