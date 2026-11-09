"""Inkwell IO — first-party ComfyUI nodes for the Inkwell workflow contract.

See README.md.
"""

# Bump on every change to the pack: the app's Settings installer compares this
# against the copy in <ComfyUI>/custom_nodes/inkwell_io to offer an update.
__version__ = "1.1.0"

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "__version__"]
