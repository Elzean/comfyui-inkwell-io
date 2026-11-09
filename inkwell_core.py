"""Pure logic of the Inkwell IO pack: no torch, no comfy imports.

Everything here works on plain Python numbers, numpy arrays and PIL images so it
can be tested with pytest outside ComfyUI. The node modules wrap it.

Contract: README.md ("Building an app on it").
"""

from __future__ import annotations

import math
import os
from typing import Optional, Sequence, Tuple

import numpy as np

CONTRACT_VERSION = 1
CATEGORY = "Inkwell"

# ---------------------------------------------------------------------------
# Output tables. APPEND-ONLY: saved graphs link to outputs by index, so a new
# field goes at the end and an existing index is never reordered or reused.
# Apps that drive these graphs mirror this table, so it only ever grows.
# ---------------------------------------------------------------------------

#: InkwellInputs outputs, (name, comfy type), index = position.
INPUTS_OUTPUTS: Tuple[Tuple[str, str], ...] = (
    ("prompt", "STRING"),  # 0
    ("positive_style", "STRING"),  # 1
    ("negative", "STRING"),  # 2
    ("seed", "INT"),  # 3
    ("width", "INT"),  # 4
    ("height", "INT"),  # 5
    ("refine_prompt", "BOOLEAN"),  # 6
    ("refine_instructions", "STRING"),  # 7
)

MAX_IMAGE_SLOTS = 10


def image_slot_name(slot: int) -> str:
    """`image` for slot 1, `image_N` after (the legacy title names)."""
    return "image" if slot == 1 else f"image_{slot}"


IMAGE_SLOT_NAMES: Tuple[str, ...] = tuple(image_slot_name(i + 1) for i in range(MAX_IMAGE_SLOTS))


def _images_outputs() -> Tuple[Tuple[str, str], ...]:
    out = []
    for name in IMAGE_SLOT_NAMES:
        out.append((name, "IMAGE"))  # 2(N-1)
        out.append((f"{name}_present", "BOOLEAN"))  # 2(N-1)+1
    out.append(("mask", "MASK"))  # 20
    out.append(("mask_present", "BOOLEAN"))  # 21
    return tuple(out)


#: InkwellImages outputs, (name, comfy type), index = position. APPEND-ONLY.
IMAGES_OUTPUTS: Tuple[Tuple[str, str], ...] = _images_outputs()

# ---------------------------------------------------------------------------
# Frame
# ---------------------------------------------------------------------------

#: Ratio presets of the frame fallback, as (w, h). `custom` = use width/height.
RATIO_PRESETS = {
    "custom": None,
    "21:9": (21, 9),
    "16:9": (16, 9),
    "3:2": (3, 2),
    "4:3": (4, 3),
    "1:1": (1, 1),
    "3:4": (3, 4),
    "2:3": (2, 3),
    "9:16": (9, 16),
}
PRESET_NAMES: Tuple[str, ...] = tuple(RATIO_PRESETS.keys())

DEFAULT_MAX_MEGAPIXELS = 1.0
DEFAULT_MULTIPLE = 16
DEFAULT_MAX_SIDE = 2048
DEFAULT_SIZE = 1024


def size_from_ratio(preset: str, max_megapixels: float, multiple: int = DEFAULT_MULTIPLE) -> Tuple[int, int]:
    """Largest size of `preset`'s ratio whose area fits in `max_megapixels`."""
    ratio = RATIO_PRESETS.get(preset)
    if ratio is None:
        raise ValueError(f"No ratio for preset {preset!r}")
    rw, rh = ratio
    mp = max_megapixels if max_megapixels and max_megapixels > 0 else DEFAULT_MAX_MEGAPIXELS
    area = mp * 1_000_000
    h = math.sqrt(area * rh / rw)
    w = h * rw / rh
    m = max(1, int(multiple or 1))
    # Floor to the multiple so the area stays within the budget.
    return max(m, int(w // m) * m), max(m, int(h // m) * m)


def snap(value: int, multiple: int) -> int:
    """Round to the nearest multiple (at least one multiple)."""
    m = max(1, int(multiple or 1))
    return max(m, int(math.floor(value / m + 0.5)) * m)  # half rounds up, not to even


def resolve_frame(
    width: int,
    height: int,
    preset: str = "custom",
    max_megapixels: float = DEFAULT_MAX_MEGAPIXELS,
    multiple: int = DEFAULT_MULTIPLE,
    max_side: int = DEFAULT_MAX_SIDE,
) -> Tuple[int, int]:
    """Output width × height of InkwellInputs.

    The app picks the exact size; this only snaps to `multiple` and clamps to
    `max_side`. When `preset` is a ratio and width/height are 0, the size comes
    from the ratio and `max_megapixels` (the hand-run fallback).
    """
    w, h = int(width or 0), int(height or 0)
    m = max(1, int(multiple or 1))
    if preset in RATIO_PRESETS and RATIO_PRESETS[preset] is not None and (w <= 0 or h <= 0):
        w, h = size_from_ratio(preset, max_megapixels, m)
    if w <= 0 and h <= 0:
        w = h = DEFAULT_SIZE
    elif w <= 0:
        w = h
    elif h <= 0:
        h = w
    limit = int(max_side or 0)
    if limit > 0 and max(w, h) > limit:
        scale = limit / max(w, h)
        w, h = w * scale, h * scale
    w, h = snap(w, m), snap(h, m)
    if limit > 0:
        # Snapping may round past the limit; step down whole multiples.
        top = max(m, (limit // m) * m)
        w, h = min(w, top), min(h, top)
    return int(w), int(h)


# ---------------------------------------------------------------------------
# Alpha cleanup / trim (HxWx4 float or uint8 arrays)
# ---------------------------------------------------------------------------


def zero_rgb_under_clear_alpha(rgba: np.ndarray) -> np.ndarray:
    """Copy of an HxWx4 array with RGB set to 0 wherever alpha is 0."""
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError(f"Expected HxWx4, got {rgba.shape}")
    out = rgba.copy()
    out[out[..., 3] == 0, :3] = 0
    return out


def alpha_bbox(alpha: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    """(left, top, right, bottom), exclusive right/bottom, of alpha > 0; None if empty."""
    ys, xs = np.nonzero(alpha > 0)
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def trim_to_alpha(rgba: np.ndarray) -> np.ndarray:
    """Crop an HxWx4 array to its alpha bounds. A fully clear image is kept as is."""
    box = alpha_bbox(rgba[..., 3])
    if box is None:
        return rgba
    l, t, r, b = box
    return rgba[t:b, l:r]


def to_rgba(arr: np.ndarray, alpha: Optional[np.ndarray] = None) -> np.ndarray:
    """HxWx{1,3,4} → HxWx4 (same dtype). `alpha` (HxW) replaces/sets the alpha channel."""
    if arr.ndim == 2:
        arr = arr[..., None]
    c = arr.shape[2]
    full = 1.0 if np.issubdtype(arr.dtype, np.floating) else 255
    if c == 1:
        rgb = np.repeat(arr, 3, axis=2)
        a = np.full(arr.shape[:2], full, dtype=arr.dtype)
    elif c == 3:
        rgb = arr
        a = np.full(arr.shape[:2], full, dtype=arr.dtype)
    elif c == 4:
        rgb = arr[..., :3]
        a = arr[..., 3]
    else:
        raise ValueError(f"Unsupported channel count {c}")
    if alpha is not None:
        a = alpha.astype(arr.dtype)
    return np.concatenate([rgb, a[..., None]], axis=2)


def float_to_uint8(arr: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(arr * 255.0), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Image loading (path → array or placeholder)
# ---------------------------------------------------------------------------

PLACEHOLDER_SIZE = 64


def resolve_image_path(path: str, input_dir: Optional[str]) -> Optional[str]:
    """Absolute path of an image widget value; None when empty.

    Accepts absolute paths, Comfy input-relative paths (`sub/x.png`) and the
    annotated form `x.png [input]`. Raises FileNotFoundError when set but missing.
    """
    p = (path or "").strip().strip('"')
    if not p:
        return None
    if p.endswith(" [input]"):
        p = p[: -len(" [input]")]
    candidates = [p] if os.path.isabs(p) else []
    if not os.path.isabs(p):
        if input_dir:
            candidates.append(os.path.join(input_dir, p))
        candidates.append(p)
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    raise FileNotFoundError(f"Inkwell image not found: {path!r}")


def placeholder_image() -> np.ndarray:
    """64×64 black RGB float image."""
    return np.zeros((PLACEHOLDER_SIZE, PLACEHOLDER_SIZE, 3), dtype=np.float32)


def placeholder_mask() -> np.ndarray:
    """64×64 empty float mask."""
    return np.zeros((PLACEHOLDER_SIZE, PLACEHOLDER_SIZE), dtype=np.float32)


def load_image_arrays(path: str, input_dir: Optional[str]) -> Tuple[np.ndarray, np.ndarray, bool]:
    """(rgb HxWx3 float32, mask HxW float32, present) for an image widget.

    The mask follows ComfyUI's LoadImage: 1 where the image is transparent.
    An empty path gives the 64×64 placeholders and present=False.
    """
    full = resolve_image_path(path, input_dir)
    if full is None:
        return placeholder_image(), placeholder_mask(), False
    from PIL import Image, ImageOps

    with Image.open(full) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode == "I":
            im = im.point(lambda v: v * (1 / 255))
        rgb = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
        if "A" in im.getbands():
            alpha = np.asarray(im.getchannel("A"), dtype=np.float32) / 255.0
            mask = 1.0 - alpha
        else:
            mask = np.zeros(rgb.shape[:2], dtype=np.float32)
    return rgb, mask, True


def load_mask_array(path: str, input_dir: Optional[str]) -> Tuple[np.ndarray, bool]:
    """(mask HxW float32, present) for the mask widget.

    An image with alpha uses 1 - alpha (LoadImage convention); otherwise the
    luminance is the mask (white = masked).
    """
    full = resolve_image_path(path, input_dir)
    if full is None:
        return placeholder_mask(), False
    from PIL import Image, ImageOps

    with Image.open(full) as im:
        im = ImageOps.exif_transpose(im)
        if "A" in im.getbands():
            mask = 1.0 - np.asarray(im.getchannel("A"), dtype=np.float32) / 255.0
        else:
            mask = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
    return mask, True


def parse_roles(roles: str) -> list:
    return [r.strip() for r in (roles or "").split(",") if r.strip()]


def clean_subfolder(folder: str) -> str:
    """Relative output subfolder; refuses absolute paths and `..`."""
    f = (folder or "").strip().replace("\\", "/")
    if not f:
        return ""
    if f.startswith("/") or ":" in f:
        raise ValueError(f"folder must be relative to the output directory: {folder!r}")
    parts = [p for p in f.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError(f"folder must not leave the output directory: {folder!r}")
    return "/".join(parts)


def clamp_number(value: float, lo: float, hi: float) -> float:
    if lo > hi:
        lo, hi = hi, lo
    return min(max(value, lo), hi)


# ---------------------------------------------------------------------------
# Saving: PNG (default) or WebP
# ---------------------------------------------------------------------------

#: InkwellOutput `format` choices. PNG stays first: it is the default, so a
#: graph saved before the widget existed keeps writing what it always wrote.
OUTPUT_FORMATS = ("png", "webp")

#: libwebp refuses anything wider or taller than this.
WEBP_MAX_SIDE = 16383

#: quality >= this means lossless WebP. Masters default to lossless because
#: nobody knows ahead of time which pictures get edited again; lossy copies
#: for display are made from them later.
WEBP_LOSSLESS_QUALITY = 100


def plan_image_save(rgba: np.ndarray, fmt: Optional[str], quality: int = WEBP_LOSSLESS_QUALITY) -> dict:
    """How to write one HxWx4 uint8 image: {ext, mode, array, params} for PIL.

    - `png` (or anything unknown): RGBA PNG, as before.
    - `webp`: lossless at quality 100, lossy below. A fully opaque image drops
      its alpha channel. Over WEBP_MAX_SIDE it falls back to PNG.
    """
    f = str(fmt or "").strip().lower()
    h, w = rgba.shape[:2]
    if f != "webp" or max(h, w) > WEBP_MAX_SIDE:
        return {"ext": ".png", "mode": "RGBA", "array": np.ascontiguousarray(rgba), "params": {}}

    q = int(clamp_number(int(quality), 1, WEBP_LOSSLESS_QUALITY))
    opaque = bool(rgba.shape[2] < 4 or rgba[..., 3].min() == 255)
    array = np.ascontiguousarray(rgba[..., :3] if opaque else rgba)
    if q >= WEBP_LOSSLESS_QUALITY:
        # exact: keep RGB under clear alpha as written (already zeroed upstream).
        params = {"lossless": True, "quality": 80, "method": 4, "exact": True}
    else:
        params = {"lossless": False, "quality": q, "method": 4}
    return {"ext": ".webp", "mode": "RGB" if opaque else "RGBA", "array": array, "params": params}


#: EXIF tags holding generation metadata, the layout ComfyUI's own WebP
#: savers use: 0x0110 (Model) = "prompt:<json>", then 0x010F (Make) and
#: downwards = "<key>:<json>" for each extra_pnginfo entry (workflow, …).
_EXIF_PROMPT_TAG = 0x0110
_EXIF_FIRST_EXTRA_TAG = 0x010F


def webp_exif(prompt, extra_pnginfo, kind: str = ""):
    """PIL Exif with the prompt, extra_pnginfo entries and `inkwell_kind`."""
    import json

    from PIL import Image

    exif = Image.Exif()
    if prompt is not None:
        exif[_EXIF_PROMPT_TAG] = "prompt:{}".format(json.dumps(prompt))
    tag = _EXIF_FIRST_EXTRA_TAG
    for k in extra_pnginfo or {}:
        exif[tag] = "{}:{}".format(k, json.dumps(extra_pnginfo[k]))
        tag -= 1
    if kind:
        exif[tag] = "inkwell_kind:{}".format(str(kind))
    return exif


def read_webp_metadata(exif) -> dict:
    """Inverse of webp_exif: {"prompt": …, "workflow": …, "inkwell_kind": "…"}."""
    import json

    out = {}
    for tag in range(_EXIF_PROMPT_TAG, 0x0100, -1):
        raw = exif.get(tag)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str) or ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        if key == "inkwell_kind":
            out[key] = value
            continue
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


__all__: Sequence[str] = [n for n in dir() if not n.startswith("_")]
