from datetime import datetime, timezone

import pytest

from scraper.scoring import score_product


NOW = datetime(2026, 1, 6, 6, tzinfo=timezone.utc)


def history(*rows):
    return [{"date": date, "price": price} for date, price in rows]


def established_history(prices=(100, 100, 100)):
    return history(
        ("2026-01-01T09:00:00Z", prices[0]),
        ("2026-01-02T09:00:00Z", prices[1]),
        ("2026-01-03T09:00:00Z", prices[2]),
    )


def test_no_history_is_new():
    assert score_product([], 80, now=NOW) == {
        "status": "new",
        "discount_percent": 0.0,
        "lowest_days": None,
    }


def test_requires_three_observations_on_two_days():
    same_day = history(
        ("2026-01-05T00:00:00Z", 100),
        ("2026-01-05T03:00:00Z", 100),
        ("2026-01-05T06:00:00Z", 100),
    )
    assert score_product(same_day, 80, now=NOW)["status"] == "new"
    assert score_product(established_history()[:2], 80, now=NOW)["status"] == "new"


def test_real_discount_is_measured_against_history_median():
    result = score_product(established_history((90, 100, 110)), 95, now=NOW)
    assert result["status"] == "real"
    assert result["discount_percent"] == 5.0


def test_below_threshold_is_not_a_real_discount():
    result = score_product(established_history(), 95.01, now=NOW)
    assert result["status"] == "none"
    assert result["discount_percent"] == 4.99


def test_price_above_median_returns_signed_discount_without_deal_status():
    result = score_product(established_history(), 110, now=NOW)
    assert result["status"] == "none"
    assert result["discount_percent"] == -10.0


def test_inflated_list_price_is_fake_when_current_is_not_a_real_drop():
    result = score_product(established_history(), 100, list_price=120, now=NOW)
    assert result["status"] == "fake"
    assert result["discount_percent"] == 0.0


def test_sustained_recent_hike_followed_by_normal_price_is_fake():
    rows = established_history() + history(
        ("2026-01-05T00:00:00Z", 120),
        ("2026-01-05T03:00:00Z", 120),
    )
    result = score_product(rows, 100, now=NOW)
    assert result["status"] == "fake"


def test_genuine_historical_drop_takes_precedence_over_misleading_list_price():
    result = score_product(established_history(), 80, list_price=200, now=NOW)
    assert result["status"] == "real"
    assert result["discount_percent"] == 20.0


def test_lowest_days_uses_observed_history_span_only():
    result = score_product(established_history(), 80, now=NOW)
    assert result["status"] == "real"
    assert result["lowest_days"] == 5


def test_current_tied_with_observed_minimum_is_a_lowest_price():
    result = score_product(established_history(), 100, now=NOW)
    assert result["status"] == "none"
    assert result["lowest_days"] == 5


def test_stale_and_future_observations_do_not_count():
    rows = history(
        ("2025-12-01T00:00:00Z", 100),
        ("2025-12-02T00:00:00Z", 100),
        ("2025-12-03T00:00:00Z", 100),
        ("2026-01-07T00:00:00Z", 100),
    )
    assert score_product(rows, 80, now=NOW)["status"] == "new"


def test_exact_30_day_boundary_is_included_and_offsets_normalize_to_utc():
    rows = history(
        ("2025-12-07T08:00:00+02:00", 100),
        ("2025-12-07T09:00:00Z", 100),
        ("2025-12-08T09:00:00Z", 100),
    )
    assert score_product(rows, 90, now=NOW)["status"] == "real"


def test_exact_current_crawl_observation_is_not_used_as_baseline():
    rows = established_history()[:2] + history(("2026-01-06T06:00:00Z", 80))
    assert score_product(rows, 80, now=NOW)["status"] == "new"


def test_invalid_history_rows_and_list_price_are_ignored():
    rows = established_history() + [
        {"date": "not-a-date", "price": 100},
        {"date": "2026-01-05", "price": float("nan")},
        None,
    ]
    assert score_product(rows, 100, list_price=float("inf"), now=NOW)["status"] == "none"


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf"), "80"])
def test_invalid_current_price_raises(price):
    with pytest.raises(ValueError, match="current_price"):
        score_product([], price, now=NOW)


def test_invalid_now_raises():
    with pytest.raises(ValueError, match="now"):
        score_product([], 80, now="tomorrow")
