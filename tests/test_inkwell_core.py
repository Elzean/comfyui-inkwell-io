import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inkwell_core as core  # noqa: E402


# --- output tables ---------------------------------------------------------


def test_inputs_output_order_is_the_contract():
    assert [n for n, _ in core.INPUTS_OUTPUTS] == [
        "prompt",
        "positive_style",
        "negative",
        "seed",
        "width",
        "height",
        "refine_prompt",
        "refine_instructions",
    ]
    assert [t for _, t in core.INPUTS_OUTPUTS] == ["STRING", "STRING", "STRING", "INT", "INT", "INT", "BOOLEAN", "STRING"]


def test_images_output_indices():
    outs = core.IMAGES_OUTPUTS
    assert len(outs) == 22
    for slot in range(1, 11):
        name = core.image_slot_name(slot)
        assert outs[2 * (slot - 1)] == (name, "IMAGE")
        assert outs[2 * (slot - 1) + 1] == (f"{name}_present", "BOOLEAN")
    assert outs[20] == ("mask", "MASK")
    assert outs[21] == ("mask_present", "BOOLEAN")
    assert core.image_slot_name(1) == "image"
    assert core.image_slot_name(10) == "image_10"


# --- frame -----------------------------------------------------------------


@pytest.mark.parametrize("w,h", [(960, 1280), (1536, 2048), (1280, 960), (1024, 1024), (2048, 1536)])
def test_exact_app_sizes_pass_with_defaults(w, h):
    assert core.resolve_frame(w, h) == (w, h)


def test_exact_size_passes_even_with_a_ratio_preset():
    assert core.resolve_frame(960, 1280, preset="16:9") == (960, 1280)


def test_1768x2360_passes_with_graph_settings_that_allow_it():
    # 1768 is a multiple of 8, not 16, and 2360 is over the default 2048 side.
    assert core.resolve_frame(1768, 2360, multiple=8, max_side=2560) == (1768, 2360)


def test_1768x2360_is_snapped_and_clamped_by_defaults():
    w, h = core.resolve_frame(1768, 2360)
    assert max(w, h) <= 2048
    assert w % 16 == 0 and h % 16 == 0
    assert abs(w / h - 1768 / 2360) < 0.01


def test_snaps_to_multiple():
    assert core.resolve_frame(1000, 1001) == (1008, 1008)
    assert core.resolve_frame(1000, 1001, multiple=8) == (1000, 1000)
    assert core.resolve_frame(1003, 997, multiple=64) == (1024, 1024)


def test_clamps_to_max_side_keeping_ratio():
    assert core.resolve_frame(4096, 2048) == (2048, 1024)
    assert core.resolve_frame(3000, 3000, max_side=1024) == (1024, 1024)


def test_clamp_never_exceeds_after_snap():
    w, h = core.resolve_frame(2047, 100, multiple=64, max_side=2047)
    assert w <= 2047 and w % 64 == 0


@pytest.mark.parametrize("preset", [p for p in core.PRESET_NAMES if p != "custom"])
def test_preset_fallback_derives_size(preset):
    rw, rh = core.RATIO_PRESETS[preset]
    w, h = core.resolve_frame(0, 0, preset=preset, max_megapixels=1.0)
    assert w % 16 == 0 and h % 16 == 0
    assert w * h <= 1_000_000
    assert abs((w / h) - (rw / rh)) < 0.05
    assert w * h > 0.85 * 1_000_000


def test_preset_examples():
    assert core.resolve_frame(0, 0, preset="1:1", max_megapixels=1.0) == (992, 992)
    assert core.resolve_frame(0, 0, preset="16:9", max_megapixels=1.0) == (1328, 736)
    assert core.resolve_frame(0, 0, preset="9:16", max_megapixels=1.0) == (736, 1328)


def test_preset_respects_max_side():
    w, h = core.resolve_frame(0, 0, preset="21:9", max_megapixels=8.0)
    assert max(w, h) == 2048


def test_custom_with_zero_size_defaults():
    assert core.resolve_frame(0, 0) == (1024, 1024)
    assert core.resolve_frame(768, 0) == (768, 768)


def test_size_from_ratio_rejects_custom():
    with pytest.raises(ValueError):
        core.size_from_ratio("custom", 1.0)


# --- alpha / trim ----------------------------------------------------------


def test_zero_rgb_under_clear_alpha():
    a = np.full((2, 2, 4), 200, dtype=np.uint8)
    a[0, 0, 3] = 0
    a[1, 1, 3] = 1
    out = core.zero_rgb_under_clear_alpha(a)
    assert out[0, 0].tolist() == [0, 0, 0, 0]
    assert out[1, 1].tolist() == [200, 200, 200, 1]
    assert a[0, 0, 0] == 200  # input untouched


def test_trim_to_alpha_bbox():
    a = np.zeros((10, 12, 4), dtype=np.float32)
    a[2:5, 3:9, 3] = 1.0
    out = core.trim_to_alpha(a)
    assert out.shape == (3, 6, 4)
    assert core.alpha_bbox(a[..., 3]) == (3, 2, 9, 5)


def test_trim_keeps_fully_clear_image():
    a = np.zeros((4, 4, 4), dtype=np.uint8)
    assert core.trim_to_alpha(a).shape == (4, 4, 4)
    assert core.alpha_bbox(a[..., 3]) is None


def test_to_rgba():
    rgb = np.ones((3, 3, 3), dtype=np.float32) * 0.5
    out = core.to_rgba(rgb)
    assert out.shape == (3, 3, 4) and out[..., 3].min() == 1.0
    alpha = np.zeros((3, 3), dtype=np.float32)
    assert core.to_rgba(rgb, alpha)[..., 3].max() == 0.0


# --- image or placeholder --------------------------------------------------


def test_empty_path_gives_placeholder():
    rgb, mask, present = core.load_image_arrays("", None)
    assert present is False
    assert rgb.shape == (64, 64, 3) and rgb.max() == 0
    assert mask.shape == (64, 64) and mask.max() == 0
    m, mp = core.load_mask_array("  ", None)
    assert mp is False and m.shape == (64, 64)


def test_loads_absolute_and_input_relative(tmp_path):
    from PIL import Image

    sub = tmp_path / "sub"
    sub.mkdir()
    im = Image.new("RGBA", (8, 6), (255, 0, 0, 255))
    im.putpixel((0, 0), (0, 0, 0, 0))
    im.save(sub / "ref.png")

    rgb, mask, present = core.load_image_arrays(str(sub / "ref.png"), None)
    assert present and rgb.shape == (6, 8, 3)
    assert rgb[1, 1].tolist() == [1.0, 0.0, 0.0]
    assert mask[0, 0] == 1.0 and mask[1, 1] == 0.0

    rgb2, _, present2 = core.load_image_arrays("sub/ref.png", str(tmp_path))
    assert present2 and rgb2.shape == (6, 8, 3)
    _, _, present3 = core.load_image_arrays("sub/ref.png [input]", str(tmp_path))
    assert present3


def test_mask_from_luminance(tmp_path):
    from PIL import Image

    Image.new("L", (4, 4), 255).save(tmp_path / "m.png")
    mask, present = core.load_mask_array(str(tmp_path / "m.png"), None)
    assert present and mask.shape == (4, 4) and mask.min() == 1.0


def test_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        core.load_image_arrays("nope.png", str(tmp_path))


# --- saving ----------------------------------------------------------------


def _rgba(h=24, w=32, alpha=255):
    rng = np.random.default_rng(1)
    arr = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
    arr[..., 3] = alpha
    return arr


def _save_and_reload(tmp_path, rgba, fmt, quality=100, name="out"):
    from PIL import Image

    plan = core.plan_image_save(rgba, fmt, quality)
    path = tmp_path / f"{name}{plan['ext']}"
    Image.fromarray(plan["array"], plan["mode"]).save(path, **plan["params"])
    return plan, path, Image.open(path)


def test_png_is_the_default_and_unchanged(tmp_path):
    rgba = _rgba()
    for fmt in ["png", "", None, "PNG", "bogus"]:
        plan = core.plan_image_save(rgba, fmt, 90)
        assert plan["ext"] == ".png" and plan["mode"] == "RGBA"
        assert plan["array"].shape == rgba.shape


def test_webp_lossless_is_pixel_exact_with_alpha(tmp_path):
    rgba = _rgba()
    rgba[:4, :4, 3] = 0
    rgba[:4, :4, :3] = 0
    rgba[5:9, 5:9, 3] = 128
    plan, path, im = _save_and_reload(tmp_path, rgba, "webp")
    assert plan["ext"] == ".webp" and plan["params"]["lossless"] is True
    assert im.format == "WEBP" and "A" in im.getbands()
    assert np.array_equal(np.asarray(im.convert("RGBA")), rgba)


def test_webp_drops_alpha_when_fully_opaque(tmp_path):
    rgba = _rgba(alpha=255)
    plan, _, im = _save_and_reload(tmp_path, rgba, "webp")
    assert plan["mode"] == "RGB" and plan["array"].shape == (24, 32, 3)
    assert np.array_equal(np.asarray(im.convert("RGB")), rgba[..., :3])


def test_webp_quality_below_100_is_lossy(tmp_path):
    plan = core.plan_image_save(_rgba(), "webp", 85)
    assert plan["params"]["lossless"] is False and plan["params"]["quality"] == 85
    assert core.plan_image_save(_rgba(), "webp", 250)["params"]["lossless"] is True
    assert core.plan_image_save(_rgba(), "webp", -5)["params"]["quality"] == 1


def test_webp_over_the_side_limit_falls_back_to_png():
    big = np.zeros((2, core.WEBP_MAX_SIDE + 1, 4), dtype=np.uint8)
    assert core.plan_image_save(big, "webp", 100)["ext"] == ".png"
    ok = np.zeros((2, core.WEBP_MAX_SIDE, 4), dtype=np.uint8)
    assert core.plan_image_save(ok, "webp", 100)["ext"] == ".webp"


def test_webp_input_loads_with_alpha_mask(tmp_path):
    rgba = _rgba(h=6, w=8)
    rgba[0, 0, 3] = 0
    _, path, _ = _save_and_reload(tmp_path, rgba, "webp", name="ref")
    rgb, mask, present = core.load_image_arrays(str(path), None)
    assert present and rgb.shape == (6, 8, 3)
    assert mask[0, 0] == 1.0 and mask[1, 1] == 0.0
    m, _ = core.load_mask_array(str(path), None)
    assert m[0, 0] == 1.0


def test_webp_metadata_keeps_prompt_workflow_and_kind(tmp_path):
    from PIL import Image

    exif = core.webp_exif({"1": {"class_type": "X"}}, {"workflow": {"nodes": []}}, "portrait")
    path = tmp_path / "m.webp"
    Image.new("RGB", (4, 4)).save(path, lossless=True, exif=exif)
    read = core.read_webp_metadata(Image.open(path).getexif())
    assert read["prompt"] == {"1": {"class_type": "X"}}
    assert read["workflow"] == {"nodes": []}
    assert read["inkwell_kind"] == "portrait"
    assert core.read_webp_metadata(Image.open(path).getexif()) == read


# --- misc ------------------------------------------------------------------


def test_clean_subfolder():
    assert core.clean_subfolder("") == ""
    assert core.clean_subfolder("inkwell\\portraits/") == "inkwell/portraits"
    for bad in ["../x", "a/../../b", "C:/x", "/abs"]:
        with pytest.raises(ValueError):
            core.clean_subfolder(bad)


def test_parse_roles():
    assert core.parse_roles(" scene, portrait ,,") == ["scene", "portrait"]


def test_core_imports_no_torch_or_comfy():
    src = open(core.__file__, encoding="utf-8").read()
    assert "import torch" not in src
    assert "folder_paths" not in src and "import comfy" not in src
