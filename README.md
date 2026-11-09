# Inkwell IO

ComfyUI nodes for driving a graph from your own program through the ComfyUI API.

I wrote these for [Inkwell](https://store.steampowered.com/app/1914280/), a game that generates
its scenes and characters with ComfyUI, but nothing in them is specific to Inkwell. Your app
looks the nodes up by `class_type` (and extras by their `key` widget) instead of by node title,
so renaming a node in ComfyUI doesn't break anything. The graph's wiring also tells the app
which inputs it actually uses.

The nodes are in the **Inkwell** category of the node menu.

![A text-to-image graph with Inkwell Inputs on the left and Inkwell Output on the right](docs/images/hero_graph.webp)

A basic text-to-image graph. The app writes the prompt, negative, seed and size into Inkwell
Inputs, and picks up the file Inkwell Output saves. The nodes in between are standard ComfyUI.

The pack has:

- one node for the common inputs (prompt, negative, seed, frame size and a few more), plus
  single-value nodes for anything else, each found by a `key`;
- an image node with ten optional slots. An empty slot outputs a 64×64 placeholder and
  `present = false`, so the prompt still runs where an empty `LoadImage` would fail;
- an output node that can remove the background, trim to the subject and save PNG or lossless
  WebP, keeping the workflow metadata;
- a loader that opens PNG, WebP or JPEG from a path (`InkwellLoadImageFromPath`).

## Install

From ComfyUI-Manager or the Comfy Registry, search for "Inkwell IO".

By hand: clone this repo into `ComfyUI/custom_nodes/`, run `pip install -r requirements.txt`
with ComfyUI's Python, and restart ComfyUI.

If you use the Inkwell app, don't install it here as well. Inkwell installs and updates its own
copy in `custom_nodes/inkwell_io`, and two copies of the pack register the same node classes.

The one dependency is [transparent-background](https://github.com/plemeri/transparent-background)
(MIT), and it's only imported when `remove_bg` is on. It downloads its InSPyReNet weights the
first time it runs. Those weights aren't part of this pack and come with their own licence.

## Using it from an app

A graph with an `InkwellInputs` node in it is what I call a contract graph. To run one from
code:

1. Find the nodes by class: `InkwellInputs` (one), `InkwellImages` (none or one),
   `InkwellOutput` (one or more), and any extras (`InkwellText`, `InkwellToggle`,
   `InkwellNumber`, `InkwellImage`), told apart by their `key` widget.
2. Treat a field of `InkwellInputs` or `InkwellImages` as supported only if its output is linked
   to something. If a graph doesn't use the seed, don't offer a seed. The number of image slots
   is the highest one that's wired.
3. Set the widget values and queue the graph with `POST /prompt`. Each `InkwellOutput` reports
   its files in `ui.images`, in the same shape as `SaveImage`.
4. Read `contract_version` on `InkwellInputs` and skip graphs newer than your app supports. It's
   `1` today.

A contract graph also follows these rules:

- one `InkwellInputs` with an `id`, at most one `InkwellImages`, at least one `InkwellOutput`;
- width and height are either both wired or both unwired;
- wired image slots start at `image` with no gaps, so no `image_3` without `image_2`;
- each extra has a literal `key` that's unique in the graph. Use one of the shared keys below or
  a graph-specific `x_<name>`, and don't reuse the name of an Inputs or Images field.

Shared extra keys: `positive_outfit`, `description`, `emotions` and `regions` (text);
`same_size_as_input` (toggle); `steps`, `cfg`, `denoise` and `mask_blur` (number);
`openpose` (image).

### Output order doesn't change

Saved graphs connect to outputs by index, so new outputs only ever go at the end and existing
ones are never moved or reused. The order is defined in `inkwell_core.py` (`INPUTS_OUTPUTS`,
`IMAGES_OUTPUTS`). Class names and widget names stay the same between versions too, and new
widgets are added as optional inputs so graphs saved with an older version still load.

## Nodes

### InkwellInputs (one per graph)

<img src="docs/images/inkwell_inputs.webp" width="320" alt="Inkwell Inputs node: outputs prompt, positive_style, negative, seed, width, height, refine_prompt, refine_instructions; widgets for id, roles, the text fields, seed, size and frame fallback">

These widgets describe the graph and have no outputs:

| Widget | Type | Notes |
|---|---|---|
| `contract_version` | INT, default 1 | Must not be newer than the app's version |
| `id` | STRING | The id your app knows this graph by. Required |
| `roles` | STRING | Comma-separated list of what the graph is for, e.g. `location_image, tpose` |
| `description` | STRING, multiline | |

`preset` (`custom`, `21:9`, `16:9`, `3:2`, `4:3`, `1:1`, `3:4`, `2:3`, `9:16`), `max_megapixels`
(default 1.0), `multiple` (default 16) and `max_side` (default 2048) control the frame size and
have no outputs either.

Outputs:

| # | Field | Type |
|---|---|---|
| 0 | `prompt` | STRING |
| 1 | `positive_style` | STRING |
| 2 | `negative` | STRING |
| 3 | `seed` | INT (0 … 2⁶⁴−1, control after generate) |
| 4 | `width` | INT |
| 5 | `height` | INT |
| 6 | `refine_prompt` | BOOLEAN |
| 7 | `refine_instructions` | STRING |

The app normally sets the exact width and height. The node then:

1. works out a size from the ratio and `max_megapixels` when `preset` is a ratio and width or
   height is 0 (useful when you run the graph by hand);
2. scales the size down to `max_side` if the longest side is bigger, keeping the ratio;
3. rounds each side to the nearest `multiple` (halves round up) without going over `max_side`.

With the defaults (16 and 2048), 960×1280 and 1536×2048 come out unchanged. 1768×2360 needs
`multiple` set to 8 and `max_side` to at least 2360, since 1768 isn't a multiple of 16 and 2360
is over 2048; with the defaults it becomes 1536×2048.

### InkwellImages (at most one per graph)

<img src="docs/images/inkwell_images.webp" width="240" alt="Inkwell Images node: an image and a present output for each of ten slots, then mask and mask_present; one path widget per slot">

The `image`, `image_2` … `image_10` and `mask` widgets hold file paths and can be left empty.
A path can be absolute, relative to ComfyUI's input folder, or written as `name.png [input]`.
Only filled-in paths are loaded, and a path that doesn't exist is an error.

| # | Output | Type |
|---|---|---|
| 2(N−1) | slot N image (`image`, `image_2`, …) | IMAGE |
| 2(N−1)+1 | slot N `present` | BOOLEAN |
| 20 | `mask` | MASK |
| 21 | `mask_present` | BOOLEAN |

In full: 0 `image`, 1 `image_present`, 2 `image_2`, 3 `image_2_present`, … 18 `image_10`,
19 `image_10_present`, 20 `mask`, 21 `mask_present`.

An empty slot outputs a 64×64 black image (or an empty 64×64 mask) with `present = False`. Wire
`present` into a switch if the graph should do something different when there's no reference.
The mask comes from the alpha channel if the file has one (1 − alpha, like `LoadImage`), and
from the brightness otherwise (white is masked).

### InkwellOutput (one or more per graph)

<img src="docs/images/inkwell_output.webp" width="300" alt="Inkwell Output node set to remove the background, trim, and save lossless WebP">

| Input | Type | Notes |
|---|---|---|
| `images` | IMAGE | |
| `remove_bg` | BOOLEAN | Removes the background with transparent-background (InSPyReNet). The model is loaded the first time this is on and kept afterwards |
| `trim` | BOOLEAN | Crops to the visible part of the image. Fully opaque or fully transparent images are left alone |
| `filename_prefix` | STRING | Default `Inkwell` |
| `folder` | STRING | Subfolder of ComfyUI's output folder. Absolute paths and `..` are rejected |
| `kind` | STRING | A label for the output (`scene`, `portrait`, …), saved as `inkwell_kind` in the file's metadata |
| `format` | `png` / `webp` (optional) | Default `png`, so graphs saved before this option existed still write PNG |
| `quality` | INT 1–100 (optional) | WebP only. 100 (the default) is lossless, anything lower is lossy |

Images are saved as RGBA, with the colour set to black wherever a pixel is fully transparent.
The node reports its files in `ui.images` like `SaveImage` does:
`{filename, subfolder, type: "output"}`.

PNG files keep the prompt and workflow in PNG text chunks.

WebP files drop the alpha channel when the image is fully opaque, which makes them smaller
without changing any pixels. Anything wider or taller than 16383 px is saved as PNG instead,
since that's WebP's size limit. The metadata goes in EXIF, laid out the same way as ComfyUI's
own WebP nodes: `0x0110` holds `prompt:<json>`, then each `extra_pnginfo` entry (the workflow
and so on) goes in `0x010F` and the tags below it as `<key>:<json>`, followed by
`inkwell_kind:<kind>`. `inkwell_core.read_webp_metadata` reads it back.

In my tests lossless WebP came out 25–40% smaller than PNG. If you might edit an image again
later, keep the lossless original and make lossy copies for display.

Metadata isn't written when ComfyUI runs with `--disable-metadata`.

### InkwellLoadImageFromPath

<img src="docs/images/inkwell_load_image_from_path.webp" width="320" alt="Inkwell Load Image From Path node: one image path widget; outputs image, mask and present">

A plain file loader for graphs that don't use the contract, for example ones that find their
inputs by node title. It has one `image` widget and outputs IMAGE (0), MASK (1) and `present`
(2). The first two match the `LoadImageFromPath` node from Ib_CustomNodes, so you can switch a
graph over by changing the class name. Paths work the same as in the other image nodes
(absolute, relative to the input folder, or `x.png [input]`), and PNG, WebP and JPEG all load.
An empty path gives the placeholder image and `present = false`.

Contract graphs should use `InkwellImages` or `InkwellImage` instead.

### Extras

<img src="docs/images/inkwell_extras.webp" width="600" alt="The four extras: Inkwell Text keyed positive_outfit, Inkwell Toggle keyed same_size_as_input, Inkwell Number keyed steps, Inkwell Image keyed openpose">

Each extra holds a single value. They all have `key`, `label` and `group` widgets; `label` and
`group` are only there to help an app build its form.

| Class | Value widgets | Outputs |
|---|---|---|
| `InkwellText` | `value` (multiline) | 0 STRING |
| `InkwellToggle` | `value` | 0 BOOLEAN |
| `InkwellNumber` | `value`, `min`, `max`, `step` | 0 INT (rounded), 1 FLOAT. Kept between min and max unless they're equal |
| `InkwellImage` | `path` | 0 IMAGE, 1 MASK, 2 `present` BOOLEAN |

## Example

`examples/minimal_t2i_api.json` is a small contract graph in API format: Inputs, then a
checkpoint, CLIP and KSampler with placeholder settings, then Output.

## Code and tests

- `inkwell_core.py` has the logic that doesn't need ComfyUI: output tables, frame sizes, ratio
  presets, alpha cleanup, trimming, image loading, choosing the file format and WebP metadata.
  It doesn't import torch or comfy.
- `nodes.py` has the ComfyUI node classes and `NODE_CLASS_MAPPINGS` /
  `NODE_DISPLAY_NAME_MAPPINGS`.
- `tests/` has the pytest suite. Run `python -m pytest tests`. It only needs numpy, Pillow and
  pytest (no GPU, torch or ComfyUI).

## Licence

MIT. See [LICENSE](LICENSE).
