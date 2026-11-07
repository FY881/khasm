#!/usr/bin/env python3
"""Long-running, rate-limited price collection workers for the data branch."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from scraper import scrape
from scraper.reader import NoonReader, is_explicit_access_denial, noon_search_url
from scraper.scoring import score_product

LOG = logging.getLogger("khasm.worker")
BIG_CHANGE_RATIO = 0.20
CONFIRMATION_TOLERANCE = 0.005
MAX_CATEGORY_BACKOFF = 15 * 60
MAX_STORE_BACKOFF = 2 * 60 * 60


def utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_schedule(categories: Iterable[str], priority_categories: Iterable[str]) -> list[str]:
    """Spread high-interest categories across the available request slots."""
    ordered = list(dict.fromkeys(categories))
    if not ordered:
        raise ValueError("at least one category is required")
    priorities = set(priority_categories)
    unknown = priorities - set(ordered)
    if unknown:
        raise ValueError(f"priority categories are not in this shard: {sorted(unknown)}")
    weights = {category: (6 if category in priorities else 1) for category in ordered}
    total_weight = sum(weights.values())
    current = {category: 0 for category in ordered}
    schedule = []
    for _ in range(total_weight):
        for category in ordered:
            current[category] += weights[category]
        selected = max(ordered, key=lambda category: current[category])
        current[selected] -= total_weight
        schedule.append(selected)
    return schedule


def is_large_change(previous: float | None, current: float, threshold: float = BIG_CHANGE_RATIO) -> bool:
    if previous is None or previous <= 0 or current <= 0:
        return False
    return abs(current - previous) / previous >= threshold


def confirm_changed_prices(
    first_read: list[dict[str, Any]],
    second_read: list[dict[str, Any]],
    previous_prices: dict[str, float],
    *,
    threshold: float = BIG_CHANGE_RATIO,
    tolerance: float = CONFIRMATION_TOLERANCE,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep ordinary reads; keep a large change only if a second read agrees."""
    second_by_id = {item["id"]: item for item in second_read}
    accepted: list[dict[str, Any]] = []
    rejected: list[str] = []
    for item in first_read:
        previous = previous_prices.get(item["id"])
        price = float(item["current_price"])
        if not is_large_change(previous, price, threshold):
            accepted.append(item)
            continue
        confirmation = second_by_id.get(item["id"])
        if confirmation is None:
            rejected.append(item["id"])
            continue
        confirmed_price = float(confirmation["current_price"])
        if abs(confirmed_price - price) / price > tolerance:
            rejected.append(item["id"])
            continue
        accepted.append(confirmation)
    return accepted, rejected


class RequestGate:
    """Enforce a minimum interval between request starts in one store worker."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if interval < 0:
            raise ValueError("request interval must not be negative")
        self.interval = interval
        self.clock = clock
        self.sleeper = sleeper
        self.last_started: float | None = None

    def wait(self, deadline: float | None = None) -> bool:
        if self.last_started is None:
            return deadline is None or self.clock() < deadline
        remaining = self.last_started + self.interval - self.clock()
        if remaining > 0:
            if deadline is not None and self.clock() + remaining >= deadline:
                return False
            self.sleeper(remaining)
        return deadline is None or self.clock() < deadline

    def mark_started(self) -> None:
        self.last_started = self.clock()


class DataBranchPublisher:
    """Publish to an isolated, seeded data branch without switching the scraper checkout."""

    def __init__(self, root: Path, branch: str = "data"):
        self.source_root = root.resolve()
        self.root = self.source_root
        self.branch = branch
        self._worktree: Path | None = None
        self._temp_dir: Path | None = None

    def _git_at(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, check=False,
        )
        if check and result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
        return result

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._git_at(self.root, *args, check=check)

    def _remote_branch_exists(self) -> bool:
        result = self._git("ls-remote", "--heads", "origin", self.branch)
        return bool(result.stdout.strip())

    def _prepare_worktree(self) -> Path:
        if self._worktree is not None:
            return self._worktree
        self._temp_dir = Path(tempfile.mkdtemp(prefix="khasm-data-"))
        self._worktree = self._temp_dir / "branch"
        exists = self._remote_branch_exists()
        if exists:
            self._git("fetch", "origin", self.branch)
            self._git("worktree", "add", "--detach", str(self._worktree), f"refs/remotes/origin/{self.branch}")
        else:
            self._git("worktree", "add", "--detach", str(self._worktree), "HEAD")
            self._git_at(self._worktree, "switch", "--orphan", f"data-seed-{os.getpid()}")
            for child in self._worktree.iterdir():
                if child.name == ".git":
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            source_data = self.source_root / "data"
            if source_data.is_dir():
                shutil.copytree(source_data, self._worktree / "data")
            else:
                (self._worktree / "data").mkdir(parents=True)
            self._git_at(self._worktree, "config", "user.name", "github-actions[bot]")
            self._git_at(self._worktree, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
            self._git_at(self._worktree, "add", "-A")
            self._git_at(self._worktree, "commit", "-m", "Seed price data from main")
            pushed = self._git_at(self._worktree, "push", "origin", f"HEAD:{self.branch}", check=False)
            if pushed.returncode:
                self._git("fetch", "origin", self.branch)
                self._git_at(self._worktree, "reset", "--hard", f"origin/{self.branch}")
        self.root = self._worktree
        self._git("config", "user.name", "github-actions[bot]")
        self._git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        return self.root

    def checkout(self) -> None:
        self._prepare_worktree()

    def close(self) -> None:
        if self._worktree is not None:
            self._git_at(self.source_root, "worktree", "remove", "--force", str(self._worktree), check=False)
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._worktree = self._temp_dir = None
        self.root = self.source_root

    @staticmethod
    def _read_products(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        if not path.exists():
            return {"updated_at": None, "products": []}, {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot safely read site snapshot {path}: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise RuntimeError(f"invalid site snapshot: {path}")
        products: dict[str, dict[str, Any]] = {}
        for item in payload["products"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise RuntimeError(f"invalid product in site snapshot: {path}")
            products[item["id"]] = item
        return payload, products

    def _update_snapshot(self, checked_products: list[dict[str, Any]], history_rows: list[dict[str, Any]]) -> None:
        data_dir = self.root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "products.json"
        payload, products = self._read_products(path)
        unique_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in history_rows:
            unique_rows[(row["id"], row["date"])] = row
        rows_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in unique_rows.values():
            rows_by_id[row["id"]].append(row)
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - 30 * 86400
        checked_by_id: dict[str, dict[str, Any]] = {}
        for item in checked_products:
            product_id = item["id"]
            current = checked_by_id.get(product_id)
            if current is None or str(item.get("last_checked", "")) >= str(current.get("last_checked", "")):
                checked_by_id[product_id] = item
        for observed in checked_by_id.values():
            product_id = observed["id"]
            old = products.get(product_id, {})
            history = list(old.get("history", [])) if isinstance(old.get("history"), list) else []
            for row in rows_by_id.get(product_id, []):
                point = {"date": row["date"], "price": row["price"], "sample_type": row["sample_type"]}
                if row.get("daily_sample"):
                    point["daily_sample"] = True
                history.append(point)
            history = [
                point for point in history
                if isinstance(point, dict) and isinstance(point.get("date"), str)
                and scrape._datetime(point["date"]).timestamp() >= cutoff
            ]
            scoring_history = [
                point for point in history
                if point.get("sample_type") in (None, "daily") or point.get("daily_sample") is True
            ]
            current_price = float(observed["current_price"])
            checked_at = observed["last_checked"]
            try:
                score_now = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                raise RuntimeError(f"invalid last_checked timestamp for {product_id}")
            score = score_product(
                scoring_history, current_price, observed.get("list_price"), now=score_now,
            )
            score["lowest_days"] = score_product(
                history, current_price, observed.get("list_price"), now=score_now,
            )["lowest_days"]
            products[product_id] = {
                **old,
                **{key: observed.get(key) for key in ("id", "store", "title", "url", "image", "list_price", "seller", "category")},
                "current_price": current_price,
                "last_checked": checked_at,
                "history": history,
                "history_is_sampled": True,
                "score": score,
            }
        payload.pop("blocked_pages", None)
        payload.pop("unavailable_pages", None)
        payload.update({
            "updated_at": utc_stamp(),
            "history_sampling": {"method": "price_changes_plus_daily_samples", "complete": False},
            "products": sorted(products.values(), key=lambda item: (
                str(item.get("store", "")), str(item.get("category", "")),
                str(item.get("title", "")).casefold(), item["id"],
            )),
        })
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(path)

    def publish(
        self,
        store: str,
        rows_by_category: dict[str, list[dict[str, Any]]],
        checked_products: list[dict[str, Any]] | None = None,
    ) -> int:
        latest_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for rows in rows_by_category.values():
            for row in rows:
                latest_rows[(row["id"], row["date"])] = row
        batches: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in latest_rows.values():
            batches[row["category"]].append(row)
        batches = dict(batches)
        checked_products = checked_products or []
        if not batches and not checked_products:
            return 0
        paths = [(Path("data") / "prices" / store / f"{category}.jsonl").as_posix() for category in batches]
        row_count = sum(map(len, batches.values()))
        for attempt in range(5):
            if attempt:
                self._git("fetch", "origin", self.branch)
                self._git("reset", "--hard", f"origin/{self.branch}")
            for category, rows in batches.items():
                path = self.root / "data" / "prices" / store / f"{category}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as output:
                    for row in rows:
                        output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._update_snapshot(checked_products, [row for rows in batches.values() for row in rows])
            if paths:
                self._git("add", "--", *paths)
            self._git("add", "--", "data/products.json")
            if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return 0
            self._git("commit", "-m", f"data: prices {store} batch ({row_count} observations)")
            commit = self._git("rev-parse", "HEAD").stdout.strip()
            result = self._git("push", "origin", f"HEAD:{self.branch}", check=False)
            if result.returncode == 0:
                return row_count
            self._git("fetch", "origin", self.branch)
            if self._git("merge-base", "--is-ancestor", commit, f"origin/{self.branch}", check=False).returncode == 0:
                return row_count
            if attempt == 4:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"could not publish data batch after retries: {detail}")
            time.sleep(random.uniform(1.0, 4.0) * (attempt + 1))
        raise RuntimeError("data publish retry loop ended unexpectedly")


class PriceWorker:
    def __init__(
        self,
        store: str,
        categories: list[str],
        publisher: DataBranchPublisher,
        *,
        priority_categories: list[str],
        request_interval: float = 60,
        publish_interval: float = 300,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        scraper_factory: Callable[[], Any] | None = None,
    ):
        if store not in scrape.HOSTS:
            raise ValueError(f"unsupported store: {store}")
        if not categories or any(category not in scrape.QUERIES for category in categories):
            raise ValueError("categories must be supported and non-empty")
        if request_interval <= 0 or publish_interval <= 0:
            raise ValueError("request and publish intervals must be positive")
        self.store = store
        self.categories = list(dict.fromkeys(categories))
        self.schedule = build_schedule(self.categories, priority_categories)
        self.publisher = publisher
        self.publish_interval = publish_interval
        self.clock = clock
        self.sleeper = sleeper
        self.gate = RequestGate(request_interval, clock=clock, sleeper=sleeper)
        self.scraper = (scraper_factory or (lambda: scrape.StoreScraper(delay=0, retries=0, timeout=20)))()
        self.reader = NoonReader() if store == "noon" else None
        self.reader_urls = {category: noon_search_url(category) for category in self.categories} if store == "noon" else {}
        self.urls = {
            category: url for store_name, category, url in scrape.search_pages()
            if store_name == store and category in self.categories
        }
        if set(self.urls) != set(self.categories):
            raise RuntimeError("search page definitions do not cover worker categories")
        self.latest_prices: dict[str, float] = {}
        self.product_state: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.pending_checked: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.last_publish = clock()
        self.category_failures: dict[str, int] = defaultdict(int)
        self.category_retry_at: dict[str, float] = defaultdict(float)
        self.store_failures = 0
        self.store_retry_at = 0.0

    def _load_latest_prices(self) -> dict[str, float]:
        path = self.publisher.root / "data" / "products.json"
        _, products = DataBranchPublisher._read_products(path)
        self.product_state = {
            product_id: item for product_id, item in products.items()
            if item.get("store") == self.store and item.get("category") in self.categories
        }
        latest = {}
        for product_id, item in self.product_state.items():
            price = item.get("current_price")
            if (isinstance(price, bool) or not isinstance(price, (int, float))
                    or not math.isfinite(price) or price <= 0):
                raise RuntimeError(f"invalid saved current price for {product_id}")
            latest[product_id] = float(price)
        return latest

    def _request(self, category: str, deadline: float) -> tuple[list[dict[str, Any]] | None, str | None]:
        if not self.gate.wait(deadline) or self.clock() >= deadline:
            return None, None
        self.gate.mark_started()
        html, error = self.scraper.fetch(self.store, self.urls[category])
        if error:
            if self.reader is not None and not is_explicit_access_denial(error):
                return self._reader_request(category, deadline, error)
            return None, error
        products = scrape.parse_search_page(self.store, category, self.urls[category], html or "")
        if products or self.reader is None:
            return products, None
        return self._reader_request(category, deadline, "No products parsed from Noon primary response")

    def _reader_request(
        self, category: str, deadline: float, primary_error: str,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        products, error = self.reader.fetch(
            self.reader_urls[category], category, gate=self.gate, deadline=deadline, clock=self.clock,
        )
        if error:
            return None, f"{primary_error}; Reader fallback failed: {error}"
        if products:
            LOG.info("Noon Reader fallback returned %d Egypt/EGP products for %s", len(products), category)
        return products, None

    def _has_pending(self) -> bool:
        return any(self.pending.values()) or any(self.pending_checked.values())

    def _flush_if_due(self, now: float) -> None:
        if self._has_pending() and now - self.last_publish >= self.publish_interval:
            self.flush()

    def _sleep_until(self, target: float, deadline: float) -> None:
        now = self.clock()
        duration = min(max(0.0, target - now), max(0.0, deadline - now))
        if duration > 0:
            self.sleeper(duration)

    def _next_ready_category(self, now: float, schedule_index: int) -> tuple[str | None, int]:
        for offset in range(len(self.schedule)):
            index = (schedule_index + offset) % len(self.schedule)
            category = self.schedule[index]
            if self.category_retry_at[category] <= now:
                return category, index + 1
        return None, schedule_index + len(self.schedule)

    def _handle_failure(self, category: str, error: str) -> None:
        lower = error.lower()
        if "403" in lower or "429" in lower or "challenge" in lower:
            self.store_failures += 1
            delay = min(MAX_STORE_BACKOFF, 30 * 60 * (2 ** min(self.store_failures - 1, 3)))
            self.store_retry_at = self.clock() + delay * random.uniform(0.9, 1.1)
            LOG.warning("Pausing %s after access denial/challenge for about %d minutes", self.store, round(delay / 60))
            return
        self.category_failures[category] += 1
        delay = min(MAX_CATEGORY_BACKOFF, 60 * (2 ** min(self.category_failures[category] - 1, 4)))
        self.category_retry_at[category] = self.clock() + delay * random.uniform(0.9, 1.1)
        LOG.warning("Backing off %s/%s for about %d seconds: %s", self.store, category, round(delay), error)

    def _observations(self, category: str, products: list[dict[str, Any]]) -> int:
        stamp = utc_stamp()
        today = stamp[:10]
        for item in products:
            product_id = item["id"]
            price = float(item["current_price"])
            old = self.product_state.get(product_id, {})
            history = old.get("history", []) if isinstance(old.get("history"), list) else []
            sampled_today = any(
                isinstance(point, dict) and isinstance(point.get("date"), str)
                and point["date"][:10] == today
                and (point.get("sample_type") in (None, "daily") or point.get("daily_sample") is True)
                for point in history
            )
            changed = product_id not in self.latest_prices or price != self.latest_prices[product_id]
            if changed or not sampled_today:
                row = {
                    "id": product_id, "store": self.store, "category": category,
                    "date": stamp, "price": price,
                    "sample_type": "daily" if not sampled_today else "change",
                    "daily_sample": not sampled_today,
                    "title": item["title"], "url": item["url"],
                    "image": item.get("image"), "seller": item.get("seller"),
                }
                same_check_rows = [
                    queued for category_rows in self.pending.values() for queued in category_rows
                    if queued["id"] == product_id and queued["date"] == stamp
                ]
                if any(queued.get("daily_sample") for queued in same_check_rows):
                    row["daily_sample"] = True
                    row["sample_type"] = "daily"
                for queued_category in list(self.pending):
                    self.pending[queued_category] = [
                        queued for queued in self.pending[queued_category]
                        if not (queued["id"] == product_id and queued["date"] == stamp)
                    ]
                self.pending[category].append(row)
                history = list(history)
                daily_sample = row.get("daily_sample") is True or any(
                    point.get("date") == stamp and point.get("daily_sample") is True
                    for point in history if isinstance(point, dict)
                )
                history = [point for point in history if not (
                    isinstance(point, dict) and point.get("date") == stamp
                )]
                history.append({
                    "date": row["date"], "price": row["price"],
                    "sample_type": row["sample_type"],
                    **({"daily_sample": True} if daily_sample else {}),
                })
            checked = {
                "id": product_id, "store": self.store, "category": category,
                "title": item["title"], "url": item["url"], "image": item.get("image"),
                "current_price": price, "list_price": item.get("list_price"),
                "seller": item.get("seller"), "last_checked": stamp,
            }
            self.pending_checked[category].append(checked)
            self.product_state[product_id] = {**old, **checked, "history": history}
            self.latest_prices[product_id] = price
        return len(products)

    def flush(self) -> int:
        if not any(self.pending.values()) and not any(self.pending_checked.values()):
            self.last_publish = self.clock()
            return 0
        checked_by_id: dict[str, dict[str, Any]] = {}
        for items in self.pending_checked.values():
            for item in items:
                previous = checked_by_id.get(item["id"])
                if previous is None or item["last_checked"] >= previous["last_checked"]:
                    checked_by_id[item["id"]] = item
        checked = list(checked_by_id.values())
        count = self.publisher.publish(self.store, dict(self.pending), checked_products=checked)
        self.pending.clear()
        self.pending_checked.clear()
        self.last_publish = self.clock()
        LOG.info("Published %d observations for %s on data branch", count, self.store)
        return count

    def run(self, duration_seconds: float, stagger_seconds: float = 0) -> dict[str, int]:
        if duration_seconds <= 0 or stagger_seconds < 0:
            raise ValueError("duration must be positive and stagger non-negative")
        self.publisher.checkout()
        self.latest_prices = self._load_latest_prices()
        self.last_publish = self.clock()
        started = self.clock()
        deadline = started + duration_seconds
        if stagger_seconds:
            self.sleeper(min(stagger_seconds, max(0, deadline - self.clock())))
        page_count = product_count = blocked_count = rejected_count = 0
        schedule_index = 0
        while self.clock() < deadline:
            now = self.clock()
            self._flush_if_due(now)
            if self.store_retry_at > now:
                target = self.store_retry_at
                if self._has_pending():
                    target = min(target, self.last_publish + self.publish_interval)
                self._sleep_until(target, deadline)
                self._flush_if_due(self.clock())
                continue
            category, schedule_index = self._next_ready_category(now, schedule_index)
            if category is None:
                target = min(self.category_retry_at[name] for name in self.categories)
                if self._has_pending():
                    target = min(target, self.last_publish + self.publish_interval)
                self._sleep_until(target, deadline)
                self._flush_if_due(self.clock())
                continue
            if self.clock() >= deadline:
                break
            products, error = self._request(category, deadline)
            if products is None and error is None:
                break
            page_count += 1
            if error:
                blocked_count += 1
                self._handle_failure(category, error)
            else:
                self.category_failures[category] = 0
                self.store_failures = 0
                if not products:
                    LOG.warning("No products parsed for %s/%s; previous observations retained", self.store, category)
                else:
                    large = [item for item in products if is_large_change(
                        self.latest_prices.get(item["id"]), float(item["current_price"]),
                    )]
                    if large:
                        reread, reread_error = self._request(category, deadline)
                        if reread is None and reread_error is None:
                            _, rejected = confirm_changed_prices(products, [], self.latest_prices)
                            rejected_count += len(rejected)
                            if rejected:
                                LOG.warning("Suppressed %d large changes not confirmed before deadline for %s/%s", len(rejected), self.store, category)
                            products = [item for item in products if item["id"] not in set(rejected)]
                            product_count += self._observations(category, products)
                            break
                        if reread_error:
                            blocked_count += 1
                            self._handle_failure(category, reread_error)
                            confirmed, rejected = confirm_changed_prices(products, [], self.latest_prices)
                            LOG.warning("Could not confirm %d large changes for %s/%s: %s", len(rejected), self.store, category, reread_error)
                        else:
                            confirmed, rejected = confirm_changed_prices(products, reread or [], self.latest_prices)
                        rejected_count += len(rejected)
                        if rejected:
                            LOG.warning("Suppressed %d unconfirmed large changes for %s/%s", len(rejected), self.store, category)
                        products = confirmed
                    product_count += self._observations(category, products)
            self._flush_if_due(self.clock())
        self.flush()
        self.publisher.close()
        result = {
            "pages": page_count, "products": product_count, "blocked": blocked_count,
            "unconfirmed_changes": rejected_count,
        }
        LOG.info("Worker finished for %s: %s", self.store, result)
        return result


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, choices=sorted(scrape.HOSTS))
    parser.add_argument("--categories", default=",".join(scrape.QUERIES))
    parser.add_argument("--priority-categories", default="phones,laptops")
    parser.add_argument("--duration-seconds", type=float, default=19_800)
    parser.add_argument("--request-interval", type=float, default=60)
    parser.add_argument("--publish-interval", type=float, default=300)
    parser.add_argument("--stagger-seconds", type=float, default=0)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.getenv("KHASM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    worker = None
    try:
        worker = PriceWorker(
            args.store, _csv(args.categories), DataBranchPublisher(scrape.ROOT),
            priority_categories=_csv(args.priority_categories),
            request_interval=args.request_interval, publish_interval=args.publish_interval,
        )
        result = worker.run(args.duration_seconds, args.stagger_seconds)
    except Exception:
        LOG.exception("Price worker failed; existing remote observations were not deleted")
        return 1
    finally:
        if worker is not None:
            worker.publisher.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
