import io

from PIL import Image

from ytmatrix import motion


def jpeg(fill, *, size=(120, 90), blob=None):
    image = Image.new("L", size, fill)
    if blob:
        x, y, w, h, shade = blob
        image.paste(Image.new("L", (w, h), shade), (x, y))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def test_identical_frames_score_near_zero():
    frame = jpeg(120)
    assert motion.score_frames([frame, frame, frame]) < 0.5


def test_identical_frames_are_classified_static():
    frame = jpeg(120)
    assert motion.is_static(motion.score_frames([frame] * 3)) is True


def test_a_moving_subject_scores_high():
    frames = [
        jpeg(40, blob=(10, 10, 30, 30, 220)),
        jpeg(40, blob=(60, 20, 30, 30, 220)),
        jpeg(40, blob=(90, 45, 30, 30, 220)),
    ]
    assert motion.score_frames(frames) > motion.DEFAULT_STATIC_THRESHOLD


def test_a_moving_subject_is_not_classified_static():
    frames = [
        jpeg(40, blob=(0, 0, 60, 60, 240)),
        jpeg(40, blob=(60, 30, 60, 60, 240)),
    ]
    assert motion.is_static(motion.score_frames(frames)) is False


def test_a_single_frame_cannot_be_judged_and_counts_as_moving():
    # Refusing to guess: dropping a legitimate video because its thumbnails
    # 404'd is worse than showing one still.
    assert motion.score_frames([jpeg(100)]) == motion.UNKNOWN_SCORE
    assert motion.is_static(motion.UNKNOWN_SCORE) is False


def test_no_frames_at_all_counts_as_moving():
    assert motion.score_frames([]) == motion.UNKNOWN_SCORE


def test_unreadable_frames_are_skipped():
    assert motion.score_frames([b"junk", b"more junk"]) == motion.UNKNOWN_SCORE


def test_one_readable_frame_among_junk_still_cannot_be_judged():
    assert motion.score_frames([jpeg(100), b"junk"]) == motion.UNKNOWN_SCORE


# --- selection -------------------------------------------------------------

MOVING = 30.0
STILL = 1.0


def test_rank_fills_the_grid_with_moving_videos_only():
    candidates = [("a", STILL), ("b", MOVING), ("c", MOVING), ("d", STILL), ("e", MOVING)]
    result = motion.rank(candidates, needed=2, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert result["slots"] == ["b", "c"]
    assert result["relaxed"] == 0


def test_rank_keeps_search_order_among_moving_videos():
    candidates = [("b", MOVING), ("a", MOVING), ("c", MOVING)]
    result = motion.rank(candidates, needed=3, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert result["slots"] == ["b", "a", "c"], "relevance order must survive filtering"


def test_rank_pushes_stills_to_the_back_of_the_reserves():
    candidates = [("still", STILL), ("m1", MOVING), ("m2", MOVING)]
    result = motion.rank(candidates, needed=1, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert result["slots"] == ["m1"]
    assert result["reserves"] == ["m2", "still"]


def test_rank_relaxes_rather_than_leaving_cells_empty():
    # A query that returns almost nothing but album art should still fill the
    # wall. A still beats a black hole.
    candidates = [("m", MOVING), ("s1", 2.0), ("s2", 0.5), ("s3", 1.5)]
    result = motion.rank(candidates, needed=3, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert len(result["slots"]) == 3
    assert result["slots"][0] == "m"
    assert result["relaxed"] == 2


def test_rank_uses_the_liveliest_stills_when_it_must_relax():
    candidates = [("dullest", 0.1), ("liveliest", 3.0), ("middle", 1.0)]
    result = motion.rank(candidates, needed=2, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert result["slots"] == ["liveliest", "middle"]


def test_rank_reports_how_many_cells_had_to_relax():
    candidates = [("s1", 1.0), ("s2", 1.0)]
    result = motion.rank(candidates, needed=2, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert result["relaxed"] == 2


def test_rank_never_puts_the_same_video_in_two_places():
    candidates = [("a", MOVING), ("b", STILL)]
    result = motion.rank(candidates, needed=2, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert set(result["slots"]).isdisjoint(result["reserves"])
    assert len(result["slots"]) + len(result["reserves"]) == 2


def test_rank_handles_an_empty_candidate_list():
    result = motion.rank([], needed=8, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert result == {"slots": [], "reserves": [], "relaxed": 0}


def test_unknown_scores_are_treated_as_moving_and_get_used():
    candidates = [("unmeasurable", motion.UNKNOWN_SCORE), ("still", STILL)]
    result = motion.rank(candidates, needed=1, threshold=motion.DEFAULT_STATIC_THRESHOLD)
    assert result["slots"] == ["unmeasurable"]
