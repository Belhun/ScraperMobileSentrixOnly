#!/usr/bin/env python3
"""
CLI test tool for the MobileSentrix scraper.

Usage:
  python test_scraper.py
  python test_scraper.py --url https://www.mobilesentrix.com/replacement-parts/apple/iphone-parts/iphone-12-pro
  python test_scraper.py --category-only
  python test_scraper.py --json output.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mobilesentrix_scraper import (
    HAS_CURL,
    CategoryProduct,
    ProductDetail,
    scrape_category,
    scrape_category_with_details,
    scrape_product_detail,
)

DEFAULT_CATEGORY_URL = (
    "https://www.mobilesentrix.com/replacement-parts/apple/iphone-parts/iphone-12-pro"
)
EXPECTED_SCREEN_COUNT = 8
EXPECTED_FULL_CATEGORY_COUNT = 345


def print_listings(listings: list[CategoryProduct], label: str = "products") -> None:
    print(f"\nCategory listings ({len(listings)} {label}):\n")
    for index, item in enumerate(listings, 1):
        price = item.price_text or (
            f"${item.price_value:.2f}" if item.price_value is not None else "N/A"
        )
        print(f"{index}. {item.title}")
        if item.badge_label:
            print(f"   badge: {item.badge_label}")
        if item.compatible_title and item.compatible_title != item.title:
            print(f"   compatible: {item.compatible_title}")
        print(
            f"   price: {price} | color: {item.color_info or 'n/a'} | page: {item.page_number}"
        )
        print(f"   url: {item.url}")


def print_detail(detail: ProductDetail, heading: str) -> None:
    print(f"\n{heading}")
    print(f"title: {detail.title}")
    print(f"sku: {detail.sku}")
    print(f"price: {detail.price_text} ({detail.price_value})")
    print(f"rating: {detail.rating}")
    print(f"color: {detail.color_info}")
    print(f"badge: {detail.badge}")
    print(f"tags: {', '.join(detail.tags[:6])}")
    print(f"images: {len(detail.image_urls)}")
    if detail.image_urls:
        print(f"  first image: {detail.image_urls[0]}")
    print("description bullets:")
    for bullet in detail.description_bullets[:5]:
        print(f"  - {bullet[:120]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test MobileSentrix scraper")
    parser.add_argument("--url", default=DEFAULT_CATEGORY_URL, help="Category URL to scrape")
    parser.add_argument(
        "--category-only",
        action="store_true",
        help="Only scrape the category listing (skip detail pages)",
    )
    parser.add_argument(
        "--screen-assemblies-only",
        action="store_true",
        help="Only OLED/LCD assemblies in $13.93-$300.60 (default: all parts on page)",
    )
    parser.add_argument(
        "--single-page",
        action="store_true",
        help="Only scrape page 1 (skip pagination)",
    )
    parser.add_argument(
        "--detail-url",
        help="Scrape a single product detail page instead of a category",
    )
    parser.add_argument("--json", dest="json_path", help="Write full results to JSON file")
    args = parser.parse_args()

    if not HAS_CURL:
        print(
            "Warning: curl_cffi is not installed; Cloudflare bypass may fail.",
            file=sys.stderr,
        )

    listing_label = "screen assemblies" if args.screen_assemblies_only else "products"

    if args.detail_url:
        detail = scrape_product_detail(args.detail_url)
        print_detail(detail, "Product detail")
        payload = {"detail": detail.__dict__}
        listing_count = 1
    elif args.category_only:
        listings = scrape_category(
            args.url,
            screen_assemblies_only=args.screen_assemblies_only,
            paginate=not args.single_page,
        )
        print_listings(listings, label=listing_label)
        payload = {
            "category_url": args.url,
            "listing_count": len(listings),
            "listings": [item.__dict__ for item in listings],
        }
        listing_count = len(listings)
    else:
        payload = scrape_category_with_details(
            args.url,
            screen_assemblies_only=args.screen_assemblies_only,
            paginate=not args.single_page,
        )
        listings = [CategoryProduct(**row) for row in payload["listings"]]
        details = [ProductDetail(**row) for row in payload["details"]]
        print_listings(listings, label=listing_label)
        if args.screen_assemblies_only:
            print(
                f"\nExpected ~{EXPECTED_SCREEN_COUNT} OLED/LCD assemblies; "
                f"found {payload['listing_count']}"
            )
        else:
            pages = payload.get("pages_scraped", 1)
            print(
                f"\nTotal products on category page: {payload['listing_count']} "
                f"across {pages} page(s)"
            )
        if details:
            print_detail(details[0], "Sample detail #1")
        if len(details) > 1:
            print_detail(details[1], "Sample detail #2")
        listing_count = payload["listing_count"]

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote JSON to {args.json_path}")

    if args.detail_url:
        return 0
    if args.screen_assemblies_only and not args.detail_url:
        return 0 if listing_count >= EXPECTED_SCREEN_COUNT else 1
    if not args.screen_assemblies_only and not args.single_page and not args.detail_url:
        return 0 if listing_count >= EXPECTED_FULL_CATEGORY_COUNT else 1
    return 0 if listing_count > 0 or args.detail_url else 1


if __name__ == "__main__":
    raise SystemExit(main())
