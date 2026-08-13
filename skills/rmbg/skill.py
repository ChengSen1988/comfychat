"""ComfyChat 原生技能：RMBG 抠图（BiRefNet）。

独立 PyTorch 推理，不依赖 ComfyUI。
模型：models/com2matte.safetensors（惰性加载，推理后释放显存）。
输入：PIL Image（RGB）
输出：PIL Image（RGBA，透明背景抠图结果）
"""
import os
import sys
import gc
import threading
from pathlib import Path

SKILL_DIR = Path(__file__).parent          # skills/rmbg/
CHAT_ROOT = SKILL_DIR.parent.parent        # comfy_chat/
MODEL_PATH = CHAT_ROOT / "models" / "com2matte.safetensors"

if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", str(CHAT_ROOT / "cache" / "inductor"))
os.environ.setdefault("TRITON_CACHE_DIR", str(CHAT_ROOT / "cache" / "triton"))

import torch
from torchvision import transforms
from scipy import ndimage
from PIL import Image, ImageFilter
import numpy as np

from birefnet import BiRefNet
from safetensors.torch import load_file
import warnings
warnings.filterwarnings("ignore", message="Not enough SMs to use max_autotune_gemm mode")

_lock = threading.Lock()
_model = None
_device = None
_dtype = None

# 输入尺寸档位：取大于目标的最接近值，避免拉伸变形
_SIZES = [512, 768, 1024, 1280, 1536, 1792, 2048]


def _pick_device():
    if torch.cuda.is_available():
        return "cuda", torch.bfloat16
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps", torch.float16
    return "cpu", torch.float32


def _load_rmbg_dict(state_dict, unwanted_prefixes=("module.", "_orig_mod.")):
    for k in list(state_dict.keys()):
        prefix_length = 0
        for prefix in unwanted_prefixes:
            if k[prefix_length:].startswith(prefix):
                prefix_length += len(prefix)
        state_dict[k[prefix_length:]] = state_dict.pop(k)
    return state_dict


def get_model():
    """惰性加载模型（线程安全）；返回 (model, device)。"""
    global _model, _device, _dtype
    with _lock:
        if _model is not None:
            return _model, _device, _dtype
        if not MODEL_PATH.exists():
            raise FileNotFoundError(f"抠图模型不存在：{MODEL_PATH}")
        _device, _dtype = _pick_device()
        print(f"[rmbg] 加载模型 {MODEL_PATH.name} ({MODEL_PATH.stat().st_size / 2**30:.2f}GB) → {_device}")
        net = BiRefNet(bb_pretrained=False)
        state = load_file(str(MODEL_PATH), device=_device)
        state = _load_rmbg_dict(state)
        net.load_state_dict(state)
        net = net.to(_device)
        torch.set_float32_matmul_precision("high")
        net.eval()
        if _device == "cuda":
            net.half()
        _model = net
        print("[rmbg] 模型就绪")
        return _model, _device, _dtype


def release_model():
    """推理完成后释放显存（下次使用再加载）。"""
    global _model, _device, _dtype
    with _lock:
        if _model is not None:
            del _model
            _model = None
            _device = None
            _dtype = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("[rmbg] 模型已释放")


def _transform(nx, ny):
    return transforms.Compose([
        transforms.Resize((nx, ny)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


def _closest_size(target: int) -> int:
    for s in _SIZES:
        if s > target:
            return s
    return _SIZES[-1]


@torch.no_grad()
def _detail_mask(image, model, device, dtype):
    w, h = image.size
    tf = _transform(_closest_size(w), _closest_size(h))
    tensor = tf(image).unsqueeze(0).to(device)
    if device == "cuda":
        tensor = tensor.half()
    else:
        tensor = tensor.float()
    mask = model(tensor)[-1].sigmoid()
    mask = (mask * 255).clamp(0, 255).to(torch.uint8)[0].squeeze()
    pil = transforms.ToPILImage()(mask.cpu())
    return pil.resize(image.size)


def _merge(image, mask, feather=0, remove_translucent=0, erode=0):
    if image.size != mask.size:
        mask = mask.resize(image.size)
    if erode > 0:
        arr = np.array(mask)
        struct = np.ones((3, 3))
        for _ in range(erode):
            arr = ndimage.binary_erosion(arr, structure=struct).astype(arr.dtype)
        mask = Image.fromarray(arr)
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=feather))
    if remove_translucent > 0:
        mask = mask.point(lambda p: 0 if p < remove_translucent else p)
    img = image.convert("RGBA")
    img_arr = np.array(img)
    img_arr[:, :, 3] = np.array(mask.convert("L"))
    return Image.fromarray(img_arr)


def run(image: Image.Image, feather=0, remove_translucent=0, erode=0) -> Image.Image:
    """对单张图片执行抠图，返回 RGBA 结果图。"""
    model, device, dtype = get_model()
    try:
        rgb = image.convert("RGB")
        mask = _detail_mask(rgb, model, device, dtype)
        return _merge(rgb, mask, feather=feather,
                      remove_translucent=remove_translucent, erode=erode)
    finally:
        release_model()   # 用完即释放，避免与 ComfyUI 抢显存
