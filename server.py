#!/usr/bin/env python3
"""
Egyptian Arabic Annotation Tool - Flask HTTP Server v4
VAD segmentation, ASR pre-annotation, waveform display, folder structure, user login.
"""

import argparse
import fcntl
import json
import logging
import os
import random
import re
import secrets
import struct
import time
from functools import wraps
from pathlib import Path

# 错误日志写入 gunicorn.log（systemd 已重定向），用于排查线上问题
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("annotator")

import soundfile as sf
from flask import Flask, request, jsonify, send_file, make_response, session, redirect, url_for

# ============================================================
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"
INDEX_PATH = SCRIPT_DIR / "index.html"
LOGIN_PATH = SCRIPT_DIR / "login.html"
SESSION_TIMEOUT_MINUTES = 30
ASSIGNMENT_TIMEOUT_HOURS = 72  # 分配超时：用户关浏览器离开超过3天后自动释放文件

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".webm", ".opus", ".wma", ".aac"}
MIME_TYPES = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".webm": "audio/webm",
    ".opus": "audio/opus", ".wma": "audio/x-ms-wma", ".aac": "audio/aac",
}
SKIP_REASONS = {"noisy": "Noisy", "not_egyptian": "Not Egyptian", "poor_quality": "Poor Quality"}

app = Flask(__name__, static_folder=None)


def load_config():
    c = {"audio_dir": "./audio", "annotations_dir": "./annotations", "port": 8080, "backup_dir": "./annotations_copy"}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            c.update(json.load(f))
    return c


# ============================================================
# Active user tracking (shared across gunicorn workers via file + fcntl)
# ============================================================

def _active_users_path():
    """Path to the active users tracking file."""
    ad = _annotations_dir()
    return ad / "active_users.json"


def load_active_users():
    """Load active users dict. Returns {} if file doesn't exist."""
    p = _active_users_path()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_active_users(data):
    """Atomically write active users dict with file locking."""
    p = _active_users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.parent / ".active_users.lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
    finally:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()


def cleanup_stale_users(users):
    """Remove users who haven't been seen for SESSION_TIMEOUT_MINUTES. Returns cleaned dict."""
    now = time.time()
    timeout_s = SESSION_TIMEOUT_MINUTES * 60
    return {name: info for name, info in users.items()
            if now - info.get("last_seen_ts", 0) < timeout_s}


def update_user_activity(username):
    """Update last_seen timestamp for an active user. Call on every authenticated request."""
    users = load_active_users()
    users = cleanup_stale_users(users)
    if username in users:
        users[username]["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        users[username]["last_seen_ts"] = time.time()
        save_active_users(users)


# ============================================================
# Task assignment tracking (shared across gunicorn workers via file + fcntl)
# ============================================================

def _assignments_path():
    return _annotations_dir() / "assignments.json"


def load_assignments():
    p = _assignments_path()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_assignments(data):
    """Atomically write assignments dict with file locking."""
    p = _assignments_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.parent / ".assignments.lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
    finally:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()


def _rel_path_to_key(rel_path):
    """Convert a rel_path like 'folder/file.wav' to annotation key like 'folder/file'."""
    return rel_path.rsplit(".", 1)[0].replace("|", "-")


def get_available_files():
    """Return list of (rel_path, name_no_ext, filename) for files not annotated/skipped and not assigned."""
    cleanup_stale_assignments()  # 先清理超时分配
    assignments = load_assignments()
    assigned_files = {v["audio"] for v in assignments.values()}

    _, flat = scan_audio_structure()
    available = []
    for rel_path in flat:
        name_no_ext = _rel_path_to_key(rel_path)
        seg_data = load_segments(name_no_ext)
        if not seg_data or not seg_data.get("segments"):
            continue
        status = seg_data.get("status", "pending")
        if status in ("annotated", "skipped"):
            continue
        if name_no_ext in assigned_files:
            continue
        available.append({
            "rel_path": rel_path,
            "name_no_ext": name_no_ext,
            "filename": rel_path.rsplit("/", 1)[-1] if "/" in rel_path else rel_path,
        })
    return available


def assign_file(username):
    """Assign a random available file to username. Returns file info dict or None."""
    available = get_available_files()
    if not available:
        return None
    chosen = random.choice(available)
    assignments = load_assignments()
    assignments[username] = {
        "audio": chosen["name_no_ext"],
        "rel_path": chosen["rel_path"],
        "assigned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_activity": time.time(),
    }
    save_assignments(assignments)
    return chosen


def release_assignment(username):
    """Remove a user's assignment."""
    assignments = load_assignments()
    assignments.pop(username, None)
    save_assignments(assignments)


def cleanup_stale_assignments():
    """Release assignments that have been inactive for > ASSIGNMENT_TIMEOUT_HOURS.
    Returns the number of stale assignments cleaned up."""
    assignments = load_assignments()
    now = time.time()
    timeout_s = ASSIGNMENT_TIMEOUT_HOURS * 3600
    stale = [
        name for name, entry in assignments.items()
        if now - entry.get("last_activity", now) > timeout_s
    ]
    for name in stale:
        del assignments[name]
    if stale:
        save_assignments(assignments)
    return len(stale)


def get_user_assignment(username):
    """Get the assignment entry for username (dict with audio, rel_path, assigned_at), or None.
    Automatically releases assignments that have been inactive for > ASSIGNMENT_TIMEOUT_HOURS."""
    assignments = load_assignments()
    entry = assignments.get(username)
    if not entry:
        return None
    # Check for timeout
    now = time.time()
    timeout_s = ASSIGNMENT_TIMEOUT_HOURS * 3600
    last_activity = entry.get("last_activity", now)
    if now - last_activity > timeout_s:
        release_assignment(username)
        return None
    return entry


# ============================================================
# Authentication decorator
# ============================================================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = session.get("user", "")
        if not user:
            return jsonify({"error": "Not authenticated", "redirect": "/login.html"}), 401
        # Refresh activity timestamp
        update_user_activity(user)
        return f(*args, **kwargs)
    return decorated


def get_audio_dir():
    return app.config.get("AUDIO_DIR", str(SCRIPT_DIR / "audio"))


# ============================================================
# 文件锁 + 分文件夹读写
# ============================================================

def _annotations_dir():
    return Path(app.config.get("ANNOTATIONS_DIR", str(Path(get_audio_dir()) / "annotations")))


def _group_by_folder(data):
    groups = {}
    for key, val in data.items():
        parts = key.split("/", 1)
        folder = parts[0] if len(parts) > 1 else "_root_"
        groups.setdefault(folder, {})[key] = val
    return groups


def load_annotations():
    ad = _annotations_dir()
    if not ad.exists():
        return {}
    all_ann = {}
    for f in sorted(ad.rglob("*.json")):
        if f.name.startswith(".") or f.parent.name.startswith("."):
            continue
        try:
            d = json.load(open(f, "r", encoding="utf-8"))
            if "segments" in d:
                key = d.get("audio", f.stem)
                folder = str(f.parent.relative_to(ad)) if f.parent != ad else ""
                full_key = f"{folder}/{key}" if folder else key
                all_ann[full_key] = d
            elif "annotations" in d:
                all_ann.update(d.get("annotations", {}))
        except (json.JSONDecodeError, OSError):
            continue
    return all_ann


def load_segments(audio_name):
    """加载单个音频的 segment 标注数据（支持子目录结构）"""
    ad = _annotations_dir()
    parts = audio_name.rsplit("/", 1)
    if len(parts) == 2:
        p = ad / parts[0] / f"{parts[1]}.json"
    else:
        p = ad / f"{audio_name}.json"
    if p.exists():
        try:
            return json.load(open(p, "r", encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_segments(audio_name, data):
    """保存单个音频的 segment 标注数据（镜像音频目录结构）"""
    ad = _annotations_dir()
    # audio_name 可能是 "folder/file" 格式，创建对应子目录
    parts = audio_name.rsplit("/", 1)
    if len(parts) == 2:
        sub_dir, fname = parts
        ad = ad / sub_dir
    else:
        fname = audio_name
    ad.mkdir(parents=True, exist_ok=True)

    lock_path = _annotations_dir() / ".lock"
    json_path = ad / f"{fname}.json"

    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)

        data["last_modified"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = str(json_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(json_path))
    finally:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()


def backup_segments(audio_name, data):
    """备份，镜像音频目录结构"""
    backup_root = app.config.get("BACKUP_DIR", "")
    if not backup_root:
        return
    bd = Path(backup_root)
    parts = audio_name.rsplit("/", 1)
    if len(parts) == 2:
        bd = bd / parts[0]
    bd.mkdir(parents=True, exist_ok=True)

    fname = parts[1] if len(parts) == 2 else audio_name
    json_path = bd / f"{fname}.json"

    data["last_modified"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = str(json_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(json_path))


# ============================================================
# 音频扫描
# ============================================================

def scan_audio_structure():
    ap = Path(get_audio_dir())
    if not ap.exists():
        return [], []

    folder_map = {}
    flat_list = []

    for root, dirs, files in os.walk(ap):
        dirs[:] = [d for d in sorted(dirs) if d != "annotations" and not d.startswith(".")]
        rel_dir = str(Path(root).relative_to(ap))
        if rel_dir == ".":
            rel_dir = ""

        for fname in sorted(files):
            if fname.startswith(".") or Path(fname).suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            rel_path = f"{rel_dir}/{fname}" if rel_dir else fname
            # 标注文件名：用相对路径（去除扩展名），/ 替换为 _
            name_no_ext = rel_path.rsplit(".", 1)[0].replace("|", "-")

            # 检查是否有 segment 数据
            seg_data = load_segments(name_no_ext)
            status = "pending"
            if seg_data:
                status = seg_data.get("status", "pending")

            file_info = {
                "filename": fname,
                "name_no_ext": name_no_ext,
                "rel_path": rel_path,
                "status": status,
            }
            if status == "skipped" and seg_data:
                file_info["skip_reasons"] = seg_data.get("skip_reasons", [])
            if seg_data:
                file_info["segment_count"] = len(seg_data.get("segments", []))
                file_info["duration"] = seg_data.get("duration", 0)

            flat_list.append(rel_path)
            folder_map.setdefault(rel_dir, []).append(file_info)

    folders = []
    if "" in folder_map:
        folders.append({"name": "", "display": "📁 根目录", "files": folder_map.pop("")})
    for fn in sorted(folder_map.keys()):
        folders.append({"name": fn, "display": f"📁 {fn}", "files": folder_map[fn]})

    return folders, flat_list


# ============================================================
# 波形生成
# ============================================================

@app.route("/api/waveform/<path:filename>")
@login_required
def api_waveform(filename):
    """返回音频波形采样点（用于前端波形绘制）"""
    ap = Path(get_audio_dir())
    requested = Path(filename)
    safe_parts = [p for p in requested.parts if p != ".."]
    safe_path = ap.joinpath(*safe_parts)

    if not safe_path.exists() or not safe_path.is_file():
        return jsonify({"error": "not found"}), 404

    try:
        safe_path.resolve().relative_to(ap.resolve())
    except ValueError:
        return jsonify({"error": "access denied"}), 403

    # 读取音频，降采样到约 2000 个点
    wav, sr = sf.read(str(safe_path), dtype="float32")
    if wav.ndim > 1:
        wav = wav.mean(axis=1)

    target_points = 2000
    step = max(1, len(wav) // target_points)
    samples = [float(wav[i]) for i in range(0, len(wav), step)]

    return jsonify({
        "samples": samples,
        "sample_rate": sr,
        "duration": len(wav) / sr,
        "points": len(samples),
    })


# ============================================================
# API 端点
# ============================================================

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ============================================================
# Page routes
# ============================================================

@app.route("/")
@app.route("/index.html")
def serve_index():
    """Serve the main annotation interface. Requires login."""
    if not session.get("user"):
        return redirect("/login.html")
    if not INDEX_PATH.exists():
        return jsonify({"error": "index.html not found"}), 500
    return send_file(str(INDEX_PATH), mimetype="text/html")


@app.route("/login.html")
def serve_login():
    """Serve the login page."""
    if not LOGIN_PATH.exists():
        return jsonify({"error": "login.html not found"}), 500
    return send_file(str(LOGIN_PATH), mimetype="text/html")


# ============================================================
# Authentication API
# ============================================================

@app.route("/api/login", methods=["POST"])
def api_login():
    """Log in with a username. No password required. Name must be unique."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400

    username = data.get("username", "").strip()
    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
    if len(username) > 50:
        return jsonify({"success": False, "error": "Username too long (max 50 characters)"}), 400
    # Only allow letters, digits, spaces, dots, hyphens, underscores
    if not re.match(r'^[\w\s.\-]+$', username):
        return jsonify({"success": False, "error": "Username contains invalid characters"}), 400

    users = load_active_users()
    users = cleanup_stale_users(users)

    # Check if name is already taken by an active user
    if username in users:
        return jsonify({"success": False, "error": f"'{username}' is already in use. Please choose a different name."}), 409

    # Register the user
    now = time.time()
    users[username] = {
        "login_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_seen_ts": now,
    }
    save_active_users(users)
    session["user"] = username
    session.permanent = True

    return jsonify({"success": True, "username": username})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """Log out: release assignment, remove from active users, clear session."""
    username = session.get("user", "")
    if username:
        release_assignment(username)
        users = load_active_users()
        users.pop(username, None)
        save_active_users(users)
    session.clear()
    return jsonify({"success": True})


@app.route("/api/active-users")
def api_active_users():
    """Return list of currently active usernames."""
    users = load_active_users()
    users = cleanup_stale_users(users)
    if users != load_active_users():
        save_active_users(users)  # persist cleanup
    active = sorted(users.keys())
    return jsonify({"users": active, "count": len(active)})


@app.route("/api/leaderboard")
def api_leaderboard():
    """返回所有用户的标注统计（无需登录）。按贡献量降序排列。"""
    ad = _annotations_dir()
    user_stats = {}
    for f in sorted(ad.rglob("*.json")):
        if f.name.startswith(".") or f.name in ("active_users.json", "assignments.json"):
            continue
        try:
            d = json.load(open(f, "r", encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        status = d.get("status", "pending")
        duration = d.get("duration", 0)
        annotated_by = d.get("annotated_by", "")
        skipped_by = d.get("skipped_by", "")
        if status == "annotated" and annotated_by:
            s = user_stats.setdefault(annotated_by, {"annotated": 0, "skipped": 0, "hours": 0.0})
            s["annotated"] += 1
            s["hours"] += duration
        elif status == "skipped" and skipped_by:
            s = user_stats.setdefault(skipped_by, {"annotated": 0, "skipped": 0, "hours": 0.0})
            s["skipped"] += 1
    leaderboard = [
        {"user": u, "annotated": s["annotated"], "skipped": s["skipped"],
         "hours": round(s["hours"] / 3600, 2)}
        for u, s in sorted(user_stats.items(),
            key=lambda x: x[1]["hours"], reverse=True)
    ]
    return jsonify({"leaderboard": leaderboard})


@app.route("/api/current-user")
def api_current_user():
    """Return the currently logged-in username (from session)."""
    user = session.get("user", "")
    if not user:
        return jsonify({"user": None}), 401
    return jsonify({"user": user})


@app.route("/api/heartbeat")
@login_required
def api_heartbeat():
    """Keep the session alive and refresh assignment activity. Called periodically by the frontend."""
    username = session.get("user", "")
    if username:
        assignments = load_assignments()
        if username in assignments:
            assignments[username]["last_activity"] = time.time()
            save_assignments(assignments)
    return jsonify({"ok": True})


@app.route("/api/health")
def api_health():
    """Public health check — no auth required."""
    return jsonify({"ok": True, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")})


@app.route("/api/clientlog", methods=["POST"])
def api_clientlog():
    """接收前端上报的错误，追加写入 client_errors.log（无需登录，用于排查远端用户的问题）。"""
    data = request.get_json(silent=True) or {}
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user": session.get("user", "anonymous"),
        "type": str(data.get("type", "unknown"))[:50],
        "message": str(data.get("message", ""))[:500],
        "url": str(data.get("url", ""))[:300],
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr or ""),
    }
    try:
        with open(SCRIPT_DIR / "client_errors.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("clientlog write failed: %s", e)
    return jsonify({"success": True})


# ============================================================
# Task Assignment API (all protected)
# ============================================================

@app.route("/api/assignment")
@login_required
def api_get_assignment():
    """Get or create an assignment for the current user."""
    username = session.get("user", "")

    # 1. Check if user already has an assignment
    existing = get_user_assignment(username)
    if existing:
        audio_name = existing["audio"]
        seg_data = load_segments(audio_name)
        # Try to find rel_path from assignment (new format) or fall back to scanning
        rel_path = existing.get("rel_path", "")
        if not rel_path:
            # Backward compat: try to find the file by scanning audio_dir
            _, flat = scan_audio_structure()
            for rp in flat:
                if _rel_path_to_key(rp) == audio_name:
                    rel_path = rp
                    break
        if seg_data:
            return jsonify({
                "assigned": True,
                "audio_name": audio_name,
                "rel_path": rel_path,
                "filename": seg_data.get("audio", ""),
                "folder": seg_data.get("folder", ""),
                "duration": seg_data.get("duration", 0),
                "status": seg_data.get("status", "pending"),
                "segments": seg_data.get("segments", []),
                "skip_reasons": seg_data.get("skip_reasons", []),
                "resumed": True,
            })

    # 2. Assign a new file
    chosen = assign_file(username)
    if not chosen:
        # No files available — return stats
        _, flat = scan_audio_structure()
        annotations = load_annotations()
        annotated = sum(1 for v in annotations.values()
                        if isinstance(v, dict) and v.get("status") == "annotated")
        skipped = sum(1 for v in annotations.values()
                      if isinstance(v, dict) and v.get("status") == "skipped")
        return jsonify({
            "done": True,
            "total": len(flat),
            "annotated": annotated,
            "skipped": skipped,
            "pending": len(flat) - annotated - skipped,
        })

    # Load the chosen file's segments
    seg_data = load_segments(chosen["name_no_ext"])
    return jsonify({
        "assigned": True,
        "audio_name": chosen["name_no_ext"],
        "filename": chosen["filename"],
        "rel_path": chosen["rel_path"],
        "folder": seg_data.get("folder", "") if seg_data else "",
        "duration": seg_data.get("duration", 0) if seg_data else 0,
        "status": seg_data.get("status", "pending") if seg_data else "pending",
        "segments": seg_data.get("segments", []) if seg_data else [],
        "skip_reasons": [],
        "resumed": False,
    })


@app.route("/api/assignment/release", methods=["POST"])
@login_required
def api_release_assignment():
    """Mark the current assignment as done and release the lock."""
    username = session.get("user", "")
    data = request.get_json(silent=True) or {}
    new_status = data.get("status", "annotated")
    skip_reasons = data.get("skip_reasons", [])

    audio_name = (get_user_assignment(username) or {}).get("audio", "")
    if not audio_name:
        return jsonify({"success": False, "error": "No active assignment"}), 400

    # Save final status
    seg_data = load_segments(audio_name) or {
        "audio": f"{audio_name}.wav",
        "folder": "",
        "duration": 0,
        "status": "pending",
        "segments": [],
    }
    seg_data["status"] = new_status
    if new_status == "skipped":
        seg_data["skip_reasons"] = skip_reasons
    seg_data["last_modified"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    seg_data["last_modified_by"] = username
    if new_status == "annotated":
        seg_data["annotated_by"] = username
    if new_status == "skipped":
        seg_data["skipped_by"] = username

    try:
        backup_segments(audio_name, seg_data)
        save_segments(audio_name, seg_data)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # Release the lock
    release_assignment(username)

    return jsonify({"success": True, "next": True})


@app.route("/api/reopen", methods=["POST"])
@login_required
def api_reopen():
    """重新打开已提交的文件，允许用户返回修改。"""
    username = session.get("user", "")
    data = request.get_json(silent=True) or {}
    target_audio = data.get("audio_name", "").strip()
    if not target_audio:
        return jsonify({"success": False, "error": "Missing audio_name"}), 400

    # 检查目标文件是否已被分配给其他标注员（防止 Back/Forward 抢占他人正在处理的文件）
    cleanup_stale_assignments()
    assignments = load_assignments()
    for name, entry in assignments.items():
        if name != username and entry.get("audio") == target_audio:
            return jsonify({"success": False, "error": "File is currently assigned to another annotator"}), 409

    # 释放当前 assignment
    release_assignment(username)

    # 保留目标文件原有状态（annotated/skipped 不变）：
    # 导航本身不应改变标注状态，只有用户显式提交（Mark Done / Skip）才更新。

    # 重新分配给当前用户
    assignments = load_assignments()
    _, flat = scan_audio_structure()
    rel_path = ""
    for rp in flat:
        if _rel_path_to_key(rp) == target_audio:
            rel_path = rp
            break
    assignments[username] = {
        "audio": target_audio,
        "rel_path": rel_path,
        "assigned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_activity": time.time(),
    }
    save_assignments(assignments)

    return jsonify({"success": True})


@app.route("/api/assignment/abandon", methods=["POST"])
@login_required
def api_abandon_assignment():
    """释放当前分配但不改变文件状态（用于 Next 目标被他人领取时直接换新文件）。"""
    username = session.get("user", "")
    release_assignment(username)
    return jsonify({"success": True})


# ============================================================
# Annotation API (all protected)
# ============================================================

@app.route("/api/files")
@login_required
def api_list_files():
    folders, _ = scan_audio_structure()
    total = sum(len(fd["files"]) for fd in folders)
    annotated = sum(1 for fd in folders for f in fd["files"] if f["status"] == "annotated")
    skipped = sum(1 for fd in folders for f in fd["files"] if f["status"] == "skipped")

    # 添加文件夹统计
    for fd in folders:
        fd["total"] = len(fd["files"])
        fd["annotated"] = sum(1 for f in fd["files"] if f["status"] == "annotated")
        fd["skipped"] = sum(1 for f in fd["files"] if f["status"] == "skipped")
        fd["pending"] = fd["total"] - fd["annotated"] - fd["skipped"]

    return jsonify({
        "folders": folders,
        "total": total,
        "annotated_count": annotated,
        "skipped_count": skipped,
        "pending_count": total - annotated - skipped,
    })


@app.route("/api/segments/<path:audio_name>")
@login_required
def api_get_segments(audio_name):
    """返回某个音频的 segment 数据（含波形缓存）"""
    data = load_segments(audio_name)
    if not data:
        resp = jsonify({"error": "not found", "segments": [], "status": "pending"})
        resp.status_code = 404
    else:
        resp = jsonify(data)
    # 禁止缓存（确保保存后立即看到最新数据）
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/save", methods=["POST"])
@login_required
def api_save():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400

    audio_name = data.get("audio_name", "").strip()
    if not audio_name:
        rel_path = data.get("rel_path", data.get("filename", "")).strip()
        audio_name = Path(rel_path).stem if rel_path else ""

    if not audio_name:
        return jsonify({"success": False, "error": "Missing audio name"}), 400

    # 加载已有数据
    seg_data = load_segments(audio_name) or {
        "audio": f"{audio_name}.wav",
        "folder": "",
        "duration": 0,
        "status": "pending",
        "skip_reasons": [],
        "segments": [],
    }

    # 更新状态
    new_status = data.get("status", "")
    if new_status in ("annotated", "skipped", "pending"):
        seg_data["status"] = new_status

    if new_status == "skipped":
        seg_data["skip_reasons"] = data.get("skip_reasons", [])
    elif new_status in ("annotated", "pending"):
        seg_data["skip_reasons"] = []

    # 更新 segment 文本和边界时间（只更新提供的字段）
    if "segments" in data and data["segments"]:
        seg_map = {s["id"]: s for s in seg_data.get("segments", [])}
        for seg in data["segments"]:
            sid = seg.get("id")
            if sid in seg_map:
                if "text" in seg and seg["text"].strip(): seg_map[sid]["text"] = seg["text"]
                if "start" in seg: seg_map[sid]["start"] = seg["start"]
                if "end" in seg: seg_map[sid]["end"] = seg["end"]
                if "duration" in seg: seg_map[sid]["duration"] = seg["duration"]
                if "exclude_from_training" in seg: seg_map[sid]["exclude_from_training"] = bool(seg["exclude_from_training"])

    seg_data["last_modified"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    seg_data["last_modified_by"] = session.get("user", "unknown")
    if new_status == "annotated":
        seg_data["annotated_by"] = session.get("user", "unknown")
    if new_status == "skipped":
        seg_data["skipped_by"] = session.get("user", "unknown")

    try:
        backup_segments(audio_name, seg_data)
        save_segments(audio_name, seg_data)
    except Exception as e:
        logger.exception("SAVE FAILED audio=%s user=%s: %s", audio_name, session.get("user", "unknown"), e)
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True, "timestamp": seg_data["last_modified"], "annotator": seg_data["last_modified_by"]})


@app.route("/api/stats")
@login_required
def api_stats():
    folders, flat = scan_audio_structure()
    total = len(flat)
    annotations = load_annotations()
    annotated = sum(1 for v in annotations.values()
                    if isinstance(v, dict) and v.get("status") == "annotated")
    skipped = sum(1 for v in annotations.values()
                  if isinstance(v, dict) and v.get("status") == "skipped")
    return jsonify({
        "total": total, "annotated": annotated, "skipped": skipped,
        "pending": total - annotated - skipped,
        "percent_complete": round((annotated + skipped) / total * 100, 1) if total else 0,
    })


@app.route("/api/audio/<path:filename>")
@login_required
def serve_audio(filename):
    ap = Path(get_audio_dir())
    requested = Path(filename)
    safe_parts = [p for p in requested.parts if p != ".."]
    safe_path = ap.joinpath(*safe_parts)
    if not safe_path.exists() or not safe_path.is_file():
        return jsonify({"error": "not found"}), 404
    try:
        safe_path.resolve().relative_to(ap.resolve())
    except ValueError:
        return jsonify({"error": "access denied"}), 403

    ext = safe_path.suffix.lower()
    ct = MIME_TYPES.get(ext, "application/octet-stream")
    fs = safe_path.stat().st_size

    rh = request.headers.get("Range")
    match = re.match(r"bytes=(\d+)-(\d*)", rh) if rh else None

    if match:
        start = int(match.group(1))
        end = min(int(match.group(2) or fs - 1), fs - 1)
        cl = end - start + 1

        def gen():
            with open(safe_path, "rb") as f:
                f.seek(start)
                rem = cl
                while rem > 0:
                    c = f.read(min(65536, rem))
                    if not c: break
                    yield c; rem -= len(c)

        resp = make_response(gen())
        resp.status_code = 206
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{fs}"
        resp.headers["Content-Length"] = cl
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Type"] = ct
    else:
        resp = make_response(send_file(str(safe_path), mimetype=ct, conditional=True))
        resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ============================================================
# 启动
# ============================================================

def print_startup_info(audio_dir, port):
    folders, flat = scan_audio_structure()
    annotations = load_annotations()
    annotated = sum(1 for v in annotations.values()
                    if isinstance(v, dict) and v.get("status") == "annotated")
    skipped = sum(1 for v in annotations.values()
                  if isinstance(v, dict) and v.get("status") == "skipped")
    print(f"\n{'='*56}\n  🎵  Egyptian Arabic Annotation Tool v3\n{'='*56}")
    print(f"  Audio dir: {audio_dir}")
    print(f"  Folders: {len(folders)} | Files: {len(flat)} | Annotated: {annotated} | Skipped: {skipped}")
    print(f"  URL: http://localhost:{port}\n{'='*56}\n")


def main():
    config = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-dir", "-d", default=config.get("audio_dir", "./audio"))
    parser.add_argument("--port", "-p", type=int, default=config.get("port", 8080))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    audio_dir = Path(args.audio_dir)
    if not audio_dir.is_absolute():
        audio_dir = SCRIPT_DIR / audio_dir
    audio_dir = str(audio_dir.resolve())
    if not Path(audio_dir).exists():
        Path(audio_dir).mkdir(parents=True, exist_ok=True)

    app.config["AUDIO_DIR"] = audio_dir
    app.config["BACKUP_DIR"] = str(Path(config.get("backup_dir", "./annotations_copy")).resolve())
    print_startup_info(audio_dir, args.port)
    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True)


# ============================================================
# 模块级初始化（Gunicorn 导入时自动执行）
# ============================================================
_init_config = load_config()

# Secret key for Flask sessions — use config value or auto-generate and persist
_secret = _init_config.get("secret_key", "")
if not _secret:
    _secret = secrets.token_hex(32)
    _init_config["secret_key"] = _secret
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as _f:
            json.dump(_init_config, _f, ensure_ascii=False, indent=2)
    except OSError:
        pass  # read-only filesystem; session works but key won't persist

app.secret_key = _secret
app.config["SESSION_TIMEOUT"] = SESSION_TIMEOUT_MINUTES * 60

app.config.setdefault("AUDIO_DIR", str(Path(_init_config.get("audio_dir", "./audio")).resolve()))
app.config.setdefault("ANNOTATIONS_DIR", str(Path(_init_config.get("annotations_dir", "./annotations")).resolve()))
app.config.setdefault("BACKUP_DIR", str(Path(_init_config.get("backup_dir", "./annotations_copy")).resolve()))


if __name__ == "__main__":
    main()
