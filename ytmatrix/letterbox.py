"""Find the real picture inside a letterboxed or pillarboxed frame.

The problem: YouTube renders every video into a 16:9 player, so a
vertically-shot or ultrawide source arrives with black bars baked into the
frame. Cropping the iframe to fill the cell does not help -- the bars scale
with it.

The iframe is cross-origin, so its pixels are unreadable. The way in is the
thumbnail: `mqdefault.jpg` is the same 16:9 frame the player produces, so
whatever bars appear there will appear on screen too.

Use mqdefault (320x180) or maxresdefault (1280x720), never hqdefault -- that
one is 4:3 with padding of its own baked in, and would report letterboxing on
every video in existence.
"""

from __future__ import annotations

import io

from PIL import Image, ImageFilter

THUMBNAIL_URL = "https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"

# Luma at or below this counts as "black bar". Real footage almost never sits
# this low across a whole row, and JPEG ringing around a hard bar edge lands
# well under it.
BLACK_THRESHOLD = 24

# Refuse to trust a detection that would crop away most of the picture. A
# genuinely dark shot (a night scene, a fade-in) can read as bars; showing it
# uncropped is a far smaller error than zooming 10x into one lit corner.
MIN_CONTENT_FRACTION = 0.30

FULL_FRAME = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


def detect_content_box(image_bytes: bytes) -> dict:
    """Return the picture's bounds within the frame, normalised to 0..1.

    Falls back to the full frame whenever detection is unconvincing.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception:  # noqa: BLE001 - any unreadable image is simply "no crop"
        return dict(FULL_FRAME)

    width, height = image.size
    if not width or not height:
        return dict(FULL_FRAME)

    mask = image.point(lambda p: 255 if p > BLACK_THRESHOLD else 0)
    # Erode by one pixel so a lone bright speck -- a compression artefact, a
    # station logo sitting in the bar -- cannot drag the bounding box out to
    # the full frame.
    mask = mask.filter(ImageFilter.MinFilter(3))

    bbox = mask.getbbox()
    if bbox is None:
        return dict(FULL_FRAME)

    left, top, right, bottom = bbox
    box_w = (right - left) / width
    box_h = (bottom - top) / height

    if box_w < MIN_CONTENT_FRACTION or box_h < MIN_CONTENT_FRACTION:
        return dict(FULL_FRAME)

    return {
        "x": left / width,
        "y": top / height,
        "w": box_w,
        "h": box_h,
    }


def is_cropped(box: dict, tolerance: float = 0.02) -> bool:
    """True when the box differs enough from the full frame to be worth acting on."""
    return box["w"] < 1 - tolerance or box["h"] < 1 - tolerance
