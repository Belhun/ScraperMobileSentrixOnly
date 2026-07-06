# MobileSentrix Scraper (CLI fork)

Focused command-line scraper for [mobilesentrix.com](https://www.mobilesentrix.com). This repo is a **fork** of the original [ScraperMobileSentrix-](https://github.com/Ab00000064/ScraperMobileSentrix-) project, narrowed to MobileSentrix-only scraping with no web UI.

## About this fork

| | Original repo | This repo (`ScraperMobileSentrixOnly`) |
|---|---------------|----------------------------------------|
| **Interface** | Flask web app (browser UI) | CLI + Python API |
| **Sites** | MobileSentrix, XCellParts, TXParts, and more | MobileSentrix only |
| **Data storage** | SQLite + session history in the app | JSON files or in-memory (you choose) |
| **Deployment** | Fly.io live demo | Run locally on your machine |
| **Scope** | Full product suite (stats, export, image tools) | Category listings, product details, Cloudflare bypass |

Use this fork when you want a scriptable scraper you can run from a terminal, import into other tools, or schedule yourself. Use the original if you need the multi-vendor web dashboard.

## How it works

1. **`fetch_layer.py`** requests pages from mobilesentrix.com. It tries fast HTTP clients first (`curl_cffi`, `cloudscraper`), then browser fallbacks (Playwright, Selenium, undetected-chromedriver) if Cloudflare blocks a request.
2. **`mobilesentrix_scraper.py`** parses category listing pages (with pagination), extracts product cards, and can follow each product URL for detail-page fields.
3. **`test_scraper.py`** is the main entry point for CLI runs and JSON export.

Typical flow:

```
URL in  →  fetch_layer (bypass)  →  BeautifulSoup parse  →  structured dicts / JSON out
```

Each category listing includes: `title`, `compatible_title`, `badge_label`, `color_info`, `price_text`, `price_value`, `url`, `page_number`.

## Setup

```powershell
git clone https://github.com/Belhun/ScraperMobileSentrixOnly.git
cd ScraperMobileSentrixOnly
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Fetch methods

| Method | When it helps |
|--------|----------------|
| **cloudscraper** | Usually enough; often succeeds before browsers run |
| **Playwright** | Headless Chromium when HTTP clients get 403 |
| **Selenium** | Backup browser if Playwright fails; needs Chrome (`webdriver-manager` downloads the driver) |
| **undetected-chromedriver** | Last resort; harder for sites to fingerprint as automation |

You do not need Selenium for normal runs. It is there if Cloudflare tightens and `cloudscraper` stops working.

## How to interact with it

### CLI

```powershell
# Full category scrape (all pages, e.g. 345 iPhone 12 Pro parts)
python test_scraper.py --category-only

# Category + product detail pages
python test_scraper.py

# Save JSON output
python test_scraper.py --category-only --json output/category.json

# Only page 1 (faster)
python test_scraper.py --category-only --single-page

# Only OLED/LCD screen assemblies
python test_scraper.py --category-only --screen-assemblies-only

# Single product detail page
python test_scraper.py --detail-url "https://www.mobilesentrix.com/lcd-assembly-for-iphone-12-12-pro-aftermarket-incell"

# Test Cloudflare bypass methods
python scripts/test_bypass_methods.py --quick
```

### Python API

```python
from mobilesentrix_scraper import scrape_category, scrape_product_detail, scrape_category_with_details

products = scrape_category(
    "https://www.mobilesentrix.com/replacement-parts/apple/iphone-parts/iphone-12-pro"
)
detail = scrape_product_detail(products[0].url)
full = scrape_category_with_details(products[0].url)  # category URL → listings + details
```

### Project layout

| File | Role |
|------|------|
| `mobilesentrix_scraper.py` | Category + detail parsing, pagination |
| `fetch_layer.py` | Cloudflare bypass fallback chain |
| `test_scraper.py` | CLI test runner |
| `scripts/test_bypass_methods.py` | Per-method bypass diagnostics |
| `scripts/probe_pagination.py` | Pagination debug helper |

### Optional environment variables

| Variable | Purpose |
|----------|---------|
| `PROXY_URL` | HTTP proxy for all fetch methods |
| `FLARESOLVERR_URL` | FlareSolverr endpoint (if running locally) |

## What was removed from the original

The original repo included a large Flask application and supporting assets. This fork **removed**:

- Web UI (`templates/`, `static/`, `app.py`)
- SQLite database layer and session history (`database.py`)
- Multi-vendor engines (`xcell_scraper_engine.py`, `txparts_scraper_engine.py`, etc.)
- Docker / Fly.io deployment (`Dockerfile`, `fly.toml`, GitHub Actions deploy workflow)
- Image converter, scheduler UI, and related docs

**Added** in this fork:

- `fetch_layer.py` — layered Cloudflare bypass
- `mobilesentrix_scraper.py` — MobileSentrix-specific parsers
- `test_scraper.py` — CLI runner
- `scripts/test_bypass_methods.py`, `scripts/probe_pagination.py` — diagnostics

## No live demo (and why GitHub Pages will not help)

The original README linked to a **Fly.io** deployment (`mobilesentrix-tool-v8.fly.dev`). That demo was tied to the original author's hosting account, not this fork. You do not need to own mobilesentrix.com to scrape it locally; the blocker for a public demo is **where the scraper runs**, not site ownership.

**GitHub Pages is not a fit for this project:**

- Pages hosts **static files only** (HTML, CSS, JS). It cannot run Python, Flask, Playwright, or Selenium.
- The old web app needed a **server** to scrape, store sessions, and serve API routes. Pages cannot do that.
- This fork is **CLI-only**. There is no web interface to host. You run it on your PC or a server you control (VPS, scheduled task, etc.).

If you want a browser-based tool again, you would need to redeploy the original Flask stack (or rebuild a UI) on a platform that runs Python, such as Fly.io, Railway, or a VPS. That is separate from this repo's scope.

## Suggested GitHub repo description

Paste this into the repo **About** field on GitHub:

> Fork of ScraperMobileSentrix- focused on a MobileSentrix-only CLI scraper with layered Cloudflare bypass. No web UI; run locally via test_scraper.py or import the Python API.
