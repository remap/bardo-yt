import io

from PIL import Image

from ytmatrix import letterbox


def frame(content_w, content_h, *, size=(320, 180), fill=200, offset=None):
    """A black 16:9 frame with a lighter rectangle centred inside it."""
    image = Image.new("L", size, 0)
    x = offset[0] if offset else (size[0] - content_w) // 2
    y = offset[1] if offset else (size[1] - content_h) // 2
    image.paste(Image.new("L", (content_w, content_h), fill), (x, y))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_full_frame_reports_no_cropping():
    box = letterbox.detect_content_box(frame(320, 180))
    assert box["w"] > 0.98 and box["h"] > 0.98
    assert letterbox.is_cropped(box) is False


def test_pillarboxing_is_detected_on_a_vertical_video():
    # A 9:16 source inside a 16:9 frame occupies a narrow centre column.
    box = letterbox.detect_content_box(frame(102, 180))
    assert 0.28 < box["w"] < 0.36, box
    assert box["h"] > 0.95, box
    assert letterbox.is_cropped(box) is True


def test_letterboxing_is_detected_on_an_ultrawide_video():
    box = letterbox.detect_content_box(frame(320, 134))  # ~2.39:1
    assert box["w"] > 0.95, box
    assert 0.70 < box["h"] < 0.80, box
    assert letterbox.is_cropped(box) is True


def test_the_box_is_positioned_where_the_content_actually_is():
    box = letterbox.detect_content_box(frame(160, 90, offset=(0, 0)))
    assert box["x"] < 0.02
    assert box["y"] < 0.02
    assert 0.48 < box["w"] < 0.52
    assert 0.48 < box["h"] < 0.52


def test_an_entirely_black_frame_falls_back_to_the_full_frame():
    # A fade-to-black or an unloaded thumbnail must not crop to nothing.
    assert letterbox.detect_content_box(frame(0, 0)) == letterbox.FULL_FRAME


def test_a_tiny_bright_region_is_rejected_rather_than_zoomed_into():
    # A single lit window in a night scene is not a reason to zoom 10x.
    assert letterbox.detect_content_box(frame(12, 8)) == letterbox.FULL_FRAME


def test_an_isolated_bright_speck_does_not_defeat_detection():
    # A logo or compression artefact sitting in the black bar would otherwise
    # drag the bounding box out to the whole frame.
    image = Image.open(io.BytesIO(frame(102, 180))).convert("L")
    image.putpixel((3, 3), 255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    box = letterbox.detect_content_box(buffer.getvalue())
    assert box["w"] < 0.4, f"a single speck defeated the erosion: {box}"


def test_unreadable_bytes_fall_back_to_the_full_frame():
    assert letterbox.detect_content_box(b"not an image") == letterbox.FULL_FRAME


def test_empty_bytes_fall_back_to_the_full_frame():
    assert letterbox.detect_content_box(b"") == letterbox.FULL_FRAME


def test_detection_works_at_maxres_size_too():
    box = letterbox.detect_content_box(frame(408, 720, size=(1280, 720)))
    assert 0.28 < box["w"] < 0.36, box


def test_near_black_bars_still_count_as_bars():
    # Bars are rarely pure 0 after JPEG compression.
    image = Image.new("L", (320, 180), 12)
    image.paste(Image.new("L", (102, 180), 200), (109, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    box = letterbox.detect_content_box(buffer.getvalue())
    assert box["w"] < 0.4, box


def test_is_cropped_tolerates_a_pixel_of_rounding():
    assert letterbox.is_cropped({"x": 0, "y": 0, "w": 0.995, "h": 0.995}) is False
