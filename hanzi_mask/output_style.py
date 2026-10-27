"""Render canonical white-foreground masks in the selected output colors."""

import numpy as np


OUTPUT_STYLES = {
    "black-on-white": "白底黑字",
    "white-on-black": "黑底白字",
}
DEFAULT_OUTPUT_STYLE = "black-on-white"


def validate_output_style(output_style: str) -> None:
    if output_style not in OUTPUT_STYLES:
        raise ValueError("输出底色必须为 black-on-white（白底黑字）或 white-on-black（黑底白字）")


def render_mask(mask: np.ndarray, output_style: str = DEFAULT_OUTPUT_STYLE) -> np.ndarray:
    """Change presentation without modifying the mask's foreground semantics."""
    validate_output_style(output_style)
    return 255 - mask if output_style == "black-on-white" else mask
