# -*- coding: utf-8 -*-
"""ComfyUI 工作流解析与转换。

- 扫描 ComfyUI 保存的工作流（UI 格式 JSON: nodes/links 图结构）
- 提取可调参数（所有 widget 输入，按类型映射成表单字段）
- 转换为 API 格式（{node_id: {class_type, inputs}}）提交给 /prompt

UI 格式节点要点（ComfyUI 0.30 保存格式）:
  node["inputs"]: [{name, type, link(上游link_id或None), widget?: {name}}]
  node["widgets_values"]: 与带 widget 标记的 input 按顺序一一对应
  node["mode"]: 0/1=活跃（0.30 实测两种值都会执行，如 ResolutionSelector 常为 1），
                4=muted/disabled（前端灰色禁用）；API 提交格式不含 mode，ComfyUI 执行所有提交节点
  link: [link_id, origin_node_id, origin_slot, target_node_id, target_slot, type]

ComfyUI 0.30 兼容点（升级/排障时先查这里）:
  1. node.mode 语义与旧版不同（0 和 1 都活跃）→ _node_active 只排除 mode=4
  2. COMBO 参数两种定义格式: ["COMBO", [opts]] 旧式 / ["COMBO", {"options": [...], ...}] 新式
     → _combo_options 两者都解析
  3. widget 全部进 node.inputs（带 widget.name），widgets_values 与之顺序对齐；
     seed 后的 control_after_generate 占位值（"fixed"/"randomize" 等）需跳过 → _skip_seed_control_widget
  4. 新式节点（io.ComfyNode）可能 inputs 为空/不全，剩余 widgets_values 按 object_info
     required+optional 顺序补齐 → build_api_prompt 尾部兼容逻辑
  5. 暴露白名单：节点标题以 "C2A" 开头的节点才暴露参数；无任何 C2A 标题节点则全部
     暴露（fallback）；有 C2A 节点时未标记节点补充暴露 text/prompt 输入（保证能输提示词）
"""
import os
import json
import re
import logging

log = logging.getLogger("workflow_parser")

# 被跳过的节点类型（纯 UI 辅助）
_SKIP_TYPES = {"MarkdownNote", "Note", "Reroute"}

# 提示词承载：节点标题以 "C2A1" 开头的节点，其输入由底部提示词输入框
# (input-wrap) 承载；不再自动匹配第一个 text 节点（多 CLIP 场景容易选错）


def _is_prompt_carrier_node(node) -> bool:
    return (node.get("title") or "").strip().startswith("C2A1")


def _prompt_key(workflow: dict):
    """标题前缀 C2A1 的节点的提示词承载输入 key（底部 input-wrap 写入目标）。
    取该节点第一个未链接的 widget 输入——兼容 PrimitiveStringMultiline 的 value、
    CLIPTextEncode 的 text 等任意命名；找不到返回 None。"""
    for node in workflow.get("nodes") or []:
        if not _is_prompt_carrier_node(node):
            continue
        for inp in node.get("inputs") or []:
            if inp.get("widget") is not None and inp.get("link") is None:
                return f"{node['id']}:{inp['name']}"
    return None


# 常用输入名的中文标签（友好显示）
def _node_title(node) -> str:
    t = (node.get("title") or "").strip()
    if t:
        return t
    ntype = node.get("type") or ""
    short = ntype.rsplit("/", 1)[-1]
    return short or ntype


def _friendly_label(name: str) -> str:
    """参数显示名：直接用节点参数原名，不做中文翻译（想更友好可在 ComfyUI 里给节点改名，
    分组标题会显示用户自定义的节点名）。"""
    return name


# ==================== 工作流扫描 ====================

def list_workflows(workflow_dirs: list[str]) -> list[dict]:
    """扫描多个目录下的 .json 工作流，返回 [{name, path, mtime}]。"""
    result = []
    seen = set()
    for d in workflow_dirs:
        if not d or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".json"):
                continue
            path = os.path.join(d, fn)
            if os.path.realpath(path) in seen:
                continue
            seen.add(os.path.realpath(path))
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0
            result.append({
                "name": os.path.splitext(fn)[0],
                "path": path,
                "mtime": mtime,
            })
    # 按修改时间倒序（最近用的在前）
    result.sort(key=lambda x: x["mtime"], reverse=True)
    return result


def load_workflow(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ==================== 参数提取 ====================

def _combo_options(node_type: str, input_name: str, object_info: dict | None) -> list | None:
    """从 object_info 取 COMBO 输入的可选项。
    兼容两种定义格式：
      - 旧格式: ["COMBO", [opt1, opt2, ...]]
      - 新格式: ["COMBO", {"options": [...], "default": ..., "tooltip": ...}] (io.ComfyNode)
    """
    if not object_info or node_type not in object_info:
        return None
    info = object_info[node_type]
    inp = info.get("input", {})
    for section in ("required", "optional"):
        params = inp.get(section, {})
        if input_name in params:
            spec = params[input_name]
            if isinstance(spec, list) and spec:
                first = spec[0]
                if isinstance(first, list):
                    return first                       # 旧格式
                if len(spec) > 1 and isinstance(spec[1], dict):
                    opts = spec[1].get("options")
                    if isinstance(opts, list):
                        return opts                    # 新格式
    return None


def extract_params(workflow: dict, object_info: dict | None = None,
                   pin_filter: bool = True) -> list[dict]:
    """从工作流提取可调参数表单（同时支持 UI 格式与 API 格式）。

    pin_filter=True 时应用 C2A 标题白名单（只提取标题以 C2A 开头节点的参数）；False 返回全部。
    """
    if is_api_format(workflow):
        return _extract_params_api(workflow, object_info or {})
    return _extract_params_ui(workflow, object_info, pin_filter)


def _api_input_type(node_type: str, name: str, object_info: dict) -> str:
    """从 object_info 推断 API 格式输入的类型。"""
    info = object_info.get(node_type, {})
    inp = info.get("input", {})
    for section in ("required", "optional"):
        params = inp.get(section, {})
        if name in params:
            spec = params[name]
            if isinstance(spec, list):
                if isinstance(spec[0], list):
                    return "COMBO"
                meta = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
                return meta.get("type", "")
    return ""


def _infer_type_from_value(value):
    """无 object_info 时从默认值推断参数类型。"""
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "INT"
    if isinstance(value, float):
        return "FLOAT"
    if isinstance(value, str):
        return "STRING"
    return ""


def _extract_params_api(api: dict, object_info: dict) -> list[dict]:
    params = []
    for nid, node in api.items():
        ntype = node.get("class_type", "")
        node_label = (object_info.get(ntype, {}).get("display_name") or ntype) if object_info else ntype
        for name, value in (node.get("inputs", {}) or {}).items():
            if isinstance(value, list):  # 连接引用，跳过
                continue
            ptype = _api_input_type(ntype, name, object_info)
            if not ptype:
                ptype = _infer_type_from_value(value)
            pkind = _map_param_type(ntype, name, ptype, value)
            if pkind is None:
                continue
            param = {
                "key": f"{nid}:{name}",
                "node_id": int(nid) if str(nid).lstrip("-").isdigit() else nid,
                "node_label": node_label,
                "name": name,
                "label": _friendly_label(name),
                "type": pkind,
                "default": value,
            }
            if pkind == "combo":
                param["options"] = _combo_options(ntype, name, object_info) or []
            params.append(param)
    return params


def _extract_params_ui(workflow: dict, object_info: dict | None = None,
                       pin_filter: bool = True) -> list[dict]:
    params = []
    links = workflow.get("links") or []
    link_ids = {l[0] for l in links}
    nodes = workflow.get("nodes") or []

    # C2A 标题白名单：节点标题以 "C2A" 开头的节点才暴露参数（用户指定）。
    # 未标记节点一律不暴露，仅保留 text/prompt 文本输入（保证至少能输入提示词）。
    # pin_filter=False（展开全部参数）时不应用白名单
    exposed_ids = ({n["id"] for n in nodes
                    if (n.get("title") or "").strip().startswith("C2A")}
                   if pin_filter else set())

    for node in nodes:
        # C2A1 前缀节点整体不进面板：它的 text/prompt 由底部 input-wrap 承载
        # （展开全部时也不显示，避免与底部输入框重复）
        if _is_prompt_carrier_node(node):
            continue
        # 展开全部（pin_filter=False）时忽略白名单，所有节点视为已暴露
        is_exposed = (not pin_filter) or bool(exposed_ids and node["id"] in exposed_ids)
        # 被标记的节点豁免 mode 检查：用户主动加 C2A 前缀的节点必然想暴露参数
        if _node_active(node) is False and not is_exposed:
            continue
        if not is_exposed:
            # 未标记节点仅补充文本输入（保证至少能输入提示词），其余不暴露
            if not any(inp.get("widget") is not None
                       and inp.get("name") in ("text", "prompt", "positive_prompt", "negative_prompt")
                       for inp in (node.get("inputs") or [])):
                continue
        ntype = node.get("type") or ""
        if ntype in _SKIP_TYPES:
            continue
        node_label = _node_title(node)
        widgets = node.get("widgets_values") or []
        wv_idx = 0
        for inp in node.get("inputs") or []:
            name = inp.get("name", "")
            is_widget = inp.get("widget") is not None
            if inp.get("link") is not None and int(inp["link"]) in link_ids:
                # 已连接的输入（含被连接的 widget）：不可改，但占位推进
                if is_widget:
                    wv_idx += 1
                continue
            if is_widget:
                if _skip_seed_control_widget(widgets, wv_idx, inp.get("type", "")):
                    wv_idx += 1   # seed 后的 control_after_generate 占位值
                if _is_ui_only_widget(ntype, name):
                    wv_idx += 1   # 前端专用 widget：占位推进，不提取
                    continue
                default = widgets[wv_idx] if wv_idx < len(widgets) else None
                ptype = inp.get("type", "")
                pkind = _map_param_type(ntype, name, ptype, default)
                wv_idx += 1
                if pkind is None:
                    continue
                param = {
                    "key": f"{node['id']}:{name}",
                    "node_id": node["id"],
                    "node_label": node_label,
                    "name": name,
                    "label": _friendly_label(name),
                    "type": pkind,
                    "default": default,
                }
                if pkind == "combo":
                    param["options"] = _combo_options(ntype, name, object_info) or []
                params.append(param)
    return params


# ComfyUI 前端在 seed 类 widget 之后固定追加 control_after_generate（'fixed'/'randomize'…），
# 它只占 widgets_values 一个位置、不是 API 输入（KSampler/RandomNoise 等都会带）。
_SEED_CONTROL_VALUES = {"fixed", "randomize", "increment", "decrement"}


def _skip_seed_control_widget(widgets: list, wv_idx: int, ptype: str) -> bool:
    """widgets_values 中 control_after_generate 的占位值（seed 后的 'fixed' 等控制字符串）。
    当前输入是数字类型而值却是控制字符串时 → 跳过占位，不消费。"""
    return (wv_idx < len(widgets) and isinstance(widgets[wv_idx], str)
            and widgets[wv_idx] in _SEED_CONTROL_VALUES
            and ptype in ("INT", "FLOAT"))


def _is_ui_only_widget(node_type: str, name: str) -> bool:
    """前端专用输入名（不会出现在 API inputs 里），占位推进但不提取/不提交。"""
    return name == "control_after_generate"


def _map_param_type(node_type: str, name: str, ptype: str, default) -> str | None:
    """参数类型映射；None 表示不展示（保持工作流默认）。"""
    if ptype == "IMAGE":
        # 通用图片输入 → 上传参数（生成节点的 first_frame 等）
        return "image"
    if node_type in ("LoadImage", "LoadImageMask") and name == "image":
        # LoadImage 的 image 是 COMBO（从 input 目录选图），本质是选文件 → 上传按钮
        return "image"
    if ptype == "STRING":
        return "text"
    if ptype == "INT":
        return "int"
    if ptype == "FLOAT":
        return "float"
    if ptype == "COMBO":
        return "combo"
    if ptype == "BOOLEAN":
        return "bool"
    if ptype == "IMAGEUPLOAD" or name == "upload":
        return None
    return None


# ==================== UI -> API 转换 ====================

def _node_active(node) -> bool:
    """mode 语义（ComfyUI 0.30 实测）：
      0/1 = 活跃（0.30 保存的正常执行节点可能为 0 或 1，如 ResolutionSelector 常为 1 且正常出图）
      4 = muted/disabled（前端灰色禁用，不参与执行）
      API 提交格式不含 mode，ComfyUI 会执行提交的所有节点；mode 仅用于 UI 参数过滤与连接校验。
    """
    return node.get("mode", 0) in (0, 1)


def check_workflow_health(workflow: dict, object_info: dict | None = None) -> list[dict]:
    """加载工作流时的健康检查，提前提示可能导致"意外错误"的问题。

    返回 [{level: "info"|"warn"|"error", msg: str}, ...]
    检查项：
      - 无任何可编辑文本输入（提示词节点未 pin 或不存在）→ warn
      - 活跃节点引用了被禁用（muted/disabled）节点的输出 → error（提交必报错）
      - 无 seed 参数 → info（随机种子不可控）
    """
    issues = []
    nodes = workflow.get("nodes") or []
    active = [n for n in nodes if _node_active(n) and (n.get("type") or "") not in _SKIP_TYPES]

    # 1) 文本输入
    has_text = any(
        any(inp.get("widget") is not None and inp.get("name") in ("text", "prompt", "positive_prompt")
            for inp in (n.get("inputs") or []))
        for n in active
    )
    if not has_text:
        issues.append({"level": "warn",
                       "msg": "This workflow has no editable text input (prompt node not pinned or missing); you may not be able to enter a prompt"})

    # 2) 被禁用节点的连接
    inactive_ids = {n["id"] for n in nodes if not _node_active(n)}
    links_map = {}
    for lk in workflow.get("links") or []:
        if len(lk) >= 3:
            links_map[lk[0]] = lk
    for n in active:
        for inp in n.get("inputs") or []:
            lid = inp.get("link")
            if lid is None:
                continue
            lk = links_map.get(lid)
            if lk and lk[1] in inactive_ids:
                issues.append({"level": "error",
                               "msg": f"「{n.get('type')}」引用了被禁用节点 {lk[1]} 的输出，提交时可能报错"})

    # 3) seed 参数
    has_seed = any(
        any(inp.get("widget") is not None and inp.get("name") in ("seed", "noise_seed")
            for inp in (n.get("inputs") or []))
        for n in active
    )
    if not has_seed:
        issues.append({"level": "info", "msg": "This workflow has no seed param (random seed cannot be pinned)"})

    return issues


def is_api_format(workflow: dict) -> bool:
    """判断是否已经是 API 格式（{node_id: {class_type, inputs}}）。"""
    if not isinstance(workflow, dict) or "nodes" in workflow:
        return False
    return all(isinstance(v, dict) and "class_type" in v for v in workflow.values())


def build_api_prompt(workflow: dict, overrides: dict | None = None,
                     object_info: dict | None = None) -> dict:
    """工作流 → API 格式 {node_id_str: {class_type, inputs}}。

    支持两种输入：
      - UI 格式（有 nodes/links 图结构）→ 转换
      - API 格式 → 原样返回（overrides 仅能覆盖已存在的直接输入）
    overrides: {"node_id:input_name": value} 覆盖对应 widget 值。
    object_info: ComfyUI /object_info，用于新式节点（io.ComfyNode）的
                 widgets_values 补齐（widgets 直存、不在 inputs 里的参数）。
    """
    if is_api_format(workflow):
        return _apply_overrides_api(workflow, overrides or {})
    return _build_from_ui(workflow, overrides or {}, object_info)


def _apply_overrides_api(api: dict, overrides: dict) -> dict:
    """API 格式直接提交，overrides 按 "node_id:input_name" 覆盖。"""
    out = {}
    for nid, node in api.items():
        node = dict(node)
        inputs = dict(node.get("inputs", {}))
        for k, v in overrides.items():
            if ":" in k:
                nn, inp = k.split(":", 1)
                if nn == nid and inp in inputs:
                    inputs[inp] = v
        node["inputs"] = inputs
        out[nid] = node
    return out


class WorkflowError(RuntimeError):
    """User-readable error when a workflow cannot be converted/executed."""


def _build_from_ui(workflow: dict, overrides: dict, object_info: dict | None = None) -> dict:
    nodes = workflow.get("nodes") or []
    links = workflow.get("links") or []
    links_map = {l[0]: l for l in links}
    overrides = overrides or {}

    # 1. 收集 Reroute 追踪表：reroute 节点 id -> 其输入上游 link
    reroute_upstream = {}
    for node in nodes:
        if node.get("type") == "Reroute" and node.get("inputs"):
            link_id = node["inputs"][0].get("link")
            if link_id is not None:
                reroute_upstream[node["id"]] = link_id

    # 2. 被跳过（bypass/muted/disabled）的节点 id 集合
    inactive_ids = {node["id"] for node in nodes if not _node_active(node)}

    def resolve_link(link_id):
        """追踪 link（若指向 Reroute 则继续向上），返回 (origin_node_id, origin_slot)。
        若最终指向被禁用的节点，抛 WorkflowError。"""
        seen = set()
        while link_id is not None and link_id not in seen:
            seen.add(link_id)
            link = links_map.get(link_id)
            if not link:
                return None
            origin_node, origin_slot = link[1], link[2]
            if origin_node in reroute_upstream:
                link_id = reroute_upstream[origin_node]
                continue
            if origin_node in inactive_ids:
                raise WorkflowError(
                    "This workflow contains links from disabled nodes (bypass/muted/disabled) and cannot be executed."
                    "Enable or remove those nodes in ComfyUI, then save the workflow again."
                )
            return origin_node, origin_slot
        return None

    api = {}
    for node in nodes:
        if not _node_active(node):
            continue
        ntype = node.get("type") or ""
        if ntype in _SKIP_TYPES:
            continue
        inputs = {}
        widgets = node.get("widgets_values") or []
        wv_idx = 0
        for inp in node.get("inputs") or []:
            name = inp.get("name", "")
            if inp.get("type") == "IMAGEUPLOAD" or name == "upload":
                # 纯 UI 辅助输入（上传按钮），不提交给 ComfyUI
                if inp.get("widget") is not None:
                    wv_idx += 1
                continue
            link_id = inp.get("link")
            if link_id is not None:
                resolved = resolve_link(link_id)
                if resolved is None:
                    continue
                inputs[name] = [str(resolved[0]), resolved[1]]
                # 被连接的 widget 输入仍在 widgets_values 占位
                if inp.get("widget") is not None:
                    wv_idx += 1
                continue
            if inp.get("widget") is not None:
                if _skip_seed_control_widget(widgets, wv_idx, inp.get("type", "")):
                    wv_idx += 1   # seed 后的 control_after_generate 占位值
                if _is_ui_only_widget(ntype, name):
                    wv_idx += 1   # 前端专用 widget：占位推进，不提交
                    continue
                if wv_idx < len(widgets):
                    value = widgets[wv_idx]
                else:
                    value = None
                key = f"{node['id']}:{name}"
                if key in overrides and overrides[key] not in ("", None):
                    # 空字符串/None 不覆盖（用工作流默认），避免空 seed 等提交报错
                    value = overrides[key]
                inputs[name] = value
                wv_idx += 1
        # 新式节点（io.ComfyNode）兼容：widgets_values 直存、inputs 可能为空或
        # 未覆盖全部 widget（如 ComfyMathExpression.expression）。按 object_info 的
        # required+optional 顺序，把剩余 widgets_values 映射到未提交的非对象参数。
        if object_info and ntype in object_info:
            oi = object_info[ntype].get("input", {})
            schema = list(oi.get("required", {}).items()) + \
                     list(oi.get("optional", {}).items())
            _OBJ_TYPES = {"MODEL", "CLIP", "VAE", "CONDITIONING", "LATENT", "IMAGE",
                          "MASK", "NOISE", "GUIDER", "SAMPLER", "SIGMAS", "VIDEO",
                          "AUDIO", "CONTROL_NET", "STYLE_MODEL", "CLIP_VISION"}
            submitted = set(inputs.keys())
            for k, v in schema:
                if k in submitted:
                    continue
                typ = v[0] if isinstance(v, list) else \
                    (v.get("type") if isinstance(v, dict) else None)
                if isinstance(typ, list):
                    typ = "COMBO"   # combo 输入：v[0] 是选项列表
                if typ is None or typ in _OBJ_TYPES:
                    continue
                if wv_idx < len(widgets):
                    inputs[k] = widgets[wv_idx]
                    wv_idx += 1
                else:
                    break
        api[str(node["id"])] = {"class_type": ntype, "inputs": inputs}
    return api


# ==================== 图片上传辅助 ====================

def save_uploaded_image(file_storage, comfy_input_dir: str) -> str:
    """把用户上传的图片保存到 ComfyUI input 目录，返回文件名（用于 LoadImage 的 image 值）。
    GIF/多帧动图会解码全部帧导致内存爆炸 → 自动取第一帧转 PNG 静态图保存。"""
    from werkzeug.utils import secure_filename
    orig = file_storage.filename or "upload.png"
    base = secure_filename(orig)
    if not base:
        base = "upload.png"
    name, ext = os.path.splitext(base)
    ext = (ext or ".png").lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        ext = ".png"
    # 动图（GIF / 多帧 WebP）：取第一帧转 PNG，避免解码全部帧爆内存
    if ext in (".gif", ".webp"):
        try:
            from PIL import Image
            file_storage.seek(0)
            with Image.open(file_storage) as im:
                if getattr(im, "n_frames", 1) > 1:
                    im.seek(0)
                    frame = im.convert("RGB").copy()   # copy 强制独立单帧
                    fname = f"{name}_{int(__import__('time').time() * 1000)}.png"
                    frame.save(os.path.join(comfy_input_dir, fname), "PNG")
                    return fname
        except Exception:
            pass
        file_storage.seek(0)   # 单帧或转换失败：回到文件头，按原样保存
    filename = f"{name}_{int(__import__('time').time() * 1000)}{ext}"
    file_storage.save(os.path.join(comfy_input_dir, filename))
    return filename
