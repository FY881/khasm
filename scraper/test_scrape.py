import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_noon_card(self):
        html = '''<div data-qa="product-block"><a href="/egypt-ar/product/p/ABC123/"><h3>منتج تجريبي</h3>
          <img src="https://images.example/noon.jpg"></a><span data-qa="price">١٬٢٩٩ جنيه</span>
          <del>١٬٥٠٠ جنيه</del></div>'''
        products = parse_search_page("noon", "phones", "https://www.noon.com/egypt-ar/search/?q=x", html)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["current_price"], 1299.0)
        self.assertEqual(products[0]["list_price"], 1500.0)

    def test_noon_ids_cover_both_public_path_shapes(self):
        from scraper.scrape import _id

        self.assertEqual(_id("noon", "https://www.noon.com/egypt-ar/item/N12345678A/p/"), "noon-N12345678A")
        self.assertEqual(_id("noon", "https://www.noon.com/egypt-ar/product/p/ABC123"), "noon-ABC123")

    def test_ten_searches_each(self):
        pages = search_pages()
        self.assertEqual(sum(s == "noon" for s, _, _ in pages), 10)
        self.assertEqual(sum(s == "amazon" for s, _, _ in pages), 10)


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
