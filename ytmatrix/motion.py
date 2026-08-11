"""Tell a real video from a still image with a soundtrack.

Cover-song searches are full of uploads that are one static album-art frame
for four minutes. They are legitimate results and useless on a video wall.

The Data API cannot express this -- `search.list` has no motion filter, and
none of videoDefinition/videoDuration/videoDimension/videoType comes close.
But YouTube samples three frames across every video at /1.jpg, /2.jpg and
/3.jpg (roughly 25/50/75% in). If those are near-identical, nothing moves.

Measured against a real result set, the separation is wide: still-image
uploads score around 1-2, genuinely edited footage lands between 25 and 41.
Costs no quota -- i.ytimg.com is not the Data API.
"""

from __future__ import annotations

import io

from PIL import Image, ImageChops, ImageStat

STORYBOARD_URL = "https://i.ytimg.com/vi/{video_id}/{index}.jpg"
STORYBOARD_INDICES = (1, 2, 3)

# Frames are compared at this size. Small enough that encoding noise and
# subtitle flicker wash out, large enough that a performer moving across the
# shot still registers.
COMPARE_SIZE = (64, 36)

# Mean absolute luma difference between consecutive frames, 0-255. Chosen from
# the observed gap: stills cluster below 2.5, real footage starts around 5.
DEFAULT_STATIC_THRESHOLD = 3.5

# A video we could not measure is treated as moving. Guessing "static" would
# silently drop legitimate results whenever a thumbnail 404s.
UNKNOWN_SCORE = float("inf")


def score_frames(frames: list[bytes]) -> float:
    """Mean difference between consecutive frames. Higher means more motion."""

    def decode(raw: bytes):
        # An unreadable frame is skipped, not fatal: two good frames out of
        # three are plenty, and a 404 must not drop a legitimate video.
        try:
            return Image.open(io.BytesIO(raw)).convert("L").resize(COMPARE_SIZE)
        except Exception:  # noqa: BLE001 - Pillow raises a wide range on bad input
            return None

    images = [image for image in (decode(raw) for raw in frames) if image is not None]

    if len(images) < 2:
        return UNKNOWN_SCORE

    diffs = [
        ImageStat.Stat(ImageChops.difference(images[i], images[i + 1])).mean[0]
        for i in range(len(images) - 1)
    ]
    return sum(diffs) / len(diffs)


def is_static(score: float, threshold: float = DEFAULT_STATIC_THRESHOLD) -> bool:
    return score < threshold


def rank(candidates: list[tuple[str, float]], needed: int, threshold: float) -> dict:
    """Choose which videos go on the wall, preferring ones that move.

    `candidates` is [(video_id, motion_score)] in search-result order.

    Falls back deliberately: if there are not enough moving videos to fill the
    grid, the liveliest of the stills are used rather than leaving cells empty.
    A still image beats a black hole, and a query about, say, ambient album art
    should still produce a wall.
    """
    moving = [vid for vid, score in candidates if not is_static(score, threshold)]
    static = sorted(
        (pair for pair in candidates if is_static(pair[1], threshold)),
        key=lambda pair: pair[1],
        reverse=True,
    )
    static_ids = [vid for vid, _ in static]

    slots = moving[:needed]
    relaxed = 0
    if len(slots) < needed:
        shortfall = needed - len(slots)
        slots += static_ids[:shortfall]
        relaxed = min(shortfall, len(static_ids))

    used = set(slots)
    reserves = [vid for vid in moving if vid not in used]
    reserves += [vid for vid in static_ids if vid not in used]

    return {"slots": slots, "reserves": reserves, "relaxed": relaxed}
