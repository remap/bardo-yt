from ytmatrix import origin


def countries(order, lookup):
    return [lookup[v] for v in order]


def test_an_empty_list_stays_empty():
    assert origin.diversify([]) == []


def test_nothing_is_dropped_or_duplicated():
    pairs = [(f"v{i}", ["US", "KR", "GB"][i % 3]) for i in range(20)]
    out = origin.diversify(pairs)
    assert sorted(out) == sorted(v for v, _ in pairs)
    assert len(set(out)) == len(out)


def test_the_top_result_stays_first():
    # Diversifying reorders; it must not demote the most relevant video.
    pairs = [("top", "US"), ("b", "US"), ("c", "KR")]
    assert origin.diversify(pairs)[0] == "top"


def test_consecutive_picks_come_from_different_countries():
    pairs = [("a", "US"), ("b", "US"), ("c", "US"), ("d", "KR"), ("e", "GB")]
    out = origin.diversify(pairs)
    lookup = dict(pairs)
    assert countries(out[:3], lookup) == ["US", "KR", "GB"]


def test_the_first_eight_span_as_many_countries_as_available():
    # The real shape: one country dominates the top of the results.
    pairs = [(f"us{i}", "US") for i in range(10)] + [
        ("kr", "KR"),
        ("gb", "GB"),
        ("es", "ES"),
        ("au", "AU"),
    ]
    out = origin.diversify(pairs)[:8]
    lookup = dict(pairs)
    assert len(set(countries(out, lookup))) == 5, countries(out, lookup)


def test_relevance_order_is_preserved_within_a_country():
    pairs = [("us1", "US"), ("us2", "US"), ("us3", "US"), ("kr1", "KR")]
    out = origin.diversify(pairs)
    us_order = [v for v in out if v.startswith("us")]
    assert us_order == ["us1", "us2", "us3"]


def test_unknown_origin_is_one_bucket_not_many():
    # Otherwise unknown-country videos -- 42% of a real result set -- would
    # take a turn each and crowd out the countries we do know.
    pairs = [("u1", None), ("u2", None), ("u3", None), ("kr", "KR"), ("gb", "GB")]
    out = origin.diversify(pairs)
    assert out[:3] == ["u1", "kr", "gb"], out


def test_videos_of_unknown_origin_are_still_used():
    pairs = [("u1", None), ("u2", None), ("kr", "KR")]
    assert set(origin.diversify(pairs)) == {"u1", "u2", "kr"}


def test_a_single_country_is_left_in_its_original_order():
    pairs = [(f"v{i}", "KR") for i in range(5)]
    assert origin.diversify(pairs) == [f"v{i}" for i in range(5)]


def test_parse_channel_ids_maps_video_to_channel():
    payload = {
        "items": [
            {"id": "vid1", "snippet": {"channelId": "chan1"}},
            {"id": "vid2", "snippet": {"channelId": "chan2"}},
        ]
    }
    assert origin.parse_channel_ids(payload) == {"vid1": "chan1", "vid2": "chan2"}


def test_parse_channel_ids_skips_incomplete_items():
    payload = {"items": [{"id": "vid1", "snippet": {}}, {"snippet": {"channelId": "c"}}]}
    assert origin.parse_channel_ids(payload) == {}


def test_parse_countries_reads_the_optional_field():
    payload = {
        "items": [
            {"id": "chan1", "snippet": {"country": "KR"}},
            {"id": "chan2", "snippet": {}},
        ]
    }
    assert origin.parse_countries(payload) == {"chan1": "KR", "chan2": None}


def test_parse_handles_an_empty_response():
    assert origin.parse_channel_ids({}) == {}
    assert origin.parse_countries({}) == {}


def test_chunk_respects_the_fifty_id_batch_limit():
    ids = [f"v{i}" for i in range(120)]
    batches = origin.chunk(ids)
    assert [len(b) for b in batches] == [50, 50, 20]
    assert [v for b in batches for v in b] == ids


def test_chunk_of_an_empty_list_is_empty():
    assert origin.chunk([]) == []
