from ytmatrix import wallstate


def test_a_fresh_directory_has_no_query(tmp_path):
    assert wallstate.load(tmp_path) == {"query": None, "history": []}


def test_round_trips(tmp_path):
    wallstate.save(tmp_path, {"query": "banjo covers", "history": ["a", "banjo covers"]})
    assert wallstate.load(tmp_path) == {
        "query": "banjo covers",
        "history": ["a", "banjo covers"],
    }


def test_survives_being_reloaded_from_scratch(tmp_path):
    wallstate.save(tmp_path, {"query": "kept", "history": ["kept"]})
    # A second load is what a server restart does.
    assert wallstate.load(tmp_path)["query"] == "kept"


def test_history_is_capped_so_it_cannot_grow_forever(tmp_path):
    wallstate.save(tmp_path, {"query": "x", "history": [f"q{i}" for i in range(500)]})
    history = wallstate.load(tmp_path)["history"]
    assert len(history) == wallstate.MAX_HISTORY
    assert history[-1] == "q499", "the most recent must be the ones kept"


def test_a_corrupt_state_file_does_not_stop_the_wall_starting(tmp_path):
    wallstate.save(tmp_path, {"query": "x", "history": []})
    (tmp_path / wallstate.STATE_NAME).write_text("{not json")
    assert wallstate.load(tmp_path) == {"query": None, "history": []}


def test_an_empty_query_reads_back_as_none(tmp_path):
    wallstate.save(tmp_path, {"query": "", "history": []})
    assert wallstate.load(tmp_path)["query"] is None


def test_save_creates_the_directory(tmp_path):
    target = tmp_path / "nested"
    wallstate.save(target, {"query": "q", "history": []})
    assert wallstate.load(target)["query"] == "q"


def test_saving_leaves_no_temp_file_behind(tmp_path):
    wallstate.save(tmp_path, {"query": "q", "history": []})
    assert list(tmp_path.glob("*.tmp")) == []
