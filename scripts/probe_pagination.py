#!/usr/bin/env python3
"""Probe MobileSentrix category pagination."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup
from fetch_layer import fetch_with_fallback
from mobilesentrix_scraper import fetch_html, parse_category_products

URL = "https://www.mobilesentrix.com/replacement-parts/apple/iphone-parts/iphone-12-pro"


def count_page(url: str) -> tuple[int, BeautifulSoup]:
    result = fetch_with_fallback(url, warmup=False)
    if not result.ok:
        print(f"FAIL {url}: {result.status_code} {result.error}")
        return 0, BeautifulSoup("", "lxml")
    soup = BeautifulSoup(result.html, "lxml")
    products = parse_category_products(soup, url)
    return len(products), soup


def main() -> None:
    html = fetch_html(URL)
    soup = BeautifulSoup(html, "lxml")
    print("page1 cards:", len(soup.select("li.item")))
    print("page1 parsed:", len(parse_category_products(soup, URL)))

    for cls in ["toolbar", "pager", "pages", "limiter", "load-more", "show-more"]:
        els = soup.select(f'[class*="{cls}"]')
        if els:
            print(f"\n=== {cls} ({len(els)}) ===")
            for el in els[:5]:
                print(" ", el.get("class"), "|", el.get_text(" ", strip=True)[:100])

    patterns = [
        r"page=\d+",
        r'"total"\s*:\s*\d+',
        r"of\s+\d+",
        r"loadMore",
        r"nextUrl",
        r"productCount",
        r"itemsCount",
        r"catalog_category_view",
    ]
    for pat in patterns:
        matches = re.findall(pat, html, re.I)
        if matches:
            print(f"{pat}: {matches[:8]}")

    print("\n--- pagination URL probes ---")
    candidates = [
        f"{URL}?p=2",
        f"{URL}?page=2",
        f"{URL}?paged=2",
        f"{URL}?product_list_limit=96",
        f"{URL}?product_list_limit=200",
        f"{URL}?product_list_limit=500",
        f"{URL}?product_list_limit=1000",
        f"{URL}?limit=200",
        f"{URL}?limit=all",
    ]
    for u in candidates:
        n, s = count_page(u)
        print(f"{n:4} products | {u}")
        if n > 46:
            print("  ^ more than page 1!")


if __name__ == "__main__":
    main()
