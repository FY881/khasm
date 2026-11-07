"""Bounded r.jina.ai Reader fallback for public Noon Egypt search pages."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import httpx
from bs4 import BeautifulSoup

from scraper import scrape

READER_ROOT = "https://r.jina.ai/"
NOON_HOSTS = {"noon.com", "www.noon.com"}
READER_HEADERS = {
    "Accept": "application/json",
    "X-Respond-With": "html",
    "X-No-Cache": "true",
}
MAX_READER_RESPONSE_BYTES = 4 * 1024 * 1024
EGP_MARKER = re.compile(r"\bEGP\b|جنيه(?:\s+مصري)?|ج\s*\.?\s*م", re.I)
OTHER_CURRENCY = re.compile(
    r"\b(?:AED|SAR|KWD|BHD|QAR|OMR|USD|EUR|GBP)\b|د\s*\.?\s*إ|ريال(?:\s+سعودي)?|درهم(?:\s+إماراتي)?",
    re.I,
)
CHALLENGE_MARKERS = (
    "captcha", "robot check", "verify you are human", "automated access", "access denied",
)
UNAVAILABLE_TEXT = re.compile(
    r"\b(?:out\s+of\s+stock|sold\s+out|unavailable|not\s+available)\b"
    r"|غير\s+متوفر(?:ة|ين)?|غير\s+متاح(?:ة|ين)?|نفد\s+المخزون|نفدت\s+الكمية|نفد\s+الكمية|تم\s+نفاد\s+الكمية",
    re.I,
)
UNAVAILABLE_MARKER = re.compile(r"outofstock|soldout|unavailable|notavailable|نفد|غيرمتوفر|غيرمتاح", re.I)
REGION_QUERY_KEYS = {"country", "countrycode", "market", "marketplace", "region", "locale"}
CURRENCY_QUERY_KEYS = {"currency", "currencycode"}
EGYPT_QUERY_VALUES = {"eg", "egypt", "egypt-ar", "ar-eg", "en-eg", "egypt-en"}
EGP_QUERY_VALUES = {"egp", "egyptian pound", "egyptian pounds", "جنيه مصري"}
PRODUCT_CARD_SELECTORS = (
    "[data-qa='plp-product-box'], [data-qa='product-block'], [data-qa='product-card']"
)
PRICE_SELECTORS = (
    "[data-qa='plp-product-box-price'], [data-qa='price'], [data-qa*='price-now'], "
    "[class*='priceNow'], [class*='price-now'], [class*='priceCurrent'], [class*='price-current']"
)


def noon_search_url(category: str) -> str:
    """Return the category's Noon Egypt HTML search page, never an arbitrary URL."""
    try:
        query = scrape.QUERIES[category][0]
    except KeyError as exc:
        raise ValueError(f"unsupported Noon category: {category}") from exc
    return "https://www.noon.com/egypt-ar/search/?" + urlencode({"q": query})


def is_noon_egypt_search_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in NOON_HOSTS
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == "/egypt-ar/search"
        and len(query.get("q", [])) == 1
        and bool(query["q"][0].strip())
    )


def is_explicit_access_denial(error: str | None) -> bool:
    if not error:
        return False
    value = error.casefold()
    return (
        any(re.search(rf"\b{status}\b", value) for status in (401, 403, 429))
        or any(marker in value for marker in CHALLENGE_MARKERS)
        or "challenge" in value
        or "redirected outside the store domain" in value
    )


def _is_challenge(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    text = (soup.title.get_text(" ", strip=True) if soup.title else "") + " " + soup.get_text(" ", strip=True)
    text = text[:20_000].casefold()
    return any(marker in text for marker in CHALLENGE_MARKERS) or "challenge" in text


def _egypt_final_url(url: str) -> bool:
    if not is_noon_egypt_search_url(url):
        return False
    parsed = urlsplit(url)
    if parsed.path.rstrip("/") != "/egypt-ar/search":
        return False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = re.sub(r"[^a-z]", "", key.casefold())
        normalized_value = value.strip().casefold().replace("_", "-")
        is_region = normalized_key in REGION_QUERY_KEYS or normalized_key.endswith(
            ("country", "countrycode", "market", "marketplace", "region", "locale")
        )
        is_currency = normalized_key in CURRENCY_QUERY_KEYS or normalized_key.endswith(("currency", "currencycode"))
        if is_region and normalized_value not in EGYPT_QUERY_VALUES:
            return False
        if is_currency and value.strip().casefold() not in EGP_QUERY_VALUES:
            return False
    return True


def _same_search_query(source_url: str, final_url: str) -> bool:
    return parse_qs(urlsplit(source_url).query).get("q") == parse_qs(urlsplit(final_url).query).get("q")


def _is_unavailable(card: Any) -> bool:
    text = scrape._text(card).casefold()
    if UNAVAILABLE_TEXT.search(text):
        return True
    for node in (card, *card.find_all(True)):
        for key, value in node.attrs.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                value_text = str(item).casefold()
                if UNAVAILABLE_MARKER.search(value_text):
                    return True
                normalized_key = re.sub(r"[^a-z]", "", key.casefold())
                if normalized_key in {"isbuyable", "buyable", "available", "isavailable"} and value_text in {"false", "0", "no"}:
                    return True
    return False


def _schema_unavailable_ids(soup: BeautifulSoup, page_url: str) -> set[str]:
    unavailable: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            buyable = value.get("isBuyable", value.get("is_buyable"))
            unavailable_offer = buyable is False or (isinstance(buyable, str) and buyable.casefold() in {"false", "0", "no"})
            availability = value.get("availability")
            availability_values = availability if isinstance(availability, list) else [availability]
            unavailable_offer = unavailable_offer or any(
                isinstance(item, str) and bool(UNAVAILABLE_MARKER.search(item))
                for item in availability_values
            )
            raw_url = value.get("url") or value.get("@id")
            if unavailable_offer and isinstance(raw_url, str):
                url = scrape._product_url("noon", raw_url, page_url)
                if url:
                    unavailable.add(scrape._id("noon", url))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    return unavailable


def _parse_products(html: str, category: str, page_url: str) -> tuple[list[dict[str, Any]], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    products: dict[str, dict[str, Any]] = {}
    saw_foreign_currency = False
    unavailable_ids = _schema_unavailable_ids(soup, page_url)
    for card in soup.select(PRODUCT_CARD_SELECTORS):
        if _is_unavailable(card):
            continue
        price_node = card.select_one(PRICE_SELECTORS)
        if price_node is None:
            continue
        price_text = scrape._text(price_node)
        has_egp = bool(EGP_MARKER.search(price_text))
        has_other = bool(OTHER_CURRENCY.search(price_text))
        if has_other:
            saw_foreign_currency = True
            continue
        if not has_egp:
            continue

        link = card.select_one("a[href*='/p/']")
        name = card.select_one("[data-qa='plp-product-box-name'], h2, h3")
        if link is None or name is None:
            continue
        url = scrape._product_url("noon", str(link.get("href", "")), page_url)
        title = scrape._text(name)
        amount = price_node.select_one(
            "[class*='sellingPrice'] [class*='amount'], [class*='priceNow'], "
            "[class*='price-now'], [data-qa*='price-now'], strong"
        )
        current = scrape.parse_price(scrape._text(amount)) if amount else None
        if not url or not title or current is None:
            continue
        old_price_node = price_node.select_one("[class*='oldPrice'], del, s, [class*='strike']")
        old_price = scrape.parse_price(scrape._text(old_price_node)) if old_price_node else None
        product_id = scrape._id("noon", url)
        if product_id in unavailable_ids:
            continue
        products.setdefault(product_id, {
            "id": product_id,
            "store": "noon",
            "title": title[:500],
            "url": url,
            "image": scrape._image(card, page_url),
            "current_price": current,
            "list_price": old_price if old_price is not None and old_price > current else None,
            "seller": None,
            "category": category,
        })

    if saw_foreign_currency:
        return [], "Reader product currency mismatch"
    return list(products.values()), None


class NoonReader:
    """Make at most one gated Reader request and accept only current Egypt/EGP products."""

    def __init__(self, client: Any = None, timeout: float = 20):
        if timeout <= 0:
            raise ValueError("Reader timeout must be positive")
        self.client = client or httpx
        self.timeout = timeout

    def fetch(
        self,
        source_url: str,
        category: str,
        *,
        gate: Any,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        if category not in scrape.QUERIES:
            return None, "Reader category is unsupported"
        if not is_noon_egypt_search_url(source_url):
            return None, "Reader source is not a Noon Egypt search page"
        if not gate.wait(deadline) or clock() >= deadline:
            return None, None
        gate.mark_started()
        remaining = deadline - clock()
        timeout = min(self.timeout, remaining)
        if timeout <= 0:
            return None, None

        try:
            response = self.client.get(
                READER_ROOT + source_url,
                headers=READER_HEADERS,
                timeout=timeout,
            )
        except httpx.RequestError as exc:
            return None, f"Reader request error: {exc.__class__.__name__} (no retry)"

        if response.status_code != 200:
            return None, f"Reader HTTP {response.status_code} (no retry)"
        headers = getattr(response, "headers", None) or {}
        for name in ("content-length", "x-decompressed-content-length"):
            raw_size = headers.get(name)
            if raw_size:
                try:
                    if int(raw_size) > MAX_READER_RESPONSE_BYTES:
                        return None, "Reader response exceeds size limit"
                except (TypeError, ValueError):
                    pass
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)) and len(content) > MAX_READER_RESPONSE_BYTES:
            return None, "Reader response exceeds size limit"
        try:
            payload = response.json()
        except (ValueError, TypeError):
            if _is_challenge(getattr(response, "text", "")):
                return None, "Reader challenge page (not bypassed)"
            return None, "Reader returned non-JSON response"
        if not isinstance(payload, dict):
            return None, "Reader returned invalid JSON"
        service_status = payload.get("code")
        if service_status is not None and (type(service_status) is not int or service_status != 200):
            return None, f"Reader service HTTP {service_status} (no retry)"
        data = payload.get("data")
        if not isinstance(data, dict):
            return None, "Reader response lacks page data"
        origin_status = data.get("httpStatus")
        if type(origin_status) is not int or origin_status != 200:
            return None, f"Reader origin HTTP {origin_status} (no retry)"
        final_url = data.get("finalUrl")
        if (
            not isinstance(final_url, str)
            or not _egypt_final_url(final_url)
            or not _same_search_query(source_url, final_url)
        ):
            return None, "Reader final URL does not match Noon Egypt search"
        html = data.get("html")
        if not isinstance(html, str) or not html.strip():
            return None, "Reader returned empty HTML"
        if len(html.encode("utf-8")) > MAX_READER_RESPONSE_BYTES:
            return None, "Reader HTML exceeds size limit"
        if clock() >= deadline:
            return None, "Reader request exceeded deadline"
        if _is_challenge(html):
            return None, "Reader challenge page (not bypassed)"

        products, error = _parse_products(html, category, final_url)
        if error:
            return None, error
        if not products:
            return None, "Reader returned no valid Noon Egypt EGP products"
        return products, None
