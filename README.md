# MobileSentrix Scraper

CLI scraper for [mobilesentrix.com](https://www.mobilesentrix.com). Fetches category listings (with pagination) and product detail pages. Uses a layered Cloudflare bypass (`curl_cffi`, `cloudscraper`, Playwright fallbacks).

## Setup

```powershell
cd "F:\Codeing Project\MobilesentrixScraper"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

`fetch_layer.py` tries fast methods first (`curl_cffi`, `cloudscraper`), then browser fallbacks if those fail:

| Method | When it helps |
|--------|----------------|
| **cloudscraper** | Usually enough; often succeeds before browsers run |
| **Playwright** | Real headless Chromium when HTTP clients get 403 |
| **Selenium** | Backup browser if Playwright fails; needs Chrome installed (`webdriver-manager` downloads the driver) |
| **undetected-chromedriver** | Last resort; harder for sites to fingerprint as automation |

You do not need Selenium for normal runs. It is there so the scraper still has options if Cloudflare tightens and `cloudscraper` stops working.

## Usage

```powershell
# Full category scrape (all pages, e.g. 345 iPhone 12 Pro parts)
python test_scraper.py --category-only

# Category + product detail pages
python test_scraper.py

# Save JSON output
python test_scraper.py --category-only --json output/category.json

# Only page 1 (faster)
python test_scraper.py --category-only --single-page

# Only OLED/LCD screen assemblies (8 items)
python test_scraper.py --category-only --screen-assemblies-only

# Single product detail page
python test_scraper.py --detail-url "https://www.mobilesentrix.com/lcd-assembly-for-iphone-12-12-pro-aftermarket-incell"

# Test Cloudflare bypass methods
python scripts/test_bypass_methods.py --quick
```

## Python API

```python
from mobilesentrix_scraper import scrape_category, scrape_product_detail, scrape_category_with_details

products = scrape_category(
    "https://www.mobilesentrix.com/replacement-parts/apple/iphone-parts/iphone-12-pro"
)
detail = scrape_product_detail(products[0].url)
```

Each listing includes: `title`, `compatible_title`, `badge_label`, `color_info`, `price_text`, `price_value`, `url`, `page_number`.

## Project layout

| File | Role |
|------|------|
| `mobilesentrix_scraper.py` | Category + detail parsing, pagination |
| `fetch_layer.py` | Cloudflare bypass fallback chain |
| `test_scraper.py` | CLI test runner |
| `scripts/test_bypass_methods.py` | Per-method bypass diagnostics |
| `scripts/probe_pagination.py` | Pagination debug helper |

## Optional environment variables

| Variable | Purpose |
|----------|---------|
| `PROXY_URL` | HTTP proxy for all fetch methods |
| `FLARESOLVERR_URL` | FlareSolverr endpoint (if running locally) |
