"""Conservative deal scoring against each product's recent observed prices."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from numbers import Real
from statistics import median
from typing import Any


_WINDOW = timedelta(days=30)
_MIN_OBSERVATIONS = 3
_MIN_DISTINCT_DAYS = 2
_REAL_DISCOUNT_PERCENT = 5.0
_FAKE_LIST_MARKUP = 1.15
_RECENT_HIKE_MARKUP = 1.10


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_utc(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = _utc_datetime(value)
    if parsed is None:
        raise ValueError("now must be an ISO date/time or datetime/date")
    return parsed


def score_product(
    history: list[dict[str, Any]],
    current_price: Real,
    list_price: Real | None = None,
    now: datetime | date | str | None = None,
) -> dict[str, str | float | int | None]:
    """Score a current price against valid observations from the prior 30 days.

    A real deal needs at least three prior observations across two UTC dates and
    a 5% drop below their median. A list price is treated as inflated only when
    it is at least 15% above that median and 5% above the current price. A hike
    requires two elevated observations in the prior 72 hours, followed by a
    current price no more than 5% below the median. A confirmed historical drop
    takes precedence over either misleading-price signal.
    """
    current = _price(current_price)
    if current is None:
        raise ValueError("current_price must be a finite positive number")
    reference_time = _now_utc(now)
    list_value = _price(list_price)

    observations: list[tuple[datetime, float]] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        observed_at = _utc_datetime(item.get("date"))
        observed_price = _price(item.get("price"))
        if observed_at is None or observed_price is None:
            continue
        if not reference_time - _WINDOW <= observed_at <= reference_time:
            continue
        # If callers include the current crawl row, exclude its exact timestamp.
        if observed_at == reference_time and observed_price == current:
            continue
        observations.append((observed_at, observed_price))

    observed_days = {observed_at.date() for observed_at, _ in observations}
    if len(observations) < _MIN_OBSERVATIONS or len(observed_days) < _MIN_DISTINCT_DAYS:
        return {"status": "new", "discount_percent": 0.0, "lowest_days": None}

    baseline = float(median(price for _, price in observations))
    discount_percent = (baseline - current) / baseline * 100
    real_discount = discount_percent >= _REAL_DISCOUNT_PERCENT

    fake_list_price = (
        list_value is not None
        and list_value >= baseline * _FAKE_LIST_MARKUP
        and list_value >= current * 1.05
    )
    recent_hikes = [
        (observed_at, price)
        for observed_at, price in observations
        if observed_at >= reference_time - timedelta(days=3)
        and price >= baseline * _RECENT_HIKE_MARKUP
    ]
    distinct_hike_times = {observed_at for observed_at, _ in recent_hikes}
    fake_hike = (
        len(distinct_hike_times) >= 2
        and current >= baseline * 0.95
        and current <= max(price for _, price in recent_hikes) * 0.95
    )

    if real_discount:
        status = "real"
    elif fake_list_price or fake_hike:
        status = "fake"
    else:
        status = "none"

    lowest_days = None
    if current <= min(price for _, price in observations):
        first_day = min(observed_days)
        lowest_days = max(1, (reference_time.date() - first_day).days)

    return {
        "status": status,
        "discount_percent": round(discount_percent, 2),
        "lowest_days": lowest_days,
    }
