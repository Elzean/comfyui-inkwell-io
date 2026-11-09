"""ComfyUI nodes of the Inkwell IO pack.

The app finds these by class_type (+ the `key` widget on extras), never by
title. Contract: README.md ("Building an app on it").
Pure logic lives in inkwell_core (tested without torch / comfy).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

import numpy as np
import torch
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths

from . import inkwell_core as core

SEED_MAX = 0xFFFFFFFFFFFFFFFF
MULTILINE = {"multiline": True, "default": ""}


def _input_dir():
    try:
        return folder_paths.get_input_directory()
    except Exception:
        return None


def _image_tensor(rgb: np.ndarray) -> torch.Tensor:
    return torch.from_numpy(np.ascontiguousarray(rgb, dtype=np.float32))[None, ...]


def _mask_tensor(mask: np.ndarray) -> torch.Tensor:
    return torch.from_numpy(np.ascontiguousarray(mask, dtype=np.float32))[None, ...]


def _paths_fingerprint(*paths: str) -> str:
    """Changes when a set path changes or its file is rewritten."""
    h = hashlib.sha256()
    for p in paths:
        h.update((p or "").encode("utf-8"))
        try:
            full = core.resolve_image_path(p, _input_dir())
        except FileNotFoundError:
            full = None
        if full:
            st = os.stat(full)
            h.update(f"{st.st_mtime_ns}:{st.st_size}".encode())
        h.update(b"\0")
    return h.hexdigest()


def _extra_meta():
    return {
        "key": ("STRING", {"default": "", "tooltip": "App key: a reserved EXTRA_KEYS name or x_<name>"}),
        "label": ("STRING", {"default": "", "tooltip": "Label shown in the app"}),
        "group": ("STRING", {"default": "", "tooltip": "Group the app shows this field under"}),
    }


# ---------------------------------------------------------------------------
# InkwellInputs
# ---------------------------------------------------------------------------


class InkwellInputs:
    """Workflow metadata + the common fields. Exactly one per contract graph."""

    CATEGORY = core.CATEGORY
    FUNCTION = "run"
    # Order comes from core.INPUTS_OUTPUTS (append-only).
    RETURN_TYPES = tuple(t for _, t in core.INPUTS_OUTPUTS)
    RETURN_NAMES = tuple(n for n, _ in core.INPUTS_OUTPUTS)
    DESCRIPTION = "Inkwell workflow inputs: id/roles metadata, prompt, seed and frame. Only wired outputs count as supported."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "contract_version": ("INT", {"default": core.CONTRACT_VERSION, "min": 1, "max": 999}),
                "id": ("STRING", {"default": "", "tooltip": "Workflow id the app knows this graph by"}),
                "roles": ("STRING", {"default": "", "tooltip": "Comma list of roles, e.g. scene, portrait"}),
                "description": ("STRING", MULTILINE),
                "prompt": ("STRING", MULTILINE),
                "positive_style": ("STRING", MULTILINE),
                "negative": ("STRING", MULTILINE),
                "seed": ("INT", {"default": 0, "min": 0, "max": SEED_MAX, "control_after_generate": True}),
                "width": ("INT", {"default": 1024, "min": 0, "max": 16384, "step": 1}),
                "height": ("INT", {"default": 1024, "min": 0, "max": 16384, "step": 1}),
                "refine_prompt": ("BOOLEAN", {"default": False}),
                "refine_instructions": ("STRING", MULTILINE),
                "preset": (list(core.PRESET_NAMES), {"default": "custom"}),
                "max_megapixels": ("FLOAT", {"default": core.DEFAULT_MAX_MEGAPIXELS, "min": 0.05, "max": 64.0, "step": 0.05}),
                "multiple": ("INT", {"default": core.DEFAULT_MULTIPLE, "min": 1, "max": 512}),
                "max_side": ("INT", {"default": core.DEFAULT_MAX_SIDE, "min": 64, "max": 16384}),
            }
        }

    def run(
        self,
        contract_version,
        id,
        roles,
        description,
        prompt,
        positive_style,
        negative,
        seed,
        width,
        height,
        refine_prompt,
        refine_instructions,
        preset,
        max_megapixels,
        multiple,
        max_side,
    ):
        w, h = core.resolve_frame(width, height, preset, max_megapixels, multiple, max_side)
        values = {
            "prompt": prompt or "",
            "positive_style": positive_style or "",
            "negative": negative or "",
            "seed": int(seed),
            "width": w,
            "height": h,
            "refine_prompt": bool(refine_prompt),
            "refine_instructions": refine_instructions or "",
        }
        return tuple(values[n] for n, _ in core.INPUTS_OUTPUTS)


# ---------------------------------------------------------------------------
# InkwellImages
# ---------------------------------------------------------------------------


class InkwellImages:
    """Reference image slots image, image_2…image_10 and a mask, by path."""

    CATEGORY = core.CATEGORY
    FUNCTION = "run"
    # Order comes from core.IMAGES_OUTPUTS (append-only).
    RETURN_TYPES = tuple(t for _, t in core.IMAGES_OUTPUTS)
    RETURN_NAMES = tuple(n for n, _ in core.IMAGES_OUTPUTS)
    DESCRIPTION = "Inkwell reference images. Empty slot = 64x64 black image, present=False."

    @classmethod
    def INPUT_TYPES(cls):
        required = {name: ("STRING", {"default": ""}) for name in core.IMAGE_SLOT_NAMES}
        required["mask"] = ("STRING", {"default": ""})
        return {"required": required}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        names = list(core.IMAGE_SLOT_NAMES) + ["mask"]
        return _paths_fingerprint(*(kwargs.get(n, "") for n in names))

    def run(self, mask="", **slots):
        input_dir = _input_dir()
        out = []
        for name in core.IMAGE_SLOT_NAMES:
            rgb, _alpha_mask, present = core.load_image_arrays(slots.get(name, ""), input_dir)
            out.append(_image_tensor(rgb))
            out.append(bool(present))
        m, m_present = core.load_mask_array(mask, input_dir)
        out.append(_mask_tensor(m))
        out.append(bool(m_present))
        return tuple(out)


# ---------------------------------------------------------------------------
# InkwellOutput
# ---------------------------------------------------------------------------

_REMOVER = None
_REMOVER_LOCK = threading.Lock()


def _remover():
    """transparent-background (InSPyReNet, MIT) Remover, loaded on first use and cached.

    Its weights download on first use; nothing is bundled with Inkwell.
    """
    global _REMOVER
    with _REMOVER_LOCK:
        if _REMOVER is None:
            from transparent_background import Remover

            _REMOVER = Remover()
        return _REMOVER


def _remove_bg(rgb_u8: np.ndarray) -> np.ndarray:
    """HxWx3 uint8 → HxWx4 uint8 with the background cut out."""
    out = _remover().process(Image.fromarray(rgb_u8, "RGB"), type="rgba")
    return np.asarray(out.convert("RGBA"), dtype=np.uint8)


class InkwellOutput:
    """Saves RGBA PNG or WebP, with optional background removal and alpha trim."""

    CATEGORY = core.CATEGORY
    FUNCTION = "save"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = "Inkwell output: RGBA PNG or WebP (lossless at quality 100), RGB zeroed under clear alpha; remove_bg cuts out the subject, trim crops to alpha."

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.compress_level = 4

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "remove_bg": ("BOOLEAN", {"default": False}),
                "trim": ("BOOLEAN", {"default": False}),
                "filename_prefix": ("STRING", {"default": "Inkwell"}),
                "folder": ("STRING", {"default": "", "tooltip": "Subfolder of the ComfyUI output directory"}),
                "kind": ("STRING", {"default": "", "tooltip": "What this output is (e.g. scene, portrait); stored in the file metadata"}),
            },
            # Optional so graphs saved before these widgets existed still validate.
            "optional": {
                "format": (list(core.OUTPUT_FORMATS), {"default": "png", "tooltip": "webp: lossless at quality 100, alpha dropped when fully opaque; PNG past 16383 px"}),
                "quality": ("INT", {"default": core.WEBP_LOSSLESS_QUALITY, "min": 1, "max": core.WEBP_LOSSLESS_QUALITY, "tooltip": "WebP only: 100 = lossless, lower = lossy"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    def save(
        self,
        images,
        remove_bg,
        trim,
        filename_prefix,
        folder,
        kind,
        format="png",
        quality=core.WEBP_LOSSLESS_QUALITY,
        prompt=None,
        extra_pnginfo=None,
    ):
        sub = core.clean_subfolder(folder)
        prefix = (filename_prefix or "Inkwell").strip() or "Inkwell"
        if sub:
            prefix = f"{sub}/{prefix}"
        full_dir, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            prefix, self.output_dir, images[0].shape[1], images[0].shape[0]
        )
        results = []
        for batch_number, image in enumerate(images):
            rgba = core.to_rgba(core.float_to_uint8(image.cpu().numpy()))
            if remove_bg:
                rgba = _remove_bg(np.ascontiguousarray(rgba[..., :3]))
            rgba = core.zero_rgb_under_clear_alpha(rgba)
            if trim:
                rgba = core.trim_to_alpha(rgba)

            plan = core.plan_image_save(rgba, format, quality)
            name = filename.replace("%batch_num%", str(batch_number))
            file = f"{name}_{counter:05}_{plan['ext']}"
            out = Image.fromarray(plan["array"], plan["mode"])
            if plan["ext"] == ".webp":
                exif = _webp_metadata(prompt, extra_pnginfo, kind)
                extra = {"exif": exif} if exif is not None else {}
                out.save(os.path.join(full_dir, file), format="WEBP", **plan["params"], **extra)
            else:
                metadata = _png_metadata(prompt, extra_pnginfo, kind)
                out.save(os.path.join(full_dir, file), pnginfo=metadata, compress_level=self.compress_level)
            results.append({"filename": file, "subfolder": subfolder, "type": self.type})
            counter += 1
        return {"ui": {"images": results}}


def _metadata_disabled() -> bool:
    try:
        from comfy.cli_args import args

        return bool(args.disable_metadata)
    except Exception:
        return False


def _webp_metadata(prompt, extra_pnginfo, kind):
    if _metadata_disabled():
        return None
    return core.webp_exif(prompt, extra_pnginfo, kind)


def _png_metadata(prompt, extra_pnginfo, kind):
    if _metadata_disabled():
        return None
    meta = PngInfo()
    if prompt is not None:
        meta.add_text("prompt", json.dumps(prompt))
    if extra_pnginfo is not None:
        for k in extra_pnginfo:
            meta.add_text(k, json.dumps(extra_pnginfo[k]))
    if kind:
        meta.add_text("inkwell_kind", str(kind))
    return meta


# ---------------------------------------------------------------------------
# Extras: one value each, found by their `key` widget
# ---------------------------------------------------------------------------


class InkwellText:
    CATEGORY = core.CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("value",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {**_extra_meta(), "value": ("STRING", MULTILINE)}}

    def run(self, key, label, group, value):
        return (value or "",)


class InkwellToggle:
    CATEGORY = core.CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("BOOLEAN",)
    RETURN_NAMES = ("value",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {**_extra_meta(), "value": ("BOOLEAN", {"default": False})}}

    def run(self, key, label, group, value):
        return (bool(value),)


class InkwellNumber:
    CATEGORY = core.CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("INT", "FLOAT")
    RETURN_NAMES = ("int", "float")

    @classmethod
    def INPUT_TYPES(cls):
        big = 1e9
        return {
            "required": {
                **_extra_meta(),
                "value": ("FLOAT", {"default": 0.0, "min": -big, "max": big, "step": 0.01}),
                "min": ("FLOAT", {"default": 0.0, "min": -big, "max": big, "step": 0.01}),
                "max": ("FLOAT", {"default": 100.0, "min": -big, "max": big, "step": 0.01}),
                "step": ("FLOAT", {"default": 1.0, "min": 0.0, "max": big, "step": 0.01, "tooltip": "Step the app's control uses"}),
            }
        }

    def run(self, key, label, group, value, min, max, step):
        v = float(value)
        if min != max:
            v = core.clamp_number(v, float(min), float(max))
        return (int(round(v)), v)


class InkwellImage:
    CATEGORY = core.CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK", "BOOLEAN")
    RETURN_NAMES = ("image", "mask", "present")

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {**_extra_meta(), "path": ("STRING", {"default": ""})}}

    @classmethod
    def IS_CHANGED(cls, path="", **_):
        return _paths_fingerprint(path)

    def run(self, key, label, group, path):
        rgb, mask, present = core.load_image_arrays(path, _input_dir())
        return (_image_tensor(rgb), _mask_tensor(mask), bool(present))


class InkwellLoadImageFromPath:
    """Drop-in for the third-party `LoadImageFromPath`: same `image` widget, IMAGE at 0, MASK at 1.

    For title-based graphs (`input_image*`), so they need no extra node pack just
    to open a file. Contract graphs use InkwellImages / InkwellImage instead.
    """

    CATEGORY = core.CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK", "BOOLEAN")
    RETURN_NAMES = ("image", "mask", "present")
    DESCRIPTION = "Load an image from a path (absolute, ComfyUI input-relative, or `x.png [input]`). Alpha → mask as in LoadImage; an empty path gives a 64×64 black image and present=false."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("STRING", {"default": ""})}}

    @classmethod
    def IS_CHANGED(cls, image="", **_):
        return _paths_fingerprint(image)

    def run(self, image):
        rgb, mask, present = core.load_image_arrays(image, _input_dir())
        return (_image_tensor(rgb), _mask_tensor(mask), bool(present))


NODE_CLASS_MAPPINGS = {
    "InkwellInputs": InkwellInputs,
    "InkwellImages": InkwellImages,
    "InkwellOutput": InkwellOutput,
    "InkwellText": InkwellText,
    "InkwellToggle": InkwellToggle,
    "InkwellNumber": InkwellNumber,
    "InkwellImage": InkwellImage,
    "InkwellLoadImageFromPath": InkwellLoadImageFromPath,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "InkwellInputs": "Inkwell Inputs",
    "InkwellImages": "Inkwell Images",
    "InkwellOutput": "Inkwell Output",
    "InkwellText": "Inkwell Text",
    "InkwellToggle": "Inkwell Toggle",
    "InkwellNumber": "Inkwell Number",
    "InkwellImage": "Inkwell Image",
    "InkwellLoadImageFromPath": "Inkwell Load Image From Path",
}
