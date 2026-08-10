"""The daily search budget.

Generating a query on every reload means every reload can cost 100 of the
10,000 daily units -- roughly 100 reloads before the wall is dead until
midnight Pacific. This module makes that ceiling explicit and enforceable
rather than something you discover by hitting it.
"""

import json

from ytmatrix import budget

TODAY = "2026-08-10"
TOMORROW = "2026-08-11"


def test_a_fresh_budget_has_nothing_spent(tmp_path):
    assert budget.spent(tmp_path, today=TODAY) == 0


def test_recording_a_search_costs_one_hundred_units(tmp_path):
    budget.record_search(tmp_path, today=TODAY)
    assert budget.spent(tmp_path, today=TODAY) == 100


def test_searches_accumulate(tmp_path):
    for _ in range(3):
        budget.record_search(tmp_path, today=TODAY)
    assert budget.spent(tmp_path, today=TODAY) == 300


def test_the_count_resets_on_a_new_day(tmp_path):
    budget.record_search(tmp_path, today=TODAY)
    assert budget.spent(tmp_path, today=TOMORROW) == 0


def test_would_exceed_is_false_below_the_limit(tmp_path):
    budget.record_search(tmp_path, today=TODAY)
    assert budget.would_exceed(tmp_path, limit_units=1000, today=TODAY) is False


def test_would_exceed_is_true_once_the_next_search_would_cross(tmp_path):
    for _ in range(3):
        budget.record_search(tmp_path, today=TODAY)
    # 300 spent, limit 350: another search would reach 400.
    assert budget.would_exceed(tmp_path, limit_units=350, today=TODAY) is True


def test_a_zero_limit_disables_the_guard(tmp_path):
    for _ in range(50):
        budget.record_search(tmp_path, today=TODAY)
    assert budget.would_exceed(tmp_path, limit_units=0, today=TODAY) is False


def test_a_corrupt_ledger_is_treated_as_empty_rather_than_crashing(tmp_path):
    budget.record_search(tmp_path, today=TODAY)
    (tmp_path / budget.LEDGER_NAME).write_text("{not json")
    assert budget.spent(tmp_path, today=TODAY) == 0


def test_the_ledger_records_the_date_it_belongs_to(tmp_path):
    budget.record_search(tmp_path, today=TODAY)
    payload = json.loads((tmp_path / budget.LEDGER_NAME).read_text())
    assert payload["date"] == TODAY
    assert payload["units"] == 100


def test_the_day_is_keyed_to_pacific_time_where_google_resets_quota(tmp_path, monkeypatch):
    # 05:00 UTC on Aug 11 is 22:00 on Aug 10 in Los Angeles (UTC-7 in summer).
    # Keying on the UTC date would hand back a fresh budget two hours before
    # Google actually refills it.
    import datetime as real_datetime

    class FakeDatetime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 8, 11, 5, 0, tzinfo=real_datetime.UTC).astimezone(
                tz
            )

    monkeypatch.setattr(budget, "datetime", FakeDatetime)
    budget.record_search(tmp_path)
    assert budget.spent(tmp_path, today="2026-08-10") == 100, "should count as Aug 10 in Pacific"
    assert budget.spent(tmp_path, today="2026-08-11") == 0


def test_recording_on_a_new_day_overwrites_rather_than_accumulates(tmp_path):
    budget.record_search(tmp_path, today=TODAY)
    budget.record_search(tmp_path, today=TOMORROW)
    assert budget.spent(tmp_path, today=TOMORROW) == 100
