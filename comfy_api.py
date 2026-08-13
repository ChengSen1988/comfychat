# -*- coding: utf-8 -*-
"""ComfyUI 对接层：提交工作流、轮询执行状态、解析输出图片。

ComfyUI 0.30 兼容点（升级/排障时先查这里）:
  1. ws 消息格式: {"type": ..., "data": {...}}；事件类型 execution_start/executing/
     progress/progress_state/executed/execution_success/execution_error
  2. progress_state 新事件：nodes: {node_id: {value, max, state}}，需排除已完成的加载类节点
     （UNETLoader/CLIPLoader value=max=1 → 旧算法误判 99%）→ stream_events 内取仍在进行的最大进度
  3. 用户手动中断（/interrupt）：ws 不再推送任何事件（无 success/error），但 /history 立即出现
     status=error + execution_interrupted 消息 → stream_events 超时期间主动查 /history 感知（5s 粒度），
     get_errors 解析 execution_interrupted
  4. 新式 SaveVideo 把 .mp4 记录在 history 的 "images" 字段 → _kind_by_filename 扩展名优先判断
  5. 提交 API 格式不含 node.mode：ComfyUI 执行提交的所有节点（mode 是纯前端状态）
"""
import time
import json
import logging
import requests

log = logging.getLogger("comfy_api")

DEFAULT_COMFY_URL = "http://127.0.0.1:8188"


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, base_url: str = DEFAULT_COMFY_URL, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---------- 基础 ----------
    def _get(self, path: str, params=None):
        try:
            resp = requests.get(self.base_url + path, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise ComfyError(f"无法连接 ComfyUI ({self.base_url}): {e}") from e
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI 返回 HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    def is_online(self) -> bool:
        try:
            self._get("/system_stats")
            return True
        except ComfyError:
            return False

    def get_object_info(self, node_type: str | None = None):
        """获取节点定义（用于 COMBO 选项等）。"""
        path = "/object_info"
        if node_type:
            path += "/" + node_type
        resp = self._get(path)
        return resp.json()

    # ---------- 工作流执行 ----------
    def submit_prompt(self, api_prompt: dict, client_id: str | None = None) -> str:
        """POST /prompt 提交 API 格式工作流，返回 prompt_id。

        client_id: 与 websocket 订阅关联，提交后执行事件会路由给该连接。
        """
        payload = {"prompt": api_prompt}
        if client_id:
            payload["client_id"] = client_id
        try:
            resp = requests.post(self.base_url + "/prompt", json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise ComfyError(f"提交工作流失败: {e}") from e
        if resp.status_code != 200:
            raise ComfyError(f"提交工作流失败 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "prompt_id" not in data:
            raise ComfyError(f"提交工作流异常响应: {data}")
        if data.get("node_errors"):
            # node_errors 非空时通常也不会给 prompt_id，但防御性检查
            raise ComfyError(f"工作流节点错误: {data['node_errors']}")
        return data["prompt_id"]

    def wait_history(self, prompt_id: str, poll_interval: float = 0.8, max_wait: float = 1800.0):
        """轮询 /history/{prompt_id} 直到该 prompt 执行完成（成功或失败）。

        返回 history 条目 dict；超时抛 ComfyError。
        """
        start = time.time()
        while time.time() - start < max_wait:
            resp = self._get(f"/history/{prompt_id}")
            data = resp.json()
            if prompt_id in data:
                return data[prompt_id]
            time.sleep(poll_interval)
        raise ComfyError(f"等待 ComfyUI 执行超时（{max_wait}s）: {prompt_id}")

    # ---------- 结果解析 ----------
    @staticmethod
    def _kind_by_filename(filename: str, fallback: str) -> str:
        """按扩展名判断资源类型。

        ComfyUI 0.30 新式节点（io.ComfyNode）的 SaveVideo 把 .mp4 输出也记录在
        history 的 "images" 字段，按字段名会误判为图片 → 扩展名优先，字段名兜底。
        """
        fn = (filename or "").lower()
        if fn.endswith((".mp4", ".webm", ".mov", ".mkv")):
            return "video"
        if fn.endswith(".gif"):
            return "gif"
        if fn.endswith((".wav", ".mp3", ".flac", ".ogg", ".m4a")):
            return "audio"
        return fallback

    def parse_node_outputs(self, outputs: dict) -> list[dict]:
        """解析节点输出 dict 为统一资源列表。

        兼容两种结构：
          - {node_id: {"images": [...]}}   (history 格式)
          - {"images": [...]}              (websocket executed 单节点格式)
        """
        resources = []
        field_map = {
            "images": "image",
            "gifs": "gif",
            "videos": "video",
            "audio": "audio",
        }
        if any(k in field_map for k in (outputs or {}).keys()):
            node_items = {"0": outputs}
        else:
            node_items = outputs or {}
        for node_id, out in node_items.items():
            for field, kind in field_map.items():
                for item in out.get(field, []) or []:
                    filename = item.get("filename")
                    if not filename:
                        continue
                    subfolder = item.get("subfolder", "")
                    img_type = item.get("type", "output")
                    url = self.base_url + "/view"
                    url += f"?filename={filename}"
                    if subfolder:
                        url += f"&subfolder={subfolder}"
                    url += f"&type={img_type}"
                    resources.append({
                        "kind": self._kind_by_filename(filename, kind),
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": img_type,
                        "url": url,
                    })
        return resources

    def extract_outputs(self, history_entry: dict) -> list[dict]:
        """从 history 条目提取全部输出资源（图片/动画/视频/音频）。

        每项: {"kind", "filename", "subfolder", "type", "url"}
        kind: image | gif | video | audio
        """
        return self.parse_node_outputs(history_entry.get("outputs", {}) or {})

    def extract_images(self, history_entry: dict) -> list[dict]:
        """兼容旧接口：只返回图片。"""
        return [o for o in self.extract_outputs(history_entry) if o["kind"] == "image"]

    def get_errors(self, history_entry: dict) -> list[str]:
        """提取执行错误信息列表（含用户手动中断 execution_interrupted）。"""
        msgs = []
        status = history_entry.get("status", {})
        if status.get("status_str") == "error":
            for m in status.get("messages", []) or []:
                if isinstance(m, list) and m and m[0] == "execution_error":
                    data = m[1] if len(m) > 1 else {}
                    node = data.get("node_type") or data.get("node_id") or "?"
                    err = data.get("exception_message") or data.get("exception_type") or "未知错误"
                    msgs.append(f"[{node}] {err}")
                elif isinstance(m, list) and m and m[0] == "execution_interrupted":
                    # 用户在 ComfyUI 中点击了停止
                    data = m[1] if len(m) > 1 else {}
                    node = data.get("node_type") or data.get("node_id") or "?"
                    msgs.append(f"[{node}] 任务已在 ComfyUI 中被停止（interrupted）")
        return msgs

    # ---------- WebSocket 实时事件流 ----------
    def stream_events(self, prompt_id: str, client_id: str, timeout: float = 30.0):
        """连接 ComfyUI websocket，实时转发执行事件（供 SSE 流式输出）。

        yield dict:
          {"type": "executing", "node": str}           # 开始执行某节点
          {"type": "progress", "value": int, "max": int}  # 进度
          {"type": "executed", "outputs": {...}}       # 某节点输出完成（含实时出图）
          {"type": "success"}
          {"type": "error", "message": str}
        连接失败/超时 → 不 yield 任何事件（调用方降级为轮询 /history）
        """
        try:
            from websockets.sync.client import connect
        except ImportError:
            log.warning("websockets 未安装，无法实时进度")
            return
        uri = f"ws://{self.base_url.split('://')[1]}/ws?clientId={client_id}"
        try:
            with connect(uri, open_timeout=5) as ws:
                idle = 0
                while True:
                    try:
                        raw = ws.recv(timeout=5)
                    except TimeoutError:
                        # 单次接收超时：任务可能仍在执行；同时主动查 /history，
                        # 若用户已在 ComfyUI 停止任务（interrupt），history 会立即出现
                        # status=error，此时 ws 不再推送任何事件 → 必须主动感知，否则前端一直转圈
                        idle += 1
                        try:
                            hres = self._get(f"/history/{prompt_id}")
                            entry = hres.json().get(prompt_id)
                            if entry:
                                st = (entry.get("status") or {}).get("status_str")
                                if st == "error":
                                    yield {"type": "error",
                                           "message": "任务已在 ComfyUI 中被停止（interrupted）"}
                                    return
                                if st == "success":
                                    yield {"type": "success"}
                                    return
                        except Exception:
                            pass
                        if idle > 120:   # 10 分钟无任何进展才放弃
                            log.warning("websocket 长时间无匹配事件，停止监听")
                            break
                        continue
                    except Exception:
                        break
                    idle = 0
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    # ComfyUI 0.30 消息格式: {"type": ..., "data": {...}}
                    mtype = msg.get("type")
                    data = msg.get("data", {}) or {}
                    if data.get("prompt_id") and data["prompt_id"] != prompt_id:
                        continue
                    if mtype == "execution_start":
                        yield {"type": "start"}
                    elif mtype == "executing":
                        yield {"type": "executing", "node": data.get("node") or data.get("display_node")}
                    elif mtype == "progress":
                        yield {"type": "progress",
                               "value": data.get("value", 0), "max": data.get("max", 1)}
                    elif mtype == "progress_state":
                        # 新格式：nodes: {node_id: {value, max, state}}
                        # 排除已完成的节点（value/max>=0.99，如 UNETLoader/CLIPLoader 加载类
                        # value=max=1 → 100% 不应卡住前端显示），只取仍在进行的节点的进度
                        best = None
                        for nid, st in (data.get("nodes") or {}).items():
                            if not st.get("max"):
                                continue
                            pct = st["value"] / st["max"]
                            if pct >= 0.99:   # 已完成节点不参与
                                continue
                            ipct = min(99, int(pct * 100))
                            if best is None or ipct > best:
                                best = (st["value"], st["max"], nid)
                        if best:
                            yield {"type": "progress", "value": best[0], "max": best[1], "node": best[2]}
                    elif mtype == "executed":
                        yield {"type": "executed", "node": data.get("node"),
                               "outputs": data.get("output", {}) or {}}
                    elif mtype == "execution_success":
                        yield {"type": "success"}
                        return
                    elif mtype == "execution_error":
                        errd = data.get("exception_message") or data.get("exception_type") or "未知错误"
                        node = data.get("node_type") or data.get("node_id") or "?"
                        yield {"type": "error", "message": f"[{node}] {errd}"}
                        return
                    # 其余（status / execution_cached 等）忽略
        except Exception as e:
            log.warning(f"websocket 事件流中断: {e}")

    def run(self, api_prompt: dict, poll_interval: float = 0.8, max_wait: float = 1800.0):
        """提交并等待，返回 (outputs, errors)。"""
        prompt_id = self.submit_prompt(api_prompt)
        entry = self.wait_history(prompt_id, poll_interval=poll_interval, max_wait=max_wait)
        outputs = self.extract_outputs(entry)
        errors = self.get_errors(entry)
        return outputs, errors
