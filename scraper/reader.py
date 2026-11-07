"""Bounded r.jina.ai Reader fallback for public Noon Egypt search pages."""
from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

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
EGP_MARKER = re.compile(r"\bEGP\b|جنيه(?:\s+مصري)?|ج\s*\.?\s*م", re.I)
OTHER_CURRENCY = re.compile(
    r"\b(?:AED|SAR|KWD|BHD|QAR|OMR|USD|EUR|GBP)\b|د\s*\.?\s*إ|ريال(?:\s+سعودي)?|درهم(?:\s+إماراتي)?",
    re.I,
)
CHALLENGE_MARKERS = (
    "captcha", "robot check", "verify you are human", "automated access", "access denied",
)
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
    return parsed.path.rstrip("/") == "/egypt-ar/search"


def _parse_products(html: str, category: str, page_url: str) -> tuple[list[dict[str, Any]], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    products: dict[str, dict[str, Any]] = {}
    saw_foreign_currency = False
    for card in soup.select(PRODUCT_CARD_SELECTORS):
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
        product_id = scrape._id("noon", url)
        products.setdefault(product_id, {
            "id": product_id,
            "store": "noon",
            "title": title[:500],
            "url": url,
            "image": scrape._image(card, page_url),
            "current_price": current,
            "list_price": None,
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
        try:
            payload = response.json()
        except (ValueError, TypeError):
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
        if not isinstance(final_url, str) or not _egypt_final_url(final_url):
            return None, "Reader final URL is not Noon Egypt search"
        html = data.get("html")
        if not isinstance(html, str) or not html.strip():
            return None, "Reader returned empty HTML"
        if _is_challenge(html):
            return None, "Reader challenge page (not bypassed)"

        products, error = _parse_products(html, category, final_url)
        if error:
            return None, error
        if not products:
            return None, "Reader returned no valid Noon Egypt EGP products"
        return products, None
