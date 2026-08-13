# ComfyChat — ComfyUI Workflow Chat UI

A local web chat interface (C2Achat style) for **driving your local ComfyUI**.
Pick any workflow you saved in ComfyUI → fill in the params → send → watch results
(images / GIFs / videos / audio) stream in live.

## ✨ Features

- **Universal workflow support**: handles both ComfyUI save formats
  - UI format (`nodes`/`links` graph, saved from the ComfyUI frontend)
  - API format (`{node_id: {class_type, inputs}}`)
- **Auto param extraction**: parses the workflow's adjustable inputs into a form
  - prompt (text), width/height, seed/steps/CFG (KSampler), sampler/scheduler
    dropdowns, reference-image upload (LoadImage), etc.
  - works offline too — param types are inferred from defaults
- **Realtime execution feedback** (WebSocket → SSE)
  - live sampling progress %
  - per-node status (e.g. "Executing: KSampler")
  - **images appear the moment each SaveImage finishes**, no need to wait for the whole batch
- **Multi-type outputs**: image / GIF / video / audio, card layout, click to open full-size
- **Conversation history**: multi-session management (new/switch/delete/clear),
  messages and results persisted
- **Param panel**: grouped by node, collapsible; empty seed = random
- **UX details**: drag-and-drop reference images (with thumbnails), dark mode,
  Enter to send, 10s ComfyUI status polling, friendly errors (disabled-node detection)

## 🚀 Quick Start

1. **Download** this repo, then put the `comfy_chat` folder **inside your ComfyUI
   installation directory** (sibling of `main.py`):

   ```
   ComfyUI/
   ├── main.py              ← ComfyUI main program
   ├── user/
   └── comfy_chat/          ← put this folder here
       ├── ComfyChat.bat
       ├── StartAll.bat
       └── ...
   ```

2. **Start ComfyUI** first (default port 8188)
3. **Start ComfyChat**: double-click `ComfyChat.bat`
4. Open **http://127.0.0.1:5001**
5. Pick a workflow on the left (auto-listed from ComfyUI's `user/default/workflows/`
   and `comfy_chat/workflows/`) → fill params → type a prompt → send

Or use `StartAll.bat` to launch **ComfyUI + ComfyChat together**.

> The launchers are portable: they work from any drive/folder, auto-detect Python
> (3.10–3.12), and auto-install missing dependencies (flask/requests/Pillow) from
> the Aliyun mirror.

## 🛠 Native Skills (optional, off by default)

ComfyChat can also run in-process PyTorch skills that bypass ComfyUI entirely
(e.g. `skills/rmbg/` — BiRefNet background removal). To enable:

```
set ENABLE_SKILLS=1
```

The skill model (`models/com2matte.safetensors`) is not included in this repo —
download it separately (e.g. HuggingFace / ModelScope) and place it in `models/`.

## 📄 License

MIT
