from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from scraper.reader import NoonReader, READER_HEADERS, noon_search_url
from scraper.worker import DataBranchPublisher, PriceWorker, RequestGate


SOURCE_URL = "https://www.noon.com/egypt-ar/search/?q=phone"
PRODUCT_HTML = """
<html lang="ar-EG"><body>
  <div data-qa="plp-product-box">
    <a href="/egypt-ar/phone-model/N12345678V/p/">
      <h2 data-qa="plp-product-box-name">هاتف تجريبي</h2>
      <div data-qa="plp-product-box-price">
        <span class="currency">جنيه</span>
        <div class="sellingPrice"><strong class="amount">1,234</strong></div>
        <span class="oldPrice">1,500</span>
      </div>
    </a>
  </div>
</body></html>
"""


@dataclass
class Response:
    status_code: int = 200
    payload: object = None

    def json(self):
        return self.payload


class Client:
    def __init__(self, response=None, error=None, now=None):
        self.response = response or reader_response()
        self.error = error
        self.now = now
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append((url, headers, timeout, self.now[0] if self.now else None))
        if self.error:
            raise self.error
        return self.response


def reader_response(*, origin_status=200, final_url=SOURCE_URL, html=PRODUCT_HTML, service_status=200):
    return Response(payload={
        "code": service_status,
        "data": {"httpStatus": origin_status, "finalUrl": final_url, "html": html},
    })


def gated_reader(client, *, now=None, interval=0):
    now = now if now is not None else [0.0]

    def sleep(seconds):
        now[0] += seconds

    gate = RequestGate(interval, clock=lambda: now[0], sleeper=sleep)
    reader = NoonReader(client=client)
    return reader, gate, now


def test_reader_fetches_and_parses_current_egyptian_egp_products():
    now = [0.0]
    client = Client(now=now)
    reader, gate, _ = gated_reader(client, now=now)

    products, error = reader.fetch(SOURCE_URL, "phones", gate=gate, deadline=100, clock=lambda: now[0])

    assert error is None
    assert len(products) == 1
    assert products[0]["id"] == "noon-N12345678V"
    assert products[0]["current_price"] == 1234
    assert products[0]["category"] == "phones"
    url, headers, timeout, _ = client.calls[0]
    assert url == "https://r.jina.ai/" + SOURCE_URL
    assert headers == READER_HEADERS == {
        "Accept": "application/json",
        "X-Respond-With": "html",
        "X-No-Cache": "true",
    }
    assert timeout <= 20
    assert gate.last_started == 0


@pytest.mark.parametrize(
    ("response", "error_part"),
    [
        (Response(status_code=403), "Reader HTTP 403"),
        (reader_response(origin_status=503), "Reader origin HTTP 503"),
        (reader_response(final_url="https://www.noon.com/uae-en/search/?q=phone"), "final URL"),
        (reader_response(html="<html><title>Verify you are human</title></html>"), "challenge"),
        (reader_response(html=PRODUCT_HTML.replace("جنيه", "AED")), "currency"),
        (reader_response(html="<html lang='ar-EG'><body>No products</body></html>"), "no valid"),
        (reader_response(service_status=429), "Reader service HTTP 429"),
    ],
)
def test_reader_rejects_bad_status_market_challenge_currency_and_empty_data(response, error_part):
    client = Client(response=response)
    reader, gate, now = gated_reader(client)

    products, error = reader.fetch(SOURCE_URL, "phones", gate=gate, deadline=100, clock=lambda: now[0])

    assert products is None
    assert error_part.lower() in error.lower()
    assert len(client.calls) == 1


def test_reader_rejects_non_noon_sources_without_sending_a_request():
    client = Client()
    reader, gate, now = gated_reader(client)

    products, error = reader.fetch("https://example.com/?q=phone", "phones", gate=gate, deadline=100, clock=lambda: now[0])

    assert products is None
    assert "not a Noon Egypt" in error
    assert client.calls == []
    assert gate.last_started is None


def test_reader_does_not_start_when_request_gate_cannot_meet_deadline():
    client = Client()
    reader, gate, now = gated_reader(client, interval=60)
    gate.mark_started()

    products, error = reader.fetch(SOURCE_URL, "phones", gate=gate, deadline=30, clock=lambda: now[0])

    assert products is None and error is None
    assert client.calls == []
    assert now[0] == 0


def test_reader_network_error_is_not_retried():
    request = httpx.Request("GET", "https://r.jina.ai/")
    client = Client(error=httpx.ConnectError("reset", request=request))
    reader, gate, now = gated_reader(client)

    products, error = reader.fetch(SOURCE_URL, "phones", gate=gate, deadline=100, clock=lambda: now[0])

    assert products is None
    assert "no retry" in error
    assert len(client.calls) == 1


def test_worker_gates_each_noon_request_before_using_reader_fallback(tmp_path):
    now = [0.0]
    starts = []

    class PrimaryScraper:
        def fetch(self, store, url):
            starts.append(("primary", now[0]))
            return None, "request error: ConnectError"

    client = Client(now=now)
    worker = PriceWorker(
        "noon", ["phones"], DataBranchPublisher(tmp_path), priority_categories=["phones"],
        request_interval=60, clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        scraper_factory=PrimaryScraper,
    )
    worker.reader = NoonReader(client=client)

    products, error = worker._request("phones", deadline=100)

    assert error is None
    assert len(products) == 1
    assert starts == [("primary", 0)]
    assert client.calls[0][3] == 60
    assert now[0] == 60


@pytest.mark.parametrize("denial", ["HTTP 403 (no retry)", "HTTP 429", "challenge page (not bypassed)"])
def test_worker_never_uses_reader_after_explicit_access_denial(tmp_path, denial):
    class PrimaryScraper:
        def fetch(self, store, url):
            return None, denial

    client = Client()
    worker = PriceWorker(
        "noon", ["phones"], DataBranchPublisher(tmp_path), priority_categories=["phones"],
        request_interval=1, scraper_factory=PrimaryScraper,
    )
    worker.reader = NoonReader(client=client)

    products, error = worker._request("phones", deadline=10**20)

    assert products is None
    assert error == denial
    assert client.calls == []


def test_worker_only_uses_reader_for_noon_and_backoff_recognizes_reader_denials(tmp_path):
    class PrimaryScraper:
        def fetch(self, store, url):
            return None, "HTTP 503"

    worker = PriceWorker(
        "amazon", ["phones"], DataBranchPublisher(tmp_path), priority_categories=["phones"],
        request_interval=1, scraper_factory=PrimaryScraper,
    )
    products, error = worker._request("phones", deadline=10**20)
    assert products is None and error == "HTTP 503"
    assert worker.reader is None

    noon = PriceWorker(
        "noon", ["phones"], DataBranchPublisher(tmp_path), priority_categories=["phones"],
        request_interval=1, scraper_factory=PrimaryScraper,
    )
    noon._handle_failure("phones", "primary HTTP 503; Reader fallback failed: Reader HTTP 429 (no retry)")
    assert noon.store_retry_at > noon.clock()


def test_noon_search_urls_are_category_scoped():
    assert noon_search_url("phones").startswith("https://www.noon.com/egypt-ar/search/?q=")
    with pytest.raises(ValueError):
        noon_search_url("unsupported")
