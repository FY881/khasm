import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from scraper import scoring, scrape
from scraper.scrape import parse_price, parse_search_page, search_pages


class ScraperTests(unittest.TestCase):
    def test_arabic_and_latin_prices(self):
        self.assertEqual(parse_price("١٢٬٩٩٩٫٥٠ جنيه"), 12999.5)
        self.assertEqual(parse_price("EGP 12,999.50"), 12999.5)
        self.assertEqual(parse_price("جنيه ٢٥٠"), 250.0)
        self.assertIsNone(parse_price(None))
        self.assertIsNone(parse_price("not a price"))
        self.assertIsNone(parse_price("0 EGP"))

    def test_amazon_card(self):
        html = '''<div data-component-type="s-search-result" data-asin="B012345678">
          <h2><a href="/dp/B012345678?ref=x"><span>هاتف تجريبي</span></a></h2>
          <span class="a-price"><span class="a-offscreen">EGP 8,500.00</span></span>
          <span class="a-price a-text-price"><span class="a-offscreen">EGP 10,000.00</span></span>
          <img class="s-image" src="https://images.example/item.jpg"></div>'''
        products = parse_search_page("amazon", "phones", "https://www.amazon.eg/s?k=phone", html)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["id"], "amazon-B012345678")
        self.assertEqual(products[0]["current_price"], 8500.0)
        self.assertEqual(products[0]["list_price"], 10000.0)
        self.assertEqual(products[0]["url"], "https://www.amazon.eg/dp/B012345678")

    def test_amazon_ignores_explicitly_unavailable_cards_with_prices(self):
        html = '''
        <div data-component-type="s-search-result" data-asin="B012345678">
          <h2><a href="/dp/B012345678">Unavailable phone</a></h2>
          <span class="a-price"><span class="a-offscreen">EGP 8,500.00</span></span>
          <span class="a-color-state">Currently unavailable.</span>
        </div>
        <div data-component-type="s-search-result" data-asin="B012345679">
          <h2><a href="/dp/B012345679">هاتف غير متوفر</a></h2>
          <span class="a-price"><span class="a-offscreen">EGP 7,500.00</span></span>
        </div>
        <div data-component-type="s-search-result" data-asin="B012345680">
          <h2><a href="/dp/B012345680">Schema unavailable phone</a></h2>
          <span class="a-price"><span class="a-offscreen">EGP 6,500.00</span></span>
          <link itemprop="availability" href="https://schema.org/OutOfStock">
        </div>
        <div data-component-type="s-search-result" data-asin="B012345681">
          <h2><a href="/dp/B012345681">متوفر هاتف</a></h2>
          <span class="a-price"><span class="a-offscreen">EGP 5,500.00</span></span>
          <link itemprop="availability" href="https://schema.org/InStock">
        </div>'''

        products = parse_search_page("amazon", "phones", "https://www.amazon.eg/s?k=phone", html)

        self.assertEqual([product["id"] for product in products], ["amazon-B012345681"])
        self.assertEqual(products[0]["current_price"], 5500.0)

    def test_noon_legacy_html_card_without_verified_jsonld_is_ignored(self):
        html = '''<div data-qa="product-block"><a href="/egypt-ar/product/p/ABC123/"><h3>منتج تجريبي</h3>
          <img src="https://images.example/noon.jpg"></a><span data-qa="price">١٬٢٩٩ جنيه</span>
          <del>١٬٥٠٠ جنيه</del></div>'''
        products = parse_search_page("noon", "phones", "https://www.noon.com/egypt-ar/search/?q=x", html)
        self.assertEqual(products, [])

    def test_noon_ids_cover_both_public_path_shapes(self):
        from scraper.scrape import _id

        self.assertEqual(_id("noon", "https://www.noon.com/egypt-ar/item/N12345678A/p/"), "noon-N12345678A")
        self.assertEqual(_id("noon", "https://www.noon.com/egypt-ar/product/p/ABC123"), "noon-ABC123")

    def test_ten_searches_each(self):
        pages = search_pages()
        self.assertEqual(sum(s == "noon" for s, _, _ in pages), 10)
        self.assertEqual(sum(s == "amazon" for s, _, _ in pages), 10)

    def test_noon_search_uses_small_public_json_page(self):
        noon_pages = [(category, url) for store, category, url in search_pages() if store == "noon"]
        category, url = noon_pages[0]
        self.assertEqual(category, "phones")
        self.assertEqual(urlsplit(url).hostname, "www.noon.com")
        self.assertEqual(urlsplit(url).path, "/_vs/nc/mp-customer-catalog-api/api/v3/u/search")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["q"], ["هواتف ذكية"])
        self.assertEqual(query["limit"], ["20"])
        self.assertEqual(query["country"], ["eg"])
        self.assertEqual(query["language"], ["ar"])

    def test_noon_json_fixture_prices_seller_urls_and_stock(self):
        fixture = Path(__file__).parent / "fixtures" / "noon_search.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        products = parse_search_page(
            "noon", "phones", "https://www.noon.com/_vs/nc/mp-customer-catalog-api/api/v3/u/search", json.dumps(payload),
        )
        self.assertEqual([item["id"] for item in products], ["noon-N12345678A", "noon-N23456789A"])
        self.assertEqual(products[0]["current_price"], 1267.0)
        self.assertEqual(products[0]["list_price"], 1649.0)
        self.assertEqual(products[0]["seller"], "Test seller")
        self.assertEqual(products[0]["url"], "https://www.noon.com/sample-phone/N12345678A/p?o=offer1")
        self.assertEqual(products[0]["image"], "https://f.nooncdn.com/p/sample.jpg")
        self.assertEqual(products[1]["current_price"], 900.0)
        self.assertIsNone(products[1]["list_price"])

    def test_noon_json_requires_explicit_egypt_region_and_egp_currency(self):
        fixture = Path(__file__).parent / "fixtures" / "noon_search.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload.pop("country_code")
        self.assertEqual(
            parse_search_page("noon", "phones", "https://www.noon.com/api", json.dumps(payload)),
            [],
        )
        payload["country_code"] = "EG"
        payload["currency"] = "AED"
        self.assertEqual(
            parse_search_page("noon", "phones", "https://www.noon.com/api", json.dumps(payload)),
            [],
        )

    def test_noon_json_rejects_mixed_region_or_currency_hits(self):
        fixture = Path(__file__).parent / "fixtures" / "noon_search.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload["hits"][0]["currency"] = "AED"
        payload["hits"][1]["country_code"] = "AE"
        products = parse_search_page(
            "noon", "phones", "https://www.noon.com/api", json.dumps(payload),
        )
        self.assertEqual(products, [])

    def test_noon_jsonld_fallback_requires_egypt_scoped_page_and_egp_offer(self):
        html = '''<script type="application/ld+json">
        {"@type":"Product","name":"هاتف schema","url":"/egypt-ar/product/p/ABC123",
         "offers":{"@type":"Offer","price":"12500.50","priceCurrency":"EGP",
                   "availability":"https://schema.org/InStock"}}
        </script>'''
        page = "https://www.noon.com/_vs/search?country=eg&language=ar"
        products = parse_search_page("noon", "phones", page, html)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["id"], "noon-ABC123")
        self.assertEqual(products[0]["current_price"], 12500.5)
        self.assertIsNone(products[0]["list_price"])

        self.assertEqual(
            parse_search_page("noon", "phones", "https://www.noon.com/search?q=x", html),
            [],
        )
        self.assertEqual(
            parse_search_page("noon", "phones", page + "&country=ae", html),
            [],
        )
        self.assertEqual(
            parse_search_page("noon", "phones", page, html.replace('"EGP"', '"AED"')),
            [],
        )
        self.assertEqual(
            parse_search_page("noon", "phones", page, html.replace("InStock", "OutOfStock")),
            [],
        )

    def test_noon_rejects_conflicting_region_query_parameters(self):
        page = "https://www.noon.com/egypt-ar/search?country=eg&country=ae"
        self.assertFalse(scrape._page_is_egypt_market("noon", page))
        self.assertFalse(scrape._page_is_egypt_market("noon", "https://www.noon.com/egypt-ar/search?country_code=eg&region=ae"))

        fixture = Path(__file__).parent / "fixtures" / "noon_search.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(
            parse_search_page("noon", "phones", page, json.dumps(payload)),
            [],
        )

    def test_amazon_jsonld_fallback_validates_egp_url_and_availability(self):
        html = '''<script type="application/ld+json">
        {"@type":"Product","name":"Phone","url":"/dp/B012345678",
         "image":"https://images.example/phone.jpg",
         "offers":{"@type":"Offer","price":"8500","priceCurrency":"EGP",
                   "seller":{"name":"Store"},"availability":"https://schema.org/InStock"}}
        </script>'''
        page = "https://www.amazon.eg/s?k=phone"
        products = parse_search_page("amazon", "phones", page, html)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["id"], "amazon-B012345678")
        self.assertEqual(products[0]["current_price"], 8500.0)
        self.assertEqual(products[0]["seller"], "Store")
        self.assertEqual(products[0]["image"], "https://images.example/phone.jpg")

        self.assertEqual(parse_search_page("amazon", "phones", page, html.replace('"EGP"', '"AED"')), [])
        self.assertEqual(parse_search_page("amazon", "phones", page, html.replace("InStock", "OutOfStock")), [])


class HttpClientTests(unittest.TestCase):
    def test_http2_client_and_egypt_locale_headers(self):
        with patch.object(scrape.httpx, "Client") as client_factory:
            scrape.StoreScraper()
        self.assertIs(client_factory.call_args.kwargs["http2"], True)

        url = "https://www.noon.com/_vs/nc/mp-customer-catalog-api/api/v3/u/search?q=x&limit=20&country=eg&language=ar"
        request = httpx.Request("GET", url)
        response = httpx.Response(200, request=request, json={"country_code": "EG", "currency": "EGP", "hits": []})
        client = patch.object(scrape.httpx, "Client")
        with client as client_factory:
            client_factory.return_value.headers = {}
            client_factory.return_value.get.return_value = response
            scraper = scrape.StoreScraper(delay=0, retries=0)
            body, error = scraper.fetch("noon", url)

        self.assertIsNone(error)
        self.assertEqual(json.loads(body), {"country_code": "EG", "currency": "EGP", "hits": []})
        headers = client_factory.return_value.get.call_args.kwargs["headers"]
        self.assertEqual(headers["x-locale"], "ar-eg")
        self.assertEqual(headers["Accept-Language"], "ar-EG,ar;q=0.9,en;q=0.7")
        self.assertEqual(headers["Referer"], "https://www.noon.com/egypt-ar/")

    def test_retries_transient_server_error_with_jitter_and_honors_retry_after(self):
        url = "https://www.noon.com/_vs/nc/mp-customer-catalog-api/api/v3/u/search?q=x&limit=20&country=eg&language=ar"
        request = httpx.Request("GET", url)
        unavailable = httpx.Response(503, headers={"Retry-After": "4"}, request=request)
        success = httpx.Response(200, request=request, json={"country_code": "EG", "currency": "EGP", "hits": []})
        with patch.object(scrape.httpx, "Client") as client_factory:
            client_factory.return_value.headers = {}
            client_factory.return_value.get.side_effect = [unavailable, success]
            scraper = scrape.StoreScraper(delay=0, retries=1)
            with patch.object(scrape.random, "uniform", return_value=0.25) as jitter, patch.object(scrape.time, "sleep") as sleep:
                body, error = scraper.fetch("noon", url)

        self.assertIsNone(error)
        self.assertEqual(json.loads(body), {"country_code": "EG", "currency": "EGP", "hits": []})
        self.assertEqual(client_factory.return_value.get.call_count, 2)
        jitter.assert_called_once_with(0, 1.0)
        sleep.assert_called_once_with(4.0)

    def test_retries_timeouts_but_does_not_retry_forbidden_responses(self):
        url = "https://www.noon.com/_vs/nc/mp-customer-catalog-api/api/v3/u/search?q=x&limit=20&country=eg&language=ar"
        request = httpx.Request("GET", url)
        success = httpx.Response(200, request=request, json={"country_code": "EG", "currency": "EGP", "hits": []})
        with patch.object(scrape.httpx, "Client") as client_factory:
            client_factory.return_value.headers = {}
            client_factory.return_value.get.side_effect = [httpx.ReadTimeout("timed out", request=request), success]
            scraper = scrape.StoreScraper(delay=0, retries=1)
            with patch.object(scrape.time, "sleep"):
                body, error = scraper.fetch("noon", url)
        self.assertIsNone(error)
        self.assertEqual(json.loads(body), {"country_code": "EG", "currency": "EGP", "hits": []})
        self.assertEqual(client_factory.return_value.get.call_count, 2)

        forbidden = httpx.Response(403, request=request)
        with patch.object(scrape.httpx, "Client") as client_factory:
            client_factory.return_value.headers = {}
            client_factory.return_value.get.return_value = forbidden
            scraper = scrape.StoreScraper(delay=0, retries=3)
            body, error = scraper.fetch("noon", url)
        self.assertIsNone(body)
        self.assertEqual(error, "HTTP 403 (no retry)")
        self.assertEqual(client_factory.return_value.get.call_count, 1)

    def test_noon_fetch_rejects_json_without_verified_egypt_currency(self):
        url = "https://www.noon.com/_vs/nc/mp-customer-catalog-api/api/v3/u/search?q=x&limit=20&country=eg&language=ar"
        request = httpx.Request("GET", url)
        response = httpx.Response(200, request=request, json={"hits": []})
        with patch.object(scrape.httpx, "Client") as client_factory:
            client_factory.return_value.headers = {}
            client_factory.return_value.get.return_value = response
            scraper = scrape.StoreScraper(delay=0, retries=0)
            body, error = scraper.fetch("noon", url)
        self.assertIsNone(body)
        self.assertEqual(error, "Noon JSON lacks verified Egypt/EGP market fields")

    def test_noon_fetch_accepts_html_only_from_egypt_scoped_store_route(self):
        url = "https://www.noon.com/_vs/search?q=x&country=eg&language=ar"
        request = httpx.Request("GET", url)
        response = httpx.Response(
            200, request=request, headers={"content-type": "text/html; charset=utf-8"},
            text="<html><script type='application/ld+json'>{}</script></html>",
        )
        with patch.object(scrape.httpx, "Client") as client_factory:
            client_factory.return_value.headers = {}
            client_factory.return_value.get.return_value = response
            scraper = scrape.StoreScraper(delay=0, retries=0)
            body, error = scraper.fetch("noon", url)
        self.assertIsNone(error)
        self.assertIn("application/ld+json", body)

        url = "https://www.noon.com/search?q=x"
        response = httpx.Response(
            200, request=httpx.Request("GET", url),
            headers={"content-type": "text/html"}, text="<html></html>",
        )
        with patch.object(scrape.httpx, "Client") as client_factory:
            client_factory.return_value.headers = {}
            client_factory.return_value.get.return_value = response
            scraper = scrape.StoreScraper(delay=0, retries=0)
            body, error = scraper.fetch("noon", url)
        self.assertIsNone(body)
        self.assertEqual(error, "Noon HTML page is not explicitly Egypt-scoped")


class PersistenceTests(unittest.TestCase):
    AMAZON_CARD = '''<div data-component-type="s-search-result" data-asin="B012345678">
      <h2><a href="/dp/B012345678"><span>هاتف تجريبي</span></a></h2>
      <span class="a-price"><span class="a-offscreen">EGP 8,500.00</span></span>
      <img class="s-image" src="https://images.example/item.jpg"></div>'''

    def test_overlapping_searches_append_one_observation(self):
        class FakeScraper:
            def __init__(self, *args, **kwargs):
                pass

            def fetch(self, store, url):
                return PersistenceTests.AMAZON_CARD, None

        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            products_path = data / "products.json"
            price_dir = data / "prices"
            with (
                patch.object(scrape, "ROOT", Path(temp)),
                patch.object(scrape, "DATA", data),
                patch.object(scrape, "PRODUCTS_FILE", products_path),
                patch.object(scrape, "PRICE_DIR", price_dir),
                patch.object(scrape, "StoreScraper", FakeScraper),
                patch.object(scrape, "search_pages", return_value=[
                    ("amazon", "phones", "https://www.amazon.eg/s?k=phone"),
                    ("amazon", "laptops", "https://www.amazon.eg/s?k=phone"),
                ]),
            ):
                result = scrape.run(delay=0)
            rows = (price_dir / "amazon.jsonl").read_text(encoding="utf-8").splitlines()
            payload = json.loads(products_path.read_text(encoding="utf-8"))
            self.assertEqual(result["total_unique"], 1)
            self.assertEqual(result["history_rows_appended"], 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(payload["products"]), 1)

    def test_unparsed_page_is_unavailable_and_timestamp_stays_null(self):
        class EmptyPageScraper:
            def __init__(self, *args, **kwargs):
                pass

            def fetch(self, store, url):
                return "<html><body>no product cards</body></html>", None

        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            products_path = data / "products.json"
            with (
                patch.object(scrape, "ROOT", Path(temp)),
                patch.object(scrape, "DATA", data),
                patch.object(scrape, "PRODUCTS_FILE", products_path),
                patch.object(scrape, "PRICE_DIR", data / "prices"),
                patch.object(scrape, "StoreScraper", EmptyPageScraper),
                patch.object(scrape, "search_pages", return_value=[
                    ("amazon", "phones", "https://www.amazon.eg/s?k=phones"),
                ]),
            ):
                result = scrape.run(delay=0)

            payload = json.loads(products_path.read_text(encoding="utf-8"))
            self.assertIsNone(payload["updated_at"])
            self.assertEqual(payload["blocked_pages"], [])
            self.assertEqual(payload["unavailable_pages"], result["unavailable_pages"])
            self.assertEqual(payload["unavailable_pages"][0]["reason"], "no products parsed")
            self.assertEqual(payload["products"], [])

    def test_recovers_saved_product_history_into_append_only_file(self):
        class BlockedScraper:
            def __init__(self, *args, **kwargs):
                pass

            def fetch(self, store, url):
                return None, "HTTP 403"

        history = [
            {"date": "2026-11-05T12:00:00Z", "price": 60.0},
            {"date": "2026-11-06T12:00:00Z", "price": 55.0},
        ]
        product = {
            "id": "noon-ABC123", "store": "noon", "title": "منتج سابق",
            "url": "https://www.noon.com/egypt-ar/product/p/ABC123",
            "image": None, "current_price": 55.0, "list_price": None, "seller": None,
            "category": "phones", "history": history,
            "score": {"status": "new", "discount_percent": 0.0, "lowest_days": None},
        }
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            products_path = data / "products.json"
            price_dir = data / "prices"
            products_path.parent.mkdir(parents=True)
            products_path.write_text(json.dumps({"updated_at": "2026-11-06T12:00:00Z", "products": [product]}), encoding="utf-8")
            with (
                patch.object(scrape, "ROOT", Path(temp)),
                patch.object(scrape, "DATA", data),
                patch.object(scrape, "PRODUCTS_FILE", products_path),
                patch.object(scrape, "PRICE_DIR", price_dir),
                patch.object(scrape, "StoreScraper", BlockedScraper),
                patch.object(scrape, "search_pages", return_value=[
                    ("noon", "phones", "https://www.noon.com/egypt-ar/search/?q=phones"),
                ]),
            ):
                result = scrape.run(delay=0)
            rows = [json.loads(line) for line in (price_dir / "noon.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["history_rows_appended"], 2)
            self.assertEqual([row["price"] for row in rows], [60.0, 55.0])

    def test_blocked_store_retains_products_and_does_not_rewrite_history(self):
        class BlockedScraper:
            def __init__(self, *args, **kwargs):
                pass

            def fetch(self, store, url):
                return None, "HTTP 403"

        old_history = [{"date": "2026-11-06T12:00:00Z", "price": 55.0}]
        old_product = {
            "id": "noon-ABC123", "store": "noon", "title": "منتج سابق",
            "url": "https://www.noon.com/egypt-ar/product/p/ABC123",
            "image": None, "current_price": 55.0, "list_price": None, "seller": None,
            "category": "phones", "history": old_history,
            "score": {"status": "new", "discount_percent": 0.0, "lowest_days": None},
        }
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            products_path = data / "products.json"
            price_dir = data / "prices"
            price_dir.mkdir(parents=True)
            history_path = price_dir / "noon.jsonl"
            history_path.write_text(
                json.dumps({"id": old_product["id"], "store": "noon", **old_history[0]}) + "\n",
                encoding="utf-8",
            )
            prior_history = history_path.read_bytes()
            products_path.write_text(json.dumps({"updated_at": "2026-11-06T12:00:00Z", "products": [old_product]}), encoding="utf-8")
            with (
                patch.object(scrape, "ROOT", Path(temp)),
                patch.object(scrape, "DATA", data),
                patch.object(scrape, "PRODUCTS_FILE", products_path),
                patch.object(scrape, "PRICE_DIR", price_dir),
                patch.object(scrape, "StoreScraper", BlockedScraper),
                patch.object(scrape, "search_pages", return_value=[
                    ("noon", "phones", "https://www.noon.com/egypt-ar/search/?q=phones"),
                ]),
                patch.object(scoring, "score_product", wraps=scoring.score_product) as score_call,
            ):
                result = scrape.run(delay=0)
            payload = json.loads(products_path.read_text(encoding="utf-8"))
            self.assertEqual(result["retained"], 1)
            self.assertEqual(payload["updated_at"], "2026-11-06T12:00:00Z")
            self.assertEqual(payload["blocked_pages"], result["blocked_pages"])
            self.assertEqual(payload["unavailable_pages"], [])
            self.assertEqual(payload["blocked_pages"][0]["reason"], "HTTP 403")
            self.assertEqual(payload["products"][0]["id"], old_product["id"])
            self.assertEqual(payload["products"][0]["current_price"], 55.0)
            self.assertEqual(score_call.call_args.kwargs["now"], scrape._datetime(old_history[0]["date"]))
            self.assertEqual(history_path.read_bytes(), prior_history)


if __name__ == "__main__":
    unittest.main()
