from collections import Counter
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scraper.worker import (
    DataBranchPublisher,
    PriceWorker,
    RequestGate,
    build_schedule,
    confirm_changed_prices,
    is_large_change,
    utc_stamp,
)


def test_priority_schedule_polls_priority_categories_more_often():
    categories = ["phones", "laptops", "headphones", "tvs", "appliances"]
    schedule = build_schedule(categories, ["phones", "laptops"])
    counts = Counter(schedule)

    assert counts == {"phones": 6, "laptops": 6, "headphones": 1, "tvs": 1, "appliances": 1}
    for priority in ("phones", "laptops"):
        slots = [index for index, category in enumerate(schedule) if category == priority]
        gaps = [b - a for a, b in zip(slots, slots[1:] + [slots[0] + len(schedule)])]
        assert max(gaps) <= 4


def test_ten_category_schedule_revisits_priority_pages_within_four_minutes():
    from scraper.scrape import QUERIES

    schedule = build_schedule(QUERIES, ["phones", "laptops"])

    assert len(schedule) == 20
    for priority in ("phones", "laptops"):
        slots = [index for index, category in enumerate(schedule) if category == priority]
        gaps = [b - a for a, b in zip(slots, slots[1:] + [slots[0] + len(schedule)])]
        assert max(gaps) <= 4


def test_schedule_rejects_missing_categories_and_unknown_priority():
    with pytest.raises(ValueError, match="at least one"):
        build_schedule([], [])
    with pytest.raises(ValueError, match="not in this shard"):
        build_schedule(["phones"], ["laptops"])


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [(100, 80, True), (100, 120, True), (100, 81, False), (None, 50, False), (0, 50, False)],
)
def test_large_price_change_threshold(previous, current, expected):
    assert is_large_change(previous, current) is expected


def product(product_id, price, seller="seller"):
    return {"id": product_id, "current_price": price, "seller": seller}


def test_big_price_change_requires_matching_second_read():
    first = [product("p1", 80), product("p2", 70), product("p3", 70)]
    second = [product("p1", 80.2), product("p2", 88)]

    accepted, rejected = confirm_changed_prices(first, second, {"p1": 100, "p2": 100, "p3": 100})

    assert [item["id"] for item in accepted] == ["p1"]
    assert rejected == ["p2", "p3"]
    assert accepted[0]["current_price"] == 80.2


def test_new_or_small_price_change_does_not_require_confirmation():
    first = [product("new", 80), product("stable", 99)]
    accepted, rejected = confirm_changed_prices(first, [], {"stable": 100})

    assert accepted == first
    assert rejected == []


def test_request_gate_enforces_minimum_start_interval():
    now = [10.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    gate = RequestGate(60, clock=lambda: now[0], sleeper=sleep)
    gate.wait()
    gate.mark_started()
    now[0] += 12
    gate.wait()
    gate.mark_started()

    assert sleeps == [48]
    assert now[0] == 70


def test_request_gate_does_not_wait_past_run_deadline():
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    gate = RequestGate(60, clock=lambda: now[0], sleeper=sleep)
    gate.mark_started()

    assert not gate.wait(deadline=30)
    assert sleeps == []
    assert gate.wait(deadline=61)
    assert sleeps == [60]
    assert now[0] == 60


def test_worker_does_not_start_request_when_gate_wait_would_cross_deadline(tmp_path):
    now = [0.0]

    class Publisher(DataBranchPublisher):
        def checkout(self):
            pass

        def publish(self, store, rows_by_category, checked_products=None):
            return 0

    class Scraper:
        calls = 0

        def fetch(self, store, url):
            self.calls += 1
            raise AssertionError("request must not start after the worker deadline")

    scraper = Scraper()
    worker = PriceWorker(
        "amazon", ["phones"], Publisher(tmp_path), priority_categories=["phones"],
        request_interval=60, clock=lambda: now[0], sleeper=lambda duration: now.__setitem__(0, now[0] + duration),
        scraper_factory=lambda: scraper,
    )
    worker.gate.mark_started()

    result = worker.run(30)

    assert result["pages"] == 0
    assert scraper.calls == 0
    assert now[0] == 0


def test_pending_success_flushes_on_schedule_during_store_backoff(tmp_path):
    now = [0.0]
    publishes = []

    class Publisher(DataBranchPublisher):
        def checkout(self):
            pass

        def publish(self, store, rows_by_category, checked_products=None):
            publishes.append((now[0], checked_products))
            return sum(map(len, rows_by_category.values()))

    def sleep(seconds):
        now[0] += seconds

    worker = PriceWorker(
        "amazon", ["phones"], Publisher(tmp_path), priority_categories=["phones"],
        request_interval=0.1, publish_interval=3, clock=lambda: now[0], sleeper=sleep,
        scraper_factory=lambda: object(),
    )
    item = {
        "id": "amazon-phone", "store": "amazon", "category": "phones", "title": "Phone",
        "url": "https://www.amazon.eg/dp/B000000001", "image": None,
        "current_price": 100, "list_price": None, "seller": "Seller",
    }
    replies = iter([([item], None), (None, "HTTP 403 (no retry)")])
    worker._request = lambda category, deadline: next(replies)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("scraper.worker.random.uniform", lambda _low, _high: 1.0)
        worker.run(10)

    assert [time for time, _ in publishes] == [3]
    assert publishes[0][1]


def test_all_categories_in_backoff_sleep_instead_of_spinning(tmp_path):
    now = [0.0]
    sleeps = []
    requests = []

    class Publisher(DataBranchPublisher):
        def checkout(self):
            pass

        def publish(self, store, rows_by_category, checked_products=None):
            return 0

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    worker = PriceWorker(
        "amazon", ["phones", "headphones"], Publisher(tmp_path), priority_categories=[],
        request_interval=0.1, publish_interval=3, clock=lambda: now[0], sleeper=sleep,
        scraper_factory=lambda: object(),
    )

    def fail(category, deadline):
        requests.append((category, now[0]))
        return None, "request error: ReadTimeout"

    worker._request = fail
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("scraper.worker.random.uniform", lambda _low, _high: 1.0)
        worker.run(2)

    assert len(requests) == 2
    assert sleeps == [2]


def test_unchanged_price_refreshes_last_checked_without_history_rows(tmp_path):
    class Publisher(DataBranchPublisher):
        def __init__(self, root):
            super().__init__(root)
            self.batches = []

        def publish(self, store, rows_by_category, checked_products=None):
            self.batches.append((rows_by_category, checked_products))
            return sum(map(len, rows_by_category.values()))

    publisher = Publisher(tmp_path)
    worker = PriceWorker(
        "amazon", ["phones"], publisher, priority_categories=["phones"],
        scraper_factory=lambda: object(),
    )
    checked_at = utc_stamp()
    item = {
        "id": "amazon-old", "store": "amazon", "category": "phones", "title": "Phone",
        "url": "https://www.amazon.eg/dp/B000000001", "image": None,
        "current_price": 100, "list_price": None, "seller": "Seller",
    }
    worker.product_state[item["id"]] = {
        **item, "history": [{"date": checked_at, "price": 100, "sample_type": "daily", "daily_sample": True}],
    }
    worker.latest_prices[item["id"]] = 100

    worker._observations("phones", [item])

    assert not worker.pending["phones"]
    assert worker.pending_checked["phones"][0]["last_checked"] >= checked_at
    worker.flush()
    assert publisher.batches[0][0] == {"phones": []}


def test_duplicate_product_checks_in_one_batch_create_one_daily_sample(tmp_path):
    class Publisher(DataBranchPublisher):
        def __init__(self, root):
            super().__init__(root)
            self.batch = None

        def publish(self, store, rows_by_category, checked_products=None):
            self.batch = (rows_by_category, checked_products)
            return sum(map(len, rows_by_category.values()))

    publisher = Publisher(tmp_path)
    worker = PriceWorker(
        "amazon", ["phones", "laptops"], publisher, priority_categories=[],
        scraper_factory=lambda: object(),
    )
    item = {
        "id": "amazon-duplicate", "store": "amazon", "title": "Phone",
        "url": "https://www.amazon.eg/dp/B000000001", "image": None,
        "current_price": 100, "list_price": None, "seller": "Seller",
    }

    worker._observations("phones", [{**item, "category": "phones"}])
    worker._observations("laptops", [{**item, "category": "laptops"}])

    history = worker.product_state[item["id"]]["history"]
    assert len(history) == 1
    assert history[0]["daily_sample"] is True
    assert sum(len(rows) for rows in worker.pending.values()) == 1
    assert len(worker.pending_checked["phones"]) + len(worker.pending_checked["laptops"]) == 2

    worker.flush()
    rows_by_category, checked = publisher.batch
    assert sum(len(rows) for rows in rows_by_category.values()) == 1
    assert len(checked) == 1
    assert checked[0]["category"] == "laptops"


def test_worker_keeps_old_data_when_a_page_is_blocked(tmp_path):
    class BlockedScraper:
        def fetch(self, store, url):
            return None, "HTTP 403 (no retry)"

    class Publisher(DataBranchPublisher):
        def __init__(self, root):
            super().__init__(root)
            self.published = []

        def checkout(self):
            pass

        def publish(self, store, rows_by_category):
            self.published.append(rows_by_category)
            return sum(map(len, rows_by_category.values()))

    path = tmp_path / "data/prices/noon/phones.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"id":"noon-old","store":"noon","category":"phones","date":"2026-11-01T00:00:00Z","price":50}\n',
        encoding="utf-8",
    )
    publisher = Publisher(tmp_path)
    worker = PriceWorker(
        "noon", ["phones"], publisher, priority_categories=["phones"],
        request_interval=1, publish_interval=1, scraper_factory=BlockedScraper,
    )
    worker.run(0.01)

    assert path.read_text(encoding="utf-8").count("noon-old") == 1
    assert publisher.published == []


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def init_remote_main(root, remote):
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "scraper").mkdir()
    (root / "scraper/keep.py").write_text("source stays on main\n", encoding="utf-8")
    (root / "data/prices").mkdir(parents=True)
    (root / "data/products.json").write_text(json.dumps({
        "updated_at": "2026-11-01T00:00:00Z", "products": [],
    }), encoding="utf-8")
    (root / "data/prices/amazon.jsonl").write_text(
        '{"id":"amazon-seed","store":"amazon","date":"2026-11-01T00:00:00Z","price":25}\n',
        encoding="utf-8",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "main seed")
    git(root, "init", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")


def test_missing_data_branch_is_seeded_in_isolated_worktree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    remote = tmp_path / "remote.git"
    init_remote_main(source, remote)
    publisher = DataBranchPublisher(source)

    publisher.checkout()

    assert (source / "scraper/keep.py").read_text(encoding="utf-8") == "source stays on main\n"
    assert (publisher.root / "data/products.json").exists()
    assert (publisher.root / "data/prices/amazon.jsonl").exists()
    assert "amazon-seed" in (publisher.root / "data/prices/amazon.jsonl").read_text(encoding="utf-8")
    publisher.close()
    assert (source / "scraper/keep.py").exists()


def test_concurrent_first_run_branch_creation_is_safe(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    remote = tmp_path / "remote.git"
    init_remote_main(source, remote)
    clones = [tmp_path / "worker-a", tmp_path / "worker-b"]
    for clone in clones:
        git(tmp_path, "clone", "--branch", "main", str(remote), str(clone))
    barrier = Barrier(2)

    class RacingPublisher(DataBranchPublisher):
        def _remote_branch_exists(self):
            exists = super()._remote_branch_exists()
            barrier.wait(timeout=10)
            return exists

    publishers = [RacingPublisher(clone) for clone in clones]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda publisher: publisher.checkout(), publishers))

    for publisher in publishers:
        assert (publisher.root / "data/products.json").exists()
        publisher.close()
    assert git(tmp_path, "--git-dir", str(remote), "show", "data:data/products.json").stdout


def test_publisher_writes_compact_site_snapshot_and_per_category_log(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    remote = tmp_path / "remote.git"
    init_remote_main(source, remote)
    publisher = DataBranchPublisher(source)
    publisher.checkout()
    checked_at = "2026-11-02T00:00:00Z"
    checked = [{
        "id": "amazon-new", "store": "amazon", "category": "phones", "title": "Phone",
        "url": "https://www.amazon.eg/dp/B000000001", "image": None, "current_price": 20,
        "list_price": None, "seller": "Seller", "last_checked": checked_at,
    }]
    rows = [{
        "id": "amazon-new", "store": "amazon", "category": "phones", "date": checked_at,
        "price": 20, "sample_type": "daily", "daily_sample": True,
    }]

    publisher.publish("amazon", {"phones": rows}, checked_products=checked)

    snapshot = json.loads((publisher.root / "data/products.json").read_text(encoding="utf-8"))
    assert snapshot["history_sampling"] == {"method": "price_changes_plus_daily_samples", "complete": False}
    new_product = next(item for item in snapshot["products"] if item["id"] == "amazon-new")
    assert new_product["last_checked"] == checked_at
    assert new_product["history"] == [{"date": checked_at, "price": 20.0, "sample_type": "daily", "daily_sample": True}]
    assert (publisher.root / "data/prices/amazon/phones.jsonl").exists()
    assert (source / "scraper/keep.py").exists()
    history_before = list(new_product["history"])
    refreshed = [{**checked[0], "last_checked": "2026-11-02T00:05:00Z"}]
    publisher.publish("amazon", {}, checked_products=refreshed)
    snapshot = json.loads((publisher.root / "data/products.json").read_text(encoding="utf-8"))
    new_product = next(item for item in snapshot["products"] if item["id"] == "amazon-new")
    assert new_product["last_checked"] == "2026-11-02T00:05:00Z"
    assert new_product["history"] == history_before
    publisher.close()


def test_snapshot_clears_stale_status_and_preserves_unchecked_last_checked(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    stale_checked = "2026-11-01T00:00:00Z"
    root.joinpath("products.json").write_text(json.dumps({
        "updated_at": stale_checked,
        "blocked_pages": [{"store": "noon", "reason": "old 403"}],
        "unavailable_pages": [{"store": "amazon", "reason": "old timeout"}],
        "products": [{
            "id": "noon-stale", "store": "noon", "title": "Old", "category": "phones",
            "url": "https://www.noon.com/egypt-ar/product/p/OLD", "current_price": 50,
            "last_checked": stale_checked, "history": [],
        }],
    }), encoding="utf-8")

    DataBranchPublisher._update_snapshot(
        DataBranchPublisher(tmp_path),
        [{
            "id": "amazon-current", "store": "amazon", "category": "phones", "title": "Current",
            "url": "https://www.amazon.eg/dp/B000000001", "image": None, "current_price": 20,
            "list_price": None, "seller": None, "last_checked": utc_stamp(),
        }],
        [],
    )

    snapshot = json.loads(root.joinpath("products.json").read_text(encoding="utf-8"))
    assert "blocked_pages" not in snapshot
    assert "unavailable_pages" not in snapshot
    stale_product = next(item for item in snapshot["products"] if item["id"] == "noon-stale")
    assert stale_product["last_checked"] == stale_checked


def test_snapshot_uses_latest_duplicate_check_and_appends_history_once(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    checked_at = utc_stamp()
    from datetime import datetime, timedelta

    later = (datetime.fromisoformat(checked_at.replace("Z", "+00:00")) + timedelta(minutes=1))
    later_stamp = later.isoformat().replace("+00:00", "Z")
    checked = [
        {
            "id": "amazon-duplicate", "store": "amazon", "category": "phones", "title": "Old",
            "url": "https://www.amazon.eg/dp/B000000001", "image": None,
            "current_price": 100, "list_price": None, "seller": None, "last_checked": checked_at,
        },
        {
            "id": "amazon-duplicate", "store": "amazon", "category": "laptops", "title": "Latest",
            "url": "https://www.amazon.eg/dp/B000000001", "image": None,
            "current_price": 90, "list_price": None, "seller": "Latest seller", "last_checked": later_stamp,
        },
    ]
    row = {
        "id": "amazon-duplicate", "store": "amazon", "category": "laptops",
        "date": later_stamp, "price": 90, "sample_type": "change",
    }

    DataBranchPublisher._update_snapshot(
        DataBranchPublisher(tmp_path), checked, [row, {**row, "category": "phones"}],
    )

    snapshot = json.loads(root.joinpath("products.json").read_text(encoding="utf-8"))
    assert len(snapshot["products"]) == 1
    product = snapshot["products"][0]
    assert product["title"] == "Latest"
    assert product["last_checked"] == later_stamp
    assert product["current_price"] == 90
    assert product["history"] == [{"date": later_stamp, "price": 90, "sample_type": "change"}]


def test_lowest_price_uses_non_daily_change_while_median_uses_daily_samples(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    checked_at = utc_stamp()
    from datetime import datetime, timedelta, timezone

    now = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    def stamp(days=0, hours=0):
        value = now - timedelta(days=days, hours=hours)
        return value.isoformat().replace("+00:00", "Z")

    history = [
        {"date": stamp(days=3), "price": 100, "sample_type": "daily", "daily_sample": True},
        {"date": stamp(days=2), "price": 100, "sample_type": "daily", "daily_sample": True},
        {"date": stamp(days=1), "price": 100, "sample_type": "daily", "daily_sample": True},
        {"date": stamp(hours=1), "price": 70, "sample_type": "change"},
    ]
    root.joinpath("products.json").write_text(json.dumps({
        "products": [{
            "id": "amazon-phone", "store": "amazon", "title": "Phone", "category": "phones",
            "url": "https://www.amazon.eg/dp/B000000001", "current_price": 70,
            "history": history,
        }],
    }), encoding="utf-8")

    DataBranchPublisher._update_snapshot(
        DataBranchPublisher(tmp_path),
        [{
            "id": "amazon-phone", "store": "amazon", "category": "phones", "title": "Phone",
            "url": "https://www.amazon.eg/dp/B000000001", "image": None, "current_price": 90,
            "list_price": None, "seller": None, "last_checked": checked_at,
        }],
        [],
    )

    updated = json.loads(root.joinpath("products.json").read_text(encoding="utf-8"))["products"][0]
    assert updated["score"]["status"] == "real"
    assert updated["score"]["discount_percent"] == 10.0
    assert updated["score"]["lowest_days"] is None


def test_concurrent_publishers_preserve_both_append_only_batches(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "data", str(seed))
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.invalid")
    (seed / "data").mkdir()
    (seed / "data/.gitkeep").write_text("", encoding="utf-8")
    git(seed, "add", "data/.gitkeep")
    git(seed, "commit", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "data")

    clones = [tmp_path / "worker-a", tmp_path / "worker-b"]
    for clone in clones:
        git(tmp_path, "clone", str(remote), str(clone))
    publishers = [DataBranchPublisher(clone) for clone in clones]
    for publisher in publishers:
        publisher.checkout()

    publishers[0].publish("noon", {"phones": [{
        "id": "first", "store": "noon", "category": "phones",
        "date": "2026-11-01T00:00:00Z", "price": 10,
    }]})
    publishers[1].publish("noon", {"phones": [{
        "id": "second", "store": "noon", "category": "phones",
        "date": "2026-11-01T00:00:00Z", "price": 9,
    }]})

    verify = tmp_path / "verify"
    git(tmp_path, "clone", "--branch", "data", str(remote), str(verify))
    rows = [
        json.loads(line)
        for line in (verify / "data/prices/noon/phones.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows == [
        {
            "id": "first", "store": "noon", "category": "phones",
            "date": "2026-11-01T00:00:00Z", "price": 10,
        },
        {
            "id": "second", "store": "noon", "category": "phones",
            "date": "2026-11-01T00:00:00Z", "price": 9,
        },
    ]
