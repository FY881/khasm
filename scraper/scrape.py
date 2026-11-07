#!/usr/bin/env python3
"""Polite scraper for public Noon Egypt and Amazon Egypt search pages."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
PRODUCTS_FILE = DATA / "products.json"
PRICE_DIR = DATA / "prices"
LOG = logging.getLogger("khasm.scraper")
QUERIES = {
    "phones": ("هواتف ذكية", "smartphones"),
    "laptops": ("لابتوب", "laptops"),
    "headphones": ("سماعات", "headphones"),
    "tvs": ("تلفزيون", "smart tv"),
    "appliances": ("أجهزة منزلية", "home appliances"),
    "perfumes": ("عطور", "perfume"),
    "supermarket": ("سوبر ماركت", "grocery"),
    "gaming": ("ألعاب فيديو", "video games"),
    "tablets": ("تابلت", "tablets"),
    "fashion": ("ملابس", "clothing"),
}
HOSTS = {"noon": {"noon.com", "www.noon.com"}, "amazon": {"amazon.eg", "www.amazon.eg"}}
NOON_SEARCH_API = "https://www.noon.com/_vs/nc/mp-customer-catalog-api/api/v3/u/search"
NOON_PAGE_SIZE = 20
NOON_COUNTRY = "eg"
NOON_LANGUAGE = "ar"
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

_REGION_KEYS = {"country", "country_code", "countrycode", "market", "marketplace", "region", "locale"}
_CURRENCY_KEYS = {"currency", "currency_code", "currencycode", "price_currency", "pricecurrency"}


def search_pages() -> list[tuple[str, str, str]]:
    pages = []
    for category, (noon_query, amazon_query) in QUERIES.items():
        pages.extend([
            ("noon", category, NOON_SEARCH_API + "?" + urlencode({
                "q": noon_query, "limit": NOON_PAGE_SIZE,
                "country": NOON_COUNTRY, "language": NOON_LANGUAGE,
            })),
            ("amazon", category, "https://www.amazon.eg/s?k=" + quote_plus(amazon_query) + "&language=ar_AE"),
        ])
    return pages


def parse_price(value: str | None) -> float | None:
    if not value:
        return None
    text = value.translate(ARABIC_DIGITS).replace("٬", ",").replace("٫", ".")
    match = re.search(r"\d[\d\s,.]*", text)
    if not match:
        return None
    number = re.sub(r"\s", "", match.group()).strip(".,")
    if "," in number and "." in number:
        number = number.replace(",", "")
    elif "," in number:
        left, right = number.rsplit(",", 1)
        number = f"{left.replace(',', '')}.{right}" if len(right) in (1, 2) else number.replace(",", "")
    try:
        price = float(number)
    except ValueError:
        return None
    return price if math.isfinite(price) and price > 0 else None


def _text(node: Tag | None) -> str:
    return " ".join(node.stripped_strings) if node else ""


def _first_price(card: Tag, selectors: tuple[str, ...]) -> float | None:
    for selector in selectors:
        for node in card.select(selector):
            price = parse_price(_text(node))
            if price is not None:
                return price
    return None


def _product_url(store: str, href: str, base: str) -> str | None:
    parsed = urlsplit(urljoin(base, href))
    if parsed.scheme != "https" or parsed.hostname not in HOSTS[store]:
        return None
    if store == "amazon":
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", parsed.path, re.I)
        if match:
            return "https://www.amazon.eg/dp/" + match.group(1).upper()
    query = [
        (k, v) for k, v in parse_qsl(parsed.query)
        if not k.lower().startswith("utm_") and k.lower() not in {"ref", "nav_ctx"}
    ]
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/") or "/", urlencode(query), ""))


def _id(store: str, url: str, asin: str = "") -> str:
    if store == "amazon" and asin:
        return "amazon-" + asin.upper()
    parts = [part for part in urlsplit(url).path.split("/") if part]
    product_marker = next((i for i in range(len(parts) - 1, -1, -1) if parts[i].lower() == "p"), None)
    if product_marker is not None:
        key = parts[product_marker + 1] if product_marker + 1 < len(parts) else parts[product_marker - 1] if product_marker else ""
    else:
        key = parts[-1] if parts else ""
    key = re.sub(r"[^A-Za-z0-9_-]+", "-", key).strip("-") or hashlib.sha256(url.encode()).hexdigest()[:16]
    return "noon-" + key[:100]


def _image(card: Tag, base: str) -> str | None:
    node = card.select_one("img[src], img[data-src], img[data-lazy-src]")
    src = (node.get("data-src") or node.get("data-lazy-src") or node.get("src")) if node else None
    if not isinstance(src, str) or src.startswith("data:"):
        return None
    result = urljoin(base, src)
    return result if urlsplit(result).scheme == "https" else None


def _noon_cards(soup: BeautifulSoup) -> list[Tag]:
    cards = soup.select("[data-qa='product-block'], [data-qa='product-card'], [data-testid*='product']")
    if cards:
        return cards
    found, seen = [], set()
    for anchor in soup.select("a[href*='/p/']"):
        node: Tag | None = anchor
        for _ in range(8):
            if not isinstance(node, Tag) or not isinstance(node.parent, Tag):
                break
            node = node.parent
            text = _text(node)
            if node.select_one("img") and len(text) < 1800 and (
                re.search(r"(?:EGP|جنيه|ج\.م)", text, re.I) or node.select("[class*='price'], [data-qa*='price']")
            ):
                if id(node) not in seen:
                    found.append(node)
                    seen.add(id(node))
                break
    return found


def _amazon_out_of_stock(card: Tag) -> bool:
    text = _text(card).casefold()
    unavailable_phrases = (
        "out of stock", "currently unavailable", "temporarily unavailable",
        "sold out", "not available", "غير متوفر", "غير متوفرة", "غير متاح",
        "غير متاحة", "نفد من المخزون", "نفدت الكمية",
    )
    if any(phrase in text for phrase in unavailable_phrases):
        return True

    unavailable_schema = ("outofstock", "out_of_stock", "soldout", "sold_out", "discontinued", "unavailable")
    for node in card.select('[itemprop="availability"], [data-availability]'):
        values = [node.get("content"), node.get("href"), node.get("data-availability"), _text(node)]
        for value in values:
            if isinstance(value, str):
                normalized = re.sub(r"[^a-z]", "", value.casefold())
                if any(marker.replace("_", "") in normalized for marker in unavailable_schema):
                    return True
    return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, str):
        return parse_price(value)
    return None


def _market_values(value: Any) -> tuple[set[str], set[str]]:
    regions: set[str] = set()
    currencies: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z_]", "", str(key).lower())
            if normalized_key in _REGION_KEYS and isinstance(item, str):
                regions.add(item.strip().casefold().replace("_", "-"))
            elif normalized_key in _CURRENCY_KEYS and isinstance(item, str):
                currencies.add(item.strip().casefold())
            child_regions, child_currencies = _market_values(item)
            regions.update(child_regions)
            currencies.update(child_currencies)
    elif isinstance(value, list):
        for item in value:
            child_regions, child_currencies = _market_values(item)
            regions.update(child_regions)
            currencies.update(child_currencies)
    return regions, currencies


def _is_egypt_egp(payload: dict[str, Any]) -> bool:
    metadata = {key: value for key, value in payload.items() if key != "hits"}
    regions, currencies = _market_values(metadata)
    egypt = {"eg", "egypt", "egypt-ar", "ar-eg", "en-eg", "egypt-en"}
    egp = {"egp", "egyptian pound", "egyptian pounds", "جنيه مصري"}
    return bool(regions & egypt) and bool(currencies & egp) and regions <= egypt and currencies <= egp


def _hit_market_matches_egypt(hit: dict[str, Any]) -> bool:
    regions, currencies = _market_values(hit)
    egypt = {"eg", "egypt", "egypt-ar", "ar-eg", "en-eg", "egypt-en"}
    egp = {"egp", "egyptian pound", "egyptian pounds", "جنيه مصري"}
    return not (regions - egypt or currencies - egp)


def _page_is_egypt_market(store: str, page_url: str) -> bool:
    parsed = urlsplit(page_url)
    if parsed.hostname not in HOSTS.get(store, set()):
        return False
    if store == "amazon":
        return True
    query = parse_qs(parsed.query)
    countries = {value.strip().casefold() for value in query.get("country", [])}
    path_parts = {part.casefold() for part in parsed.path.split("/") if part}
    return bool(countries & {"eg", "egypt"}) or "egypt-ar" in path_parts


def _schema_types(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {
        re.split(r"[/#]", item.strip())[-1].casefold()
        for item in values
        if isinstance(item, str) and item.strip()
    }


def _jsonld_products(soup: BeautifulSoup, store: str, category: str, page_url: str) -> list[dict[str, Any]]:
    if not _page_is_egypt_market(store, page_url):
        return []

    product_nodes: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if "product" in _schema_types(value.get("@type")):
                product_nodes.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue

    products: dict[str, dict[str, Any]] = {}
    for node in product_nodes:
        name = node.get("name")
        raw_url = node.get("url")
        if not isinstance(name, str) or not name.strip() or not isinstance(raw_url, str):
            continue
        url = _product_url(store, raw_url, page_url)
        if not url:
            continue

        raw_offers = node.get("offers")
        offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            offer_types = _schema_types(offer.get("@type"))
            if not offer_types & {"offer", "aggregateoffer"}:
                continue
            currency = offer.get("priceCurrency")
            if not isinstance(currency, str) or currency.strip().upper() != "EGP":
                continue
            availability = offer.get("availability")
            availability_values = availability if isinstance(availability, list) else [availability]
            if any(
                marker in "".join(re.findall(r"[a-z]+", value.casefold()))
                for value in availability_values
                if isinstance(value, str)
                for marker in ("outofstock", "soldout", "discontinued")
            ):
                continue
            raw_price = offer.get("price")
            if raw_price is None and "aggregateoffer" in offer_types:
                raw_price = offer.get("lowPrice")
            current = _number(raw_price)
            if current is None:
                continue

            asin = ""
            if store == "amazon":
                match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", urlsplit(url).path, re.I)
                if not match:
                    continue
                asin = match.group(1).upper()
            product_id = _id(store, url, asin)

            image_value = node.get("image")
            if isinstance(image_value, list):
                image_value = next((item for item in image_value if isinstance(item, str)), None)
            if isinstance(image_value, dict):
                image_value = image_value.get("url")
            image = None
            if isinstance(image_value, str) and not image_value.startswith("data:"):
                candidate = urljoin(page_url, image_value)
                if urlsplit(candidate).scheme == "https":
                    image = candidate

            seller_value = offer.get("seller") or node.get("seller")
            if isinstance(seller_value, dict):
                seller_value = seller_value.get("name")
            seller = seller_value if isinstance(seller_value, str) and len(seller_value) <= 120 else None
            products.setdefault(product_id, {
                "id": product_id, "store": store, "title": name.strip()[:500], "url": url,
                "image": image, "current_price": current, "list_price": None,
                "seller": seller, "category": category,
            })
            break
    return list(products.values())


def _noon_json_products(payload: dict[str, Any], category: str, page_url: str) -> list[dict[str, Any]]:
    hits = payload.get("hits")
    if not isinstance(hits, list) or not _is_egypt_egp(payload):
        return []
    products: dict[str, dict[str, Any]] = {}
    for hit in hits:
        if (
            not isinstance(hit, dict) or hit.get("is_buyable") is False
            or not _hit_market_matches_egypt(hit)
        ):
            continue
        stock = hit.get("stock")
        if isinstance(stock, (int, float)) and not isinstance(stock, bool):
            if not math.isfinite(stock) or stock <= 0:
                continue
        if isinstance(stock, str) and stock.strip():
            try:
                numeric_stock = float(stock)
                if not math.isfinite(numeric_stock) or numeric_stock <= 0:
                    continue
            except ValueError:
                pass

        product_url = hit.get("product_url")
        pdp_url = product_url.get("pdp_url") if isinstance(product_url, dict) else None
        if not isinstance(pdp_url, str):
            pdp_url = hit.get("pdp_url")
        url = _product_url("noon", pdp_url, page_url) if isinstance(pdp_url, str) else None
        sku = hit.get("sku")
        if not url and isinstance(sku, str) and isinstance(product_url, dict):
            slug = product_url.get("url")
            if isinstance(slug, str) and slug.strip("/"):
                path = "/".join(quote(part, safe="-_") for part in slug.strip("/").split("/"))
                path += "/" + quote(sku, safe="-_") + "/p/"
                url = _product_url("noon", "/" + path, page_url)
        if not url:
            continue

        title = hit.get("name")
        if not isinstance(title, str) or not title.strip():
            continue
        price = _number(hit.get("price"))
        sale_price = _number(hit.get("sale_price"))
        current = sale_price if sale_price is not None and (price is None or sale_price <= price) else price
        if current is None:
            continue
        list_price = price if price is not None and price > current else None

        seller_value = hit.get("seller")
        if isinstance(seller_value, dict):
            seller_value = seller_value.get("store_name") or seller_value.get("name")
        seller = seller_value if isinstance(seller_value, str) and len(seller_value) <= 120 else None
        if seller is None and isinstance(hit.get("store_name"), str):
            seller = hit["store_name"][:120]
        image_value = hit.get("image_url") or hit.get("image")
        image = None
        if isinstance(image_value, str) and not image_value.startswith("data:"):
            image_url = urljoin(page_url, image_value)
            if urlsplit(image_url).scheme == "https":
                image = image_url

        product_id = _id("noon", url)
        products.setdefault(product_id, {
            "id": product_id, "store": "noon", "title": title.strip()[:500], "url": url,
            "image": image, "current_price": current, "list_price": list_price,
            "seller": seller, "category": category,
        })
    return list(products.values())


def parse_search_page(store: str, category: str, page_url: str, html: str) -> list[dict[str, Any]]:
    if store == "noon":
        try:
            payload = json.loads(html)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            return _noon_json_products(payload, category, page_url)
    soup = BeautifulSoup(html, "html.parser")
    if store == "amazon":
        cards = soup.select('[data-component-type="s-search-result"][data-asin]')
    elif store == "noon":
        cards = _noon_cards(soup)
    else:
        raise ValueError(f"Unsupported store: {store}")
    products = {}
    for card in cards:
        if store == "amazon":
            if _amazon_out_of_stock(card):
                continue
            asin = str(card.get("data-asin", "")).strip()
            link = next((a for a in card.select("a[href*='/dp/'], a[href*='/gp/product/']") if _text(a)), None)
            if link is None:
                link = card.select_one("h2 a[href]")
            if not asin or not link:
                continue
            url = _product_url(store, str(link.get("href", "")), page_url)
            title = _text(link) or str(card.select_one("h2").get("aria-label", "") if card.select_one("h2") else "")
            current = _first_price(card, (".a-price:not(.a-text-price) .a-offscreen", ".a-price:not(.a-text-price)"))
            list_price = _first_price(card, (".a-text-price .a-offscreen", "[data-a-strike='true'] .a-offscreen"))
            product_id = _id(store, url or "", asin)
        else:
            link = card.select_one("a[href*='/p/']")
            if not link:
                continue
            url = _product_url(store, str(link.get("href", "")), page_url)
            name = card.select_one("[data-qa='product-name'], [data-qa*='product-name'], h2, h3")
            title = _text(name) or str(link.get("title", "")).strip()
            if not title:
                image = card.select_one("img[alt]")
                title = str(image.get("alt", "")).strip() if image else ""
            current = _first_price(card, ("[data-qa='price']", "[data-qa*='price-now']", "[class*='priceNow']", "[class*='price-now']", "[class*='priceCurrent']", "[class*='price-current']"))
            list_price = _first_price(card, ("del", "s", "[class*='was']", "[class*='old-price']", "[class*='priceBefore']", "[class*='price-before']", "[class*='strike']"))
            if current is None:
                for node in card.select("[class*='price'], [data-qa*='price']"):
                    current = parse_price(_text(node))
                    if current and current != list_price:
                        break
            product_id = _id(store, url or "")
        if not url or not title or current is None:
            continue
        seller_node = card.select_one("[data-qa*='seller'], [class*='seller'], a[href*='/seller/']")
        seller = _text(seller_node) or None
        products.setdefault(product_id, {
            "id": product_id, "store": store, "title": title[:500], "url": url,
            "image": _image(card, page_url), "current_price": current,
            "list_price": list_price if list_price and list_price > current else None,
            "seller": seller if seller and len(seller) <= 120 else None, "category": category,
        })
    return list(products.values()) or _jsonld_products(soup, store, category, page_url)


class StoreScraper:
    def __init__(self, delay: float = 2, retries: int = 2, timeout: float = 12):
        self.delay, self.retries, self.timeout = max(0, delay), max(0, retries), timeout
        self.last_request = 0.0
        self.session = httpx.Client(
            http2=True,
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
                "DNT": "1",
            },
        )

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        delay = random.uniform(0, min(30.0, float(2 ** attempt)))
        if response is not None:
            retry_after = self._retry_after(response)
            if retry_after is not None:
                delay = max(delay, retry_after)
        time.sleep(delay)

    def fetch(self, store: str, url: str) -> tuple[str | None, str | None]:
        for attempt in range(self.retries + 1):
            wait = self.delay - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            headers = None
            if store == "noon":
                headers = {
                    "Accept": "application/json",
                    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
                    "Referer": "https://www.noon.com/egypt-ar/",
                    "x-locale": "ar-eg",
                }
            try:
                response = self.session.get(url, timeout=self.timeout, headers=headers)
            except httpx.RequestError as exc:
                if attempt < self.retries:
                    LOG.warning("Retrying %s after %s", url, exc.__class__.__name__)
                    self._backoff(attempt)
                    continue
                return None, f"request error: {exc.__class__.__name__}"
            if urlsplit(str(response.url)).hostname not in HOSTS[store]:
                return None, "redirected outside the store domain"
            if response.status_code in (401, 403):
                return None, f"HTTP {response.status_code} (no retry)"
            if response.status_code in (408, 429) or response.status_code >= 500:
                if attempt < self.retries:
                    LOG.warning("Retrying %s after HTTP %d", url, response.status_code)
                    self._backoff(attempt, response)
                    continue
            if response.status_code != 200:
                return None, f"HTTP {response.status_code}"
            body = response.text
            lower = body[:250_000].lower()
            if any(marker in lower for marker in ("robot check", "captcha", "verify you are human", "automated access")):
                return None, "challenge page (not bypassed)"
            if store == "noon":
                try:
                    payload = response.json()
                except ValueError:
                    content_type = response.headers.get("content-type", "").lower()
                    is_html = "html" in content_type or "xhtml+xml" in content_type
                    if not is_html:
                        return None, "non-JSON response (not parsed)"
                    if not _page_is_egypt_market(store, str(response.url)):
                        return None, "Noon HTML page is not explicitly Egypt-scoped"
                    return body, None
                if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
                    return None, "unexpected Noon JSON response"
                if not _is_egypt_egp(payload):
                    return None, "Noon JSON lacks verified Egypt/EGP market fields"
            elif len(response.content) < 1000:
                return None, f"short page ({len(response.content)} bytes)"
            return body, None
        return None, "retry limit reached"


def _previous() -> tuple[dict[str, dict[str, Any]], str | None]:
    if not PRODUCTS_FILE.exists():
        return {}, None
    try:
        payload = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read existing {PRODUCTS_FILE}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise RuntimeError(f"Invalid products file: {PRODUCTS_FILE}")
    updated_at = payload.get("updated_at")
    previous = {}
    for product in payload["products"]:
        if not isinstance(product, dict) or not isinstance(product.get("id"), str) or not product["id"]:
            raise RuntimeError(f"Invalid product entry in {PRODUCTS_FILE}")
        if product["id"] in previous:
            raise RuntimeError(f"Duplicate product id in {PRODUCTS_FILE}: {product['id']}")
        previous[product["id"]] = product
    return previous, updated_at if isinstance(updated_at, str) else None


def _histories() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for store in HOSTS:
        path = PRICE_DIR / f"{store}.jsonl"
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row.get("store") != store:
                        raise ValueError(f"invalid record at line {number}")
                    date, price = row.get("date"), row.get("price")
                    if (not isinstance(date, str) or _datetime(date) == datetime.min.replace(tzinfo=timezone.utc)
                            or isinstance(price, bool) or not isinstance(price, (int, float))
                            or not math.isfinite(price) or price <= 0):
                        raise ValueError(f"invalid date/price at line {number}")
                    result[row["id"]].append({"date": date, "price": float(price)})
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Cannot safely read {path}: {exc}") from exc
    for rows in result.values():
        rows.sort(key=lambda row: row["date"])
    return result


def _score(history: list[dict[str, Any]], price: float, list_price: float | None, now: datetime) -> dict[str, Any]:
    try:
        from scraper.scoring import score_product
    except ModuleNotFoundError as exc:
        if exc.name != "scraper.scoring":
            raise
        if len(history) <= 2:
            return {"status": "new", "discount_percent": 0.0, "lowest_days": None}
        raise RuntimeError("scraper.scoring.score_product is required once history has more than two observations")
    return score_product(history=history, current_price=price, list_price=list_price, now=now)


def _valid(item: dict[str, Any]) -> None:
    store, category = item.get("store"), item.get("category")
    if store not in HOSTS or category not in QUERIES:
        raise ValueError(f"invalid store/category for {item.get('id')}")
    if not isinstance(item.get("id"), str) or not item["id"] or not isinstance(item.get("title"), str) or not item["title"]:
        raise ValueError("product id/title must be non-empty")
    parsed = urlsplit(str(item.get("url", "")))
    if parsed.scheme != "https" or parsed.hostname not in HOSTS[store]:
        raise ValueError(f"unsafe URL for {item['id']}")
    price = item.get("current_price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise ValueError(f"invalid current price for {item['id']}")
    list_price = item.get("list_price")
    if list_price is not None and (
        isinstance(list_price, bool) or not isinstance(list_price, (int, float))
        or not math.isfinite(list_price) or list_price <= 0
    ):
        raise ValueError(f"invalid list price for {item['id']}")
    for field in ("image", "seller"):
        value = item.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"invalid {field} for {item['id']}")
    if item.get("image") is not None and urlsplit(item["image"]).scheme != "https":
        raise ValueError(f"unsafe image URL for {item['id']}")
    history = item.get("history")
    if not isinstance(history, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("date"), str)
        or _datetime(row.get("date", "")) == datetime.min.replace(tzinfo=timezone.utc)
        or isinstance(row.get("price"), bool) or not isinstance(row.get("price"), (int, float))
        or not math.isfinite(row["price"]) or row["price"] <= 0
        for row in history
    ):
        raise ValueError(f"invalid history for {item['id']}")
    score = item.get("score")
    if not isinstance(score, dict) or score.get("status") not in {"new", "real", "fake", "none"}:
        raise ValueError(f"invalid score for {item['id']}")
    discount = score.get("discount_percent")
    lowest_days = score.get("lowest_days")
    if isinstance(discount, bool) or not isinstance(discount, (int, float)) or not math.isfinite(discount):
        raise ValueError(f"invalid discount score for {item['id']}")
    if lowest_days is not None and (isinstance(lowest_days, bool) or not isinstance(lowest_days, int) or lowest_days < 0):
        raise ValueError(f"invalid lowest-days score for {item['id']}")


def _write_products(
    products: list[dict[str, Any]],
    updated_at: str | None,
    blocked_pages: list[dict[str, str]],
    unavailable_pages: list[dict[str, str]],
) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": updated_at,
        "blocked_pages": blocked_pages,
        "unavailable_pages": unavailable_pages,
        "products": products,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd, temp = tempfile.mkstemp(prefix="products-", suffix=".json", dir=DATA)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, PRODUCTS_FILE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def run(delay: float | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.isoformat().replace("+00:00", "Z")
    previous, previous_updated_at = _previous()
    histories = _histories()
    recovered: dict[str, list[dict[str, Any]]] = {store: [] for store in HOSTS}
    for product_id, item in previous.items():
        store = item.get("store")
        if store not in HOSTS:
            continue
        stored = {(row["date"], row["price"]) for row in histories.get(product_id, [])}
        for row in item.get("history", []):
            if not isinstance(row, dict) or not isinstance(row.get("date"), str):
                raise RuntimeError(f"Invalid saved product history for {product_id}")
            price = row.get("price")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                raise RuntimeError(f"Invalid saved product history for {product_id}")
            if _datetime(row["date"]) == datetime.min.replace(tzinfo=timezone.utc):
                raise RuntimeError(f"Invalid saved history date for {product_id}")
            pair = (row["date"], float(price))
            if pair not in stored:
                histories.setdefault(product_id, []).append({"date": pair[0], "price": pair[1]})
                recovered[store].append({"id": product_id, "store": store, "date": pair[0], "price": pair[1]})
                stored.add(pair)
    candidates: dict[str, dict[str, Any]] = {}
    report = {store: {"pages": 0, "ok": 0, "blocked": 0, "products": 0} for store in HOSTS}
    blocked_pages: list[dict[str, str]] = []
    unavailable_pages: list[dict[str, str]] = []
    scraper = StoreScraper(
        delay if delay is not None else float(os.getenv("KHASM_REQUEST_DELAY", "2")),
        retries=int(os.getenv("KHASM_RETRIES", "2")),
        timeout=float(os.getenv("KHASM_HTTP_TIMEOUT", "12")),
    )
    for store, category, url in search_pages():
        report[store]["pages"] += 1
        html, error = scraper.fetch(store, url)
        if error:
            report[store]["blocked"] += 1
            blocked_pages.append({"store": store, "category": category, "url": url, "reason": error})
            LOG.warning("Blocked [%s/%s] %s: %s", store, category, url, error)
            continue
        found = parse_search_page(store, category, url, html or "")
        if found:
            report[store]["ok"] += 1
        else:
            unavailable_pages.append({
                "store": store,
                "category": category,
                "url": url,
                "reason": "no products parsed",
            })
            LOG.warning("No products parsed [%s/%s] %s", store, category, url)
        report[store]["products"] += len(found)
        for item in found:
            if item["id"] not in candidates:
                candidates[item["id"]] = item
            else:
                old = candidates[item["id"]]
                for field in ("title", "url", "image", "current_price", "list_price", "seller"):
                    if item.get(field) is not None:
                        old[field] = item[field]

    new_rows: dict[str, list[dict[str, Any]]] = {store: list(rows) for store, rows in recovered.items()}
    old_pairs = {pid: {(r["date"], r["price"]) for r in rows} for pid, rows in histories.items()}
    for item in candidates.values():
        row = {"id": item["id"], "store": item["store"], "date": stamp, "price": item["current_price"]}
        if (stamp, float(row["price"])) not in old_pairs.get(item["id"], set()):
            new_rows[item["store"]].append(row)
            histories.setdefault(item["id"], []).append({"date": stamp, "price": float(row["price"])})
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    for store, rows in new_rows.items():
        if rows:
            with (PRICE_DIR / f"{store}.jsonl").open("a", encoding="utf-8") as output:
                for row in rows:
                    output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    merged = {**previous, **candidates}
    cutoff = now - timedelta(days=30)
    products = []
    for pid, item in merged.items():
        rows = histories.get(pid, [])
        if not rows and isinstance(item.get("history"), list):
            rows = [r for r in item["history"] if isinstance(r, dict) and isinstance(r.get("date"), str) and isinstance(r.get("price"), (int, float))]
            histories[pid] = rows
        if rows:
            rows.sort(key=lambda r: r["date"])
            item["current_price"] = rows[-1]["price"]
            item["history"] = [r for r in rows if _datetime(r["date"]) >= cutoff]
            item["score"] = _score(
                rows,
                float(item["current_price"]),
                item.get("list_price"),
                _datetime(rows[-1]["date"]),
            )
        else:
            item["history"] = []
            item["score"] = {"status": "new", "discount_percent": 0.0, "lowest_days": None}
        _valid(item)
        products.append(item)
    products.sort(key=lambda p: (p["store"], p["category"], p["title"].casefold(), p["id"]))
    updated_at = stamp if candidates else previous_updated_at
    _write_products(products, updated_at, blocked_pages, unavailable_pages)
    for store in report:
        LOG.info("%s: %d/%d parsed, %d blocked, %d products seen", store, report[store]["ok"], report[store]["pages"], report[store]["blocked"], report[store]["products"])
    report.update({
        "total_unique": len(candidates),
        "retained": len(set(previous) - set(candidates)),
        "history_rows_appended": sum(map(len, new_rows.values())),
        "blocked_pages": blocked_pages,
        "unavailable_pages": unavailable_pages,
        "products_file": str(PRODUCTS_FILE.relative_to(ROOT)),
    })
    return report


def main() -> int:
    logging.basicConfig(level=os.getenv("KHASM_LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        result = run()
    except Exception:
        LOG.exception("Scrape run failed; existing data was not intentionally removed")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
