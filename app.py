# -*- coding: utf-8 -*-
"""ComfyChat — a ComfyUI general-workflow chat Web UI in C2Achat style.

The frontend sends a user-saved ComfyUI workflow path + adjustable params; this
service converts and submits them to local ComfyUI (/prompt), streaming real-time
progress and results (image/video/audio) back over SSE.

v0.3 features:
- WebSocket realtime event stream (progress % + per-node preview images)
- multi-type outputs (image/animation/video/audio)
- conversation history persistence (JSON storage)
"""
import os
import json
import time
import uuid
import random
import traceback
import threading
from urllib.parse import urlsplit

from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file

import comfy_api
import workflow_parser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_INPUT_DIR = os.environ.get("COMFY_INPUT_DIR", r"F:\comfyui\input")
WORKFLOW_DIRS = [
    os.environ.get("COMFY_WORKFLOWS_DIR", r"F:\comfyui\user\default\workflows"),
    os.path.join(BASE_DIR, "workflows"),
]
HISTORY_DIR = os.path.join(BASE_DIR, "history")
PRESETS_PATH = os.path.join(BASE_DIR, "presets.json")
SKILL_RESULT_DIR = os.path.join(BASE_DIR, "output")   # native skill output (no ComfyUI)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024

client = comfy_api.ComfyClient(COMFY_URL)

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_object_info = None
_object_info_lock = threading.Lock()
_CONV_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,64}$")

# ── in-flight task registry (refresh recovery): even if SSE drops, the background
# thread keeps consuming events and saving results ──
# conv_id -> [ {status, message, prompt, started_at, pids, committed, abort, meta}, ... ]
ACTIVE_TASKS = {}
_TASK_LOCK = threading.Lock()


def get_object_info():
    global _object_info
    if _object_info is None:
        with _object_info_lock:
            if _object_info is None:
                try:
                    _object_info = client.get_object_info()
                except Exception:
                    _object_info = {}
    return _object_info or {}




@app.after_request
def _no_cache_static(resp):
    """Disable static asset caching: the frontend iterates often and stale JS/CSS in the
    browser cache causes weird "inconsistent behavior after refresh" bugs (e.g. param
    panel display corruption).
    """
    if request.path.startswith("/static/") or request.path == "/":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

@app.before_request
def _reject_cross_site_requests():
    for header_name in ("Origin", "Referer"):
        value = request.headers.get(header_name)
        if not value:
            continue
        try:
            host = urlsplit(value).hostname or ""
        except Exception:
            host = ""
        if host not in ("127.0.0.1", "localhost", "::1"):
            return jsonify({"error": "Cross-site request rejected"}), 403
    return None


def _allowed_workflow_path(path: str) -> str | None:
    real = os.path.realpath(path)
    allowed = [os.path.realpath(d) for d in WORKFLOW_DIRS if d]
    for d in allowed:
        if real == d or real.startswith(d + os.sep):
            return real
    return None




def _conv_path(conv_id: str) -> str:
    if not _CONV_ID_RE.match(conv_id or ""):
        raise ValueError("Invalid conversation ID")
    return os.path.join(HISTORY_DIR, conv_id + ".json")


def load_conv(conv_id: str) -> dict | None:
    path = _conv_path(conv_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_conv(conv: dict) -> bool:
    """Atomically write conversation history; degrade to False (print log) without
    breaking the generation flow. Same-dir temp file + os.replace avoids half-written
    files, and narrows the window where other processes (editor/antivirus/index
    service transient locks) block the write on Windows.
    """
    conv_id = conv.get("id", "")
    try:
        path = _conv_path(conv_id)
        os.makedirs(HISTORY_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(conv, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[ComfyChat] failed to save conversation history {conv_id}: {e}", flush=True)
        return False


def list_convs() -> list[dict]:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    items = []
    for fn in os.listdir(HISTORY_DIR):
        if not fn.endswith(".json"):
            continue
        conv_id = fn[:-5]
        conv = load_conv(conv_id)
        if not conv:
            continue
        items.append({
            "id": conv_id,
            "title": conv.get("title", "New Chat"),
            "updated": conv.get("updated", 0),
            "count": len(conv.get("messages", [])),
        })
    items.sort(key=lambda x: x["updated"], reverse=True)
    return items


@app.route("/api/conversations", methods=["GET", "POST"])
def api_conversations():
    if request.method == "GET":
        return jsonify(list_convs())

    conv = {
        "id": uuid.uuid4().hex[:16],
        "title": request.get_json(silent=True) or {},
    }
    title = conv["title"].get("title") if isinstance(conv["title"], dict) else None
    conv["title"] = (title or "New Chat")[:60]
    conv["created"] = int(time.time() * 1000)
    conv["updated"] = conv["created"]
    conv["messages"] = []
    save_conv(conv)
    return jsonify({"id": conv["id"], "title": conv["title"]})


@app.route("/api/conversations/<conv_id>", methods=["GET", "PUT", "DELETE"])
def api_conv_detail(conv_id):
    if not _CONV_ID_RE.match(conv_id or ""):
        return jsonify({"error": "Invalid conversation ID"}), 400
    if request.method == "GET":
        conv = load_conv(conv_id)
        if not conv:
            return jsonify({"error": "Conversation not found"}), 404
        return jsonify(conv)
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid data"}), 400
        data["id"] = conv_id
        data["updated"] = int(time.time() * 1000)
        if not data.get("title"):
            data["title"] = "New Chat"
        if not save_conv(data):
            return jsonify({"error": "Failed to save conversation (file locked)"}), 500
        return jsonify({"success": True})
    # DELETE
    try:
        path = _conv_path(conv_id)

        if conv_id in ACTIVE_TASKS:
            with _TASK_LOCK:
                for t in ACTIVE_TASKS.get(conv_id, []):
                    t["abort"] = True
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                time.sleep(0.5)   # wait for transient locks (antivirus/indexer) to release
                os.remove(path)
        return jsonify({"success": True})
    except OSError as e:
        traceback.print_exc()
        return jsonify({"error": f"Delete failed (file may be locked): {e}"}), 500


# if this conversation still has background tasks writing the file, mark them abort

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/comfy_status")
def comfy_status():
    return jsonify({"online": client.is_online(), "url": COMFY_URL})


@app.route("/api/workflows")
def api_workflows():
    flows = workflow_parser.list_workflows(WORKFLOW_DIRS)
    # first so deletion is not blocked by a locked file
    flows = [f for f in flows if f["name"].startswith("C2A")]
    return jsonify([{"name": f["name"], "path": f["path"]} for f in flows])


@app.route("/api/workflow_config")
def api_workflow_config():
    path = request.args.get("path", "")
    real = _allowed_workflow_path(path)
    if not real:
        return jsonify({"error": "Workflow path not allowed"}), 400
    if not os.path.isfile(real):
        return jsonify({"error": "Workflow not found"}), 404
    try:
        workflow = workflow_parser.load_workflow(real)
    except Exception as e:
        return jsonify({"error": f"Failed to parse workflow: {e}"}), 500
    oi = get_object_info()
    params = workflow_parser.extract_params(workflow, oi)
    all_params = workflow_parser.extract_params(workflow, oi, pin_filter=False)
    return jsonify({
        "name": os.path.splitext(os.path.basename(real))[0],
        "path": real,
        "params": params,
        "all_params": all_params,
        "health": workflow_parser.check_workflow_health(workflow, oi),

        "prompt_key": workflow_parser._prompt_key(workflow),
    })


# ==================== pages & API ====================

def load_presets() -> list[dict]:
    if not os.path.isfile(PRESETS_PATH):
        return []
    try:
        with open(PRESETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        # only show workflows whose names start with "C2A"
        if _repair_preset_paths(data):
            save_presets(data)
        return data
    except Exception:
        return []


def _repair_preset_paths(presets: list[dict]) -> bool:
    """Repair preset paths after workflow rename. When the old path file is missing:
    look for a same-basename file in WORKFLOW_DIRS and update the path; if none, remove
    the orphan preset (so it never leaks onto another same-named workflow).
    Returns True when something changed (needs saving)."""
    known = {}
    changed = False
    kept = []
    for p in presets:
        wp = (p.get("workflow_path") or "").replace("\\", "/")
        if not wp:
            changed = True
            continue
        if os.path.isfile(wp):
            kept.append(p)
            continue
        # filename in the new location; drop the orphan preset if no same-name file exists
        base = os.path.basename(wp)
        if base not in known:
            known[base] = None
            for d in WORKFLOW_DIRS:
                cand = os.path.join(d, base)
                if os.path.isfile(cand):
                    known[base] = cand
                    break
        if known[base]:
            p["workflow_path"] = known[base]
            changed = True
            kept.append(p)
            print(f"[ComfyChat] preset migrated: {wp} → {known[base]}", flush=True)
        else:
            changed = True   # orphan preset: drop
            print(f"[ComfyChat] dropped orphan preset: {p.get('name','?')} ({wp})", flush=True)
    if changed and kept != presets:
        presets[:] = kept
    return changed


def save_presets(presets: list[dict]) -> bool:
    """Atomically write the presets file; retry a few times on transient Windows locks."""
    last_err = None
    for attempt in range(4):
        try:
            os.makedirs(BASE_DIR, exist_ok=True)
            tmp = PRESETS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(presets, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PRESETS_PATH)
            return True
        except Exception as e:
            last_err = e
            time.sleep(0.15 * (attempt + 1))
    print(f"[ComfyChat] failed to save presets: {last_err}", flush=True)
    return False


@app.route("/api/presets")
def api_presets():
    path = request.args.get("path", "")
    presets = load_presets()
    if path:
        presets = [p for p in presets if p.get("workflow_path") == path]
    return jsonify(presets)


@app.route("/api/presets", methods=["POST"])
def api_presets_save():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not (data.get("name") or "").strip():
        return jsonify({"error": "Preset name required"}), 400
    name = data["name"].strip()[:60]
    wf_path = data.get("workflow_path", "")
    presets = load_presets()

    existing = next((p for p in presets
                     if p.get("workflow_path") == wf_path and p.get("name") == name), None)
    if existing:
        existing["prompt"] = data.get("prompt", "")
        existing["params"] = data.get("params", {})
        existing["updated"] = int(time.time() * 1000)
        pid = existing["id"]
    else:
        pid = uuid.uuid4().hex[:12]
        presets.insert(0, {
            "id": pid,
            "name": name,
            "workflow_path": wf_path,
            "prompt": data.get("prompt", ""),
            "params": data.get("params", {}),
            "updated": int(time.time() * 1000),
        })
    if not save_presets(presets):
        return jsonify({"error": "Failed to save preset, retry (file may be locked)"}), 500
    return jsonify({"success": True, "id": pid})


@app.route("/api/presets/<preset_id>", methods=["DELETE"])
def api_presets_delete(preset_id):
    presets = load_presets()
    save_presets([p for p in presets if p.get("id") != preset_id])
    return jsonify({"success": True})


@app.route("/api/upload_image", methods=["POST"])
def upload_image():
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"error": "No file received"}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return jsonify({"error": f"Unsupported image type: {ext or 'no extension'}"}), 400
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    try:
        filename = workflow_parser.save_uploaded_image(file, COMFY_INPUT_DIR)
    except Exception as e:
        return jsonify({"error": f"Save failed: {e}"}), 500
    return jsonify({"success": True, "filename": filename})


ENABLE_SKILLS = os.environ.get("ENABLE_SKILLS", "0") == "1"

_SKILLS = {
    "rmbg": {"name": "Cutout", "desc": "BiRefNet background removal (keep subject, transparent bg)"},
}


@app.route("/api/skills")
def api_skills():
    if not ENABLE_SKILLS:
        return jsonify({"skills": {}})
    return jsonify({"skills": _SKILLS})


@app.route("/api/skill/<skill_id>", methods=["POST"])
def api_skill_run(skill_id):
    if not ENABLE_SKILLS:
        return jsonify({"error": "Native skills are disabled"}), 404
    if skill_id not in _SKILLS:
        return jsonify({"error": f"Unknown skill: {skill_id}"}), 404
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "Upload at least one image"}), 400
    conv_id = request.form.get("convId") or ""
    os.makedirs(os.path.join(SKILL_RESULT_DIR, skill_id), exist_ok=True)
    try:
        import importlib
        mod = importlib.import_module(f"skills.{skill_id}.skill")
        results = []
        t0 = time.time()
        for f in files:
            try:
                from PIL import Image
                img = Image.open(f.stream).convert("RGB")
                out = mod.run(img)
            except Exception as e:
                traceback.print_exc()
                return jsonify({"error": f"Processing failed: {e}"}), 500
            fname = f"{skill_id}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.png"
            out.save(os.path.join(SKILL_RESULT_DIR, skill_id, fname))
            results.append({
                "kind": "image", "filename": fname, "type": "skill",
                "url": f"/api/result?file={skill_id}/{fname}",
                "seed": None, "param_summary": f"skill={skill_id}",
            })
        duration = round(time.time() - t0, 1)
        """Atomically write the presets file; retry a few times on transient Windows locks."""
        if conv_id and _CONV_ID_RE.match(conv_id or ""):
            meta = {"workflow_path": f"skill:{skill_id}", "count": len(results),
                    "prompt": f"[{_SKILLS[skill_id]['name']}] {len(files)} images",
                    "prompt_key": "", "params": {}, "param_summary": f"skill={skill_id}"}
            _save_task_results(conv_id, meta, results, duration)
        return jsonify({"success": True, "results": results, "duration": duration})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Skill execution failed: {e}"}), 500


@app.route("/api/result")
def api_result():
    """Serve native-skill output (output/<skill>/<file>.png)."""
    rel = (request.args.get("file") or "").replace("\\", "/")
    if not rel or ".." in rel or rel.startswith("/"):
        return jsonify({"error": "Invalid path"}), 400
    path = os.path.join(SKILL_RESULT_DIR, rel)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(path, max_age=604800)


# same-name preset (same workflow) -> overwrite, i.e. edit
_THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thumbs")
_THUMB_SIZE = 256   # max side 256px


@app.route("/api/thumb")
def api_thumb():
    filename = os.path.basename(request.args.get("filename") or "")
    if not filename:
        return jsonify({"error": "filename required"}), 400
    src = os.path.join(COMFY_INPUT_DIR, filename)
    if not os.path.isfile(src):
        return jsonify({"error": "File not found"}), 404
    try:
        from PIL import Image
        os.makedirs(_THUMB_DIR, exist_ok=True)
        thumb = os.path.join(_THUMB_DIR, os.path.splitext(filename)[0] + ".jpg")
        if not os.path.exists(thumb):
            with Image.open(src) as im:
                im.thumbnail((_THUMB_SIZE, _THUMB_SIZE))
                if im.mode != "RGB":
                    im = im.convert("RGB")
                im.save(thumb, "JPEG", quality=82)
        return send_file(thumb, mimetype="image/jpeg", max_age=604800)   # cached for 7 days
    except Exception as e:
        return jsonify({"error": f"Thumbnail failed: {e}"}), 500


# native skill switch (off by default; set env ENABLE_SKILLS=1 to enable)
_COMFY_BASE_DIRS = {
    "output": r"F:\comfyui\output",
    "input": COMFY_INPUT_DIR,
    "temp": r"F:\comfyui\temp",
}


@app.route("/api/open_folder", methods=["POST"])
def open_folder():
    # skill registry: id -> display info. Implementation in skills/<id>/skill.py
    data = request.get_json(silent=True) or {}
    filename = (data.get("filename") or "").strip()
    img_type = (data.get("type") or "output").strip()
    subfolder = (data.get("subfolder") or "").strip()

    # (must provide run(image)->image or custom logic)
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "Invalid filename"}), 400
    base = _COMFY_BASE_DIRS.get(img_type)
    if not base:
        return jsonify({"error": "Unknown resource type"}), 400

    folder = os.path.realpath(os.path.join(base, subfolder))
    base_real = os.path.realpath(base)
    if not (folder == base_real or folder.startswith(base_real + os.sep)):
        return jsonify({"error": "Path out of bounds"}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": "Directory not found"}), 404
    try:
        if os.name == "nt":
            os.startfile(folder)
        elif sys.platform == "darwin":
            __import__("subprocess").run(["open", folder])
        else:
            __import__("subprocess").run(["xdg-open", folder])
    except Exception as e:
        return jsonify({"error": f"Open failed: {e}"}), 500
    return jsonify({"success": True, "folder": folder})


# write to conversation history (reuses ComfyUI result format, visible after refresh)

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.route("/process", methods=["POST"])
def process_is():
    try:
        wf_path = request.form.get("workflowPath", "").strip()
        real = _allowed_workflow_path(wf_path)
        if not real:
            return jsonify({"error": "Workflow path not allowed"}), 400
        if not os.path.isfile(real):
            return jsonify({"error": "Workflow not found"}), 404

        try:
            count = max(1, min(int(request.form.get("count", "1") or "1"), 20))
        except ValueError:
            count = 1

        raw_overrides = {}
        for key in request.form:
            if key.startswith("param:"):
                raw_overrides[key[len("param:"):]] = request.form[key]

        workflow = workflow_parser.load_workflow(real)
        params_def = workflow_parser.extract_params(workflow, get_object_info())
        type_map = {p["key"]: p["type"] for p in params_def}

        overrides = {}
        for k, v in raw_overrides.items():
            t = type_map.get(k)
            if t == "int":
                try:
                    overrides[k] = int(v)
                except ValueError:
                    continue
            elif t == "float":
                try:
                    overrides[k] = float(v)
                except ValueError:
                    continue
            elif t == "bool":
                overrides[k] = v in ("1", "true", "True", "on")
            else:
                if v == "":
                    continue   # empty param: keep workflow default (avoid submitting empty strings)
                overrides[k] = v

        seed_keys = [p["key"] for p in params_def
                     if p["name"] in ("seed", "noise_seed") and p["type"] in ("int", "text")]


        for _n in workflow.get("nodes") or []:
            for _inp in _n.get("inputs") or []:
                if _inp.get("widget") is not None and _inp.get("name") in ("seed", "noise_seed"):
                    _k = f"{_n['id']}:{_inp['name']}"
                    if _k not in seed_keys:
                        seed_keys.append(_k)
        user_seed = None
        if seed_keys:
            for k in seed_keys:
                v = overrides.get(k)
                if isinstance(v, int):
                    user_seed = v
                    break
                if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                    user_seed = int(v.strip())
                    break

        # base dirs for each ComfyUI resource type (matching ComfyUI defaults)
        conv_id = request.form.get("convId", "").strip()
        conv_snapshot = load_conv(conv_id) if conv_id and _CONV_ID_RE.match(conv_id) else None

        """Open the folder containing a result file (Windows Explorer)."""
        _SAVE_NODE_TYPES = ("SaveImage", "SaveAnimatedWEBP", "SaveVideo", "SaveAudio",
                            "SaveAnimatedPNG", "VHS_VideoCombine")

        def _apply_conv_subfolder(api_prompt: dict):
            if not conv_id:
                return api_prompt
            for node in api_prompt.values():
                if node.get("class_type") not in _SAVE_NODE_TYPES:
                    continue
                inputs = node.get("inputs", {})
                for key in ("filename_prefix",):
                    if key in inputs and isinstance(inputs[key], str):
                        inputs[key] = f"{conv_id}/{inputs[key].lstrip('/')}"
            return api_prompt

        def _param_summary(api_prompt: dict) -> str:
            """List every param actually sent to ComfyUI (non-link inputs, incl. hidden/default/random seed)."""
            parts = []
            for node in api_prompt.values():
                for name, v in (node.get("inputs") or {}).items():
                    if isinstance(v, list):   # link reference is not a param value
                        continue
                    parts.append(f"{name}={v}")
            return ", ".join(parts)

        def generate():
            client_id = uuid.uuid4().hex
            results = []
            # extra: all seed/noise_seed widget inputs (even unpinned) get a random value when
            image_loop_key = request.form.get("image_loop_key", "")
            image_loop_list = []
            try:
                image_loop_list = json.loads(request.form.get("image_loop_list", "[]") or "[]")
            except Exception:
                image_loop_list = []
            if not (image_loop_key and isinstance(image_loop_list, list) and image_loop_list):
                image_loop_list = [None]

            # empty, so "random seed" always works and covers any manually pinned node
            # conversation log (optional): frontend appends messages when it sends convId
            prompt_text = ""
            prompt_key = request.form.get("promptKey") or ""
            if prompt_key:
                prompt_text = raw_overrides.get(prompt_key, "")
            if not prompt_text:
                for k, v in raw_overrides.items():
                    if type_map.get(k) == "text" and any(s in k for s in (":text", ":prompt")):
                        prompt_text = v
                        prompt_key = k
                        break
            exec_info = {
                "workflow_path": real,
                "count": count,
                "prompt": prompt_text,
                "prompt_key": prompt_key,
                "params": {k: v for k, v in raw_overrides.items() if k != prompt_key},
            }
            # outputs of one conversation go to one subfolder: output/<conv_id>/<orig prefix>...
            try:
                user_images = json.loads(request.form.get("user_images") or "[]")
            except Exception:
                user_images = []
            if isinstance(user_images, list) and user_images:
                exec_info["user_images"] = [{
                    "kind": "image",
                    "filename": f,
                    "url": f"{COMFY_URL}/view?filename={f}&type=input",
                    "type": "input",
                } for f in user_images if isinstance(f, str) and f]
            if len(image_loop_list) > 1 or (len(image_loop_list) == 1 and image_loop_list[0] is not None):
                exec_info["image_loop"] = {"key": image_loop_key, "list": image_loop_list}
            seed_seq = []
            if user_seed is not None:
                seed_seq = [user_seed + i for _ in image_loop_list for i in range(count)]
            task = {
                "status": "running", "message": "", "prompt": prompt_text,
                "started_at": int(time.time() * 1000), "pids": [],
                "committed": False, "abort": False,
                "meta": exec_info, "seed_seq": seed_seq,
                "pid_summary": {},   # pid -> param summary of that run (each image has its own seed)
            }
            if conv_id and conv_snapshot is not None:
                with _TASK_LOCK:
                    ACTIVE_TASKS.setdefault(conv_id, []).append(task)
                threading.Thread(target=_task_worker, args=(conv_id, task),
                                 daemon=True, name="task-worker").start()

            try:
                yield _sse({'status': 'start', 'message': f'Executing workflow "{os.path.basename(real)}"'})
                run_t0 = time.time()
                for img_idx, img in enumerate(image_loop_list):
                    for i in range(count):
                        base_over = dict(overrides)
                        if img:
                            base_over[image_loop_key] = img
                        if count > 1 or len(image_loop_list) > 1:
                            tag = f"(fig {img_idx + 1}/{len(image_loop_list)})" if len(image_loop_list) > 1 else ""
                            yield _sse({"status": "progress", "message": f"Image {i + 1}/{count}{tag}…"})
                        cur_over = dict(base_over)
                        cur_seed = user_seed + i if user_seed is not None else random.randint(1, 2 ** 31 - 1)
                        if seed_keys:
                            for k in seed_keys:
                                cur_over[k] = cur_seed
                        try:
                            api_prompt = workflow_parser.build_api_prompt(
                                workflow, cur_over, get_object_info())
                            _apply_conv_subfolder(api_prompt)

                            exec_info["param_summary"] = _param_summary(api_prompt)
                        except workflow_parser.WorkflowError as e:
                            if conv_id and task in _get_task_list(conv_id):
                                task["abort"] = True
                            yield _sse({"error": str(e)})
                            return
                        except Exception as e:
                            if conv_id and task in _get_task_list(conv_id):
                                task["abort"] = True
                            yield _sse({"error": f"Workflow conversion failed: {e}",
                                        "error_detail": traceback.format_exc()})
                            return
                        try:
                            pid = client.submit_prompt(api_prompt, client_id=client_id)
                        except comfy_api.ComfyError as e:
                            if conv_id and task in _get_task_list(conv_id):
                                task["abort"] = True
                            yield _sse({"error": str(e)})
                            return
                        if conv_id:
                            with _TASK_LOCK:
                                task["pids"].append(pid)
                                task["pid_summary"][pid] = exec_info.get("param_summary", "")
                        yield _sse({"status": "status", "message": f"Submitted ({pid[:8]})..."})

                        results = []   # this round: (kind, url, filename)
                        node_names = {}
                        for nid, node in api_prompt.items():
                            node_names[str(nid)] = node.get("class_type", nid)

                        # multi-image loop: each image in image_loop_list runs count times
                        ws_ok = False
                        ws_success = False
                        try:
                            for ev in client.stream_events(pid, client_id):
                                ws_ok = True
                                if ev["type"] == "progress":
                                    denom = max(int(ev.get("max", 1)), 1)
                                    pct = min(99, int(ev.get("value", 0) * 100 / denom))
                                    yield _sse({"status": "progress", "message": f"Running... {pct}%"})
                                elif ev["type"] == "executing" and ev.get("node"):
                                    nm = node_names.get(str(ev["node"]), str(ev["node"]))
                                    yield _sse({"status": "progress", "message": f"Executing: {nm}"})
                                elif ev["type"] == "executed":
                                    for r in client.parse_node_outputs(ev.get("outputs", {})):
                                        r["seed"] = cur_seed   # ── task registration (refresh recovery): background thread consumes & saves
                                        results.append(r)
                                        yield _sse({"resource": r, "seed": cur_seed})
                                elif ev["type"] == "error":
                                    yield _sse({"error": ev["message"]})
                                    return
                                elif ev["type"] == "success":
                                    ws_success = True
                                    break
                        except Exception:
                            ws_ok = False

                        if not ws_success:
                            # independently of the SSE connection
                            try:
                                entry = client.wait_history(pid)
                            except comfy_api.ComfyError as e:
                                yield _sse({"error": str(e)})
                                return
                            errors = client.get_errors(entry)
                            if errors:
                                for err in errors:
                                    yield _sse({"error": err})
                                return
                            for r in client.extract_outputs(entry):
                                if r not in results:
                                    r["seed"] = cur_seed   # prompt takes the frontend-declared prompt_key first (C2A1 key can be any name
                                    r["param_summary"] = exec_info.get("param_summary", "")
                                    results.append(r)
                                    yield _sse({"resource": r, "seed": cur_seed})
                            yield _sse({"status": "progress", "message": "Done"})
                duration = round(time.time() - run_t0, 1)

                yield _sse({"done": True, "message": "All done", "results": results,
                            "duration": duration, "exec": exec_info})
            except Exception as e:
                traceback.print_exc()
                yield _sse({"error": str(e), "error_detail": traceback.format_exc()})
            finally:
                # like 7:value)
                if conv_id:
                    with _TASK_LOCK:
                        task["committed"] = True

        return Response(stream_with_context(generate()), mimetype="text/event-stream")

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# images uploaded for this run (shown under the user bubble, visible after refresh)

def _get_task_list(conv_id: str) -> list:
    with _TASK_LOCK:
        return list(ACTIVE_TASKS.get(conv_id, []))


def _append_msg(conv_id: str, msg_dict: dict):
    """Append a message to the conversation (background thread, e.g. "task stopped")."""
    if not (conv_id and _CONV_ID_RE.match(conv_id)):
        return
    try:
        conv = load_conv(conv_id)
        if conv is None:
            conv = {"id": conv_id, "messages": [], "title": "New Chat"}
        conv.setdefault("messages", []).append(msg_dict)
        conv["updated"] = int(time.time() * 1000)
        save_conv(conv)
    except Exception:
        traceback.print_exc()


def _save_task_results(conv_id: str, meta: dict, results: list, duration: float):
    # record every param actually sent to ComfyUI (incl. hidden/default/random seed)
    # for the bubble
    if not (conv_id and _CONV_ID_RE.match(conv_id)):
        return
    try:
        conv = load_conv(conv_id)
        if conv is None:
            conv = {"id": conv_id, "messages": [], "title": "New Chat", "updated": int(time.time() * 1000)}
        msgs = conv.setdefault("messages", [])
        now = int(time.time() * 1000)
        # node id -> class name map (for progress hints)
        user_msg = {"role": "user", "kind": "text",
                    "content": meta.get("prompt") or "[no prompt]",
                    "ts": now}
        if meta.get("user_images"):
            user_msg["resources"] = meta["user_images"]   # WebSocket realtime event stream
        msgs.append(user_msg)
        msgs.append({
            "role": "assistant", "kind": "resources",
            "resources": [{"kind": r["kind"], "url": r["url"],
                           "filename": r["filename"], "subfolder": r.get("subfolder", ""),
                           "type": r.get("type", "output"), "seed": r.get("seed"),
                           "param_summary": r.get("param_summary", "")} for r in results],
            "duration": round(duration, 1),
            "exec": meta,
            "ts": now,
        })
        if not conv.get("title") or conv["title"] == "New Chat":
            conv["title"] = (meta.get("prompt") or "New Chat")[:40]
        save_conv(conv)
    except Exception:
        traceback.print_exc()


def _task_worker(conv_id: str, task: dict):


    try:

        while True:
            with _TASK_LOCK:
                if task.get("abort"):
                    task["status"] = "error"
                    task["message"] = "Task aborted"
                    return
                if task.get("committed"):
                    break
            time.sleep(0.3)

        with _TASK_LOCK:
            pids = list(task.get("pids", []))

        # commit signal: background thread takes over remaining consumption & saving (even
        results = []
        t0 = time.time()
        for idx, pid in enumerate(pids):
            with _TASK_LOCK:
                if task.get("abort"):
                    task["status"] = "error"
                    task["message"] = "Task aborted"
                    return
            try:
                entry = client.wait_history(pid)
            except comfy_api.ComfyError as e:
                task["status"] = "error"
                task["message"] = str(e)
                return
            errors = client.get_errors(entry)
            if errors:
                # if this SSE disconnects)
                partial = client.extract_outputs(entry)
                if partial:
                    seq = task.get("seed_seq") or []
                    ps = task.get("pid_summary") or {}
                    for idx2, r in enumerate(partial):
                        r["seed"] = seq[idx] if idx < len(seq) else None
                        r["param_summary"] = ps.get(pid, "")
                    _save_task_results(conv_id, task.get("meta") or {}, partial, time.time() - t0)
                _append_msg(conv_id, {
                    "role": "assistant", "kind": "text",
                    "content": "✗ " + "; ".join(errors),
                    "ts": int(time.time() * 1000),
                })
                task["status"] = "error"
                task["message"] = "; ".join(errors)
                return
            for r in client.extract_outputs(entry):
                if r in results:
                    continue
                seq = task.get("seed_seq") or []
                ps = task.get("pid_summary") or {}
                r["seed"] = seq[idx] if idx < len(seq) else None
                r["param_summary"] = ps.get(pid, "")
                results.append(r)

        # ==================== background task: result save & refresh recovery ====================
        duration = time.time() - t0
        _save_task_results(conv_id, task.get("meta") or {}, results, duration)
        task["status"] = "done"
        task["message"] = f"Done, {len(results)} results"
    except Exception as e:
        task["status"] = "error"
        task["message"] = str(e)
    finally:
        with _TASK_LOCK:
            if conv_id in ACTIVE_TASKS:
                try:
                    ACTIVE_TASKS[conv_id].remove(task)
                except ValueError:
                    pass
                if not ACTIVE_TASKS[conv_id]:
                    del ACTIVE_TASKS[conv_id]


@app.route("/api/active_tasks")
def api_active_tasks():

    conv_id = request.args.get("conv_id", "")
    tasks = _get_task_list(conv_id) if conv_id else []
    return jsonify([{
        "status": t.get("status", "running"),
        "message": t.get("message", ""),
        "prompt": t.get("prompt", ""),
        "started_at": t.get("started_at", 0),
    } for t in tasks])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    os.makedirs(HISTORY_DIR, exist_ok=True)

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    print(f"ComfyChat v0.3 starting: http://127.0.0.1:{port}/  (ComfyUI: {COMFY_URL})")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)