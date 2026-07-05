#!/usr/bin/env python3
"""
Test each MobileSentrix bypass method individually and report pass/fail.

Usage:
    python scripts/test_bypass_methods.py
    python scripts/test_bypass_methods.py --quick
    python scripts/test_bypass_methods.py --json-out bypass_results.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fetch_layer import (
    BYPASS_METHODS,
    CURL_IMPERSONATE_PROFILES,
    HAS_CLOUDSCRAPER,
    HAS_CURL,
    HAS_HTTPX,
    HAS_PLAYWRIGHT,
    HAS_SELENIUM,
    HAS_UC,
    FetchResult,
    fetch_with_fallback,
    get_bypass_methods,
)

CATEGORY_URL = (
    "https://www.mobilesentrix.com/replacement-parts/apple/iphone-parts/iphone-12-pro"
)
# Resolved at runtime from category page when possible
FALLBACK_DETAIL_URL = (
    "https://www.mobilesentrix.com/oled-assembly-compatible-for-iphone-12-12-pro-used-oem-pull-grade-a"
)


def _extract_product_url(html: str, base_url: str) -> str | None:
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "lxml")
    for link in soup.select("li.item a[href], .product-name a[href]"):
        href = link.get("href", "")
        if href and "/iphone-12-pro/" in href and href.count("/") >= 6:
            return urljoin(base_url, href)
    return None


def run_method(name: str, fn, url: str, timeout: int) -> dict:
    started = time.time()
    try:
        result: FetchResult = fn(url, timeout=timeout)
    except TypeError:
        result = fn(url)
    except Exception as exc:
        result = FetchResult(url=url, html="", status_code=0, method=name, error=str(exc))

    elapsed = round(time.time() - started, 2)
    return {
        "method": name,
        "url": url,
        "pass": result.ok,
        "status_code": result.status_code,
        "cf_detected": result.cf_detected,
        "bytes": len(result.html),
        "error": result.error,
        "elapsed_s": elapsed,
    }


def print_table(rows: list[dict], title: str) -> None:
    print(f"\n{title}")
    print("=" * 88)
    print(f"{'Method':<28} {'Pass':<6} {'Status':<8} {'Bytes':<10} {'CF':<5} {'Time':<8} Error")
    print("-" * 88)
    for row in rows:
        status = "PASS" if row["pass"] else "FAIL"
        err = (row.get("error") or "")[:40]
        print(
            f"{row['method']:<28} {status:<6} {row['status_code']:<8} "
            f"{row['bytes']:<10} {str(row['cf_detected']):<5} {row['elapsed_s']:<8} {err}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Test MobileSentrix bypass methods")
    parser.add_argument("--category-url", default=CATEGORY_URL)
    parser.add_argument("--detail-url", default=None, help="Override product detail URL")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--quick", action="store_true", help="Skip slow browser methods")
    parser.add_argument("--json-out", help="Write results JSON to file")
    args = parser.parse_args()

    print("Dependency availability:")
    print(f"  curl_cffi:            {HAS_CURL}")
    print(f"  cloudscraper:         {HAS_CLOUDSCRAPER}")
    print(f"  httpx:                {HAS_HTTPX}")
    print(f"  playwright:           {HAS_PLAYWRIGHT}")
    print(f"  selenium:             {HAS_SELENIUM}")
    print(f"  undetected-chromedriver: {HAS_UC}")

    methods = get_bypass_methods()
    slow_methods = {"playwright", "selenium", "undetected_chrome", "flaresolverr", "curl_cffi_rotating"}

    category_rows: list[dict] = []
    for name, fn in sorted(methods.items()):
        if args.quick and name in slow_methods:
            continue
        if name == "warmup_homepage":
            continue
        print(f"Testing {name} on category...", flush=True)
        category_rows.append(run_method(name, fn, args.category_url, args.timeout))

    print_table(category_rows, f"Category page: {args.category_url}")

    detail_url = args.detail_url
    if not detail_url:
        for row in category_rows:
            if row["pass"]:
                from fetch_layer import fetch_curl_cffi

                cat_html = fetch_curl_cffi(args.category_url, timeout=args.timeout).html
                detail_url = _extract_product_url(cat_html, args.category_url)
                if detail_url:
                    break
        detail_url = detail_url or FALLBACK_DETAIL_URL

    print(f"\nProduct detail URL: {detail_url}")

    detail_rows: list[dict] = []
    for name, fn in sorted(methods.items()):
        if args.quick and name in slow_methods:
            continue
        if name == "warmup_homepage":
            continue
        print(f"Testing {name} on detail...", flush=True)
        detail_rows.append(run_method(name, fn, detail_url, args.timeout))

    print_table(detail_rows, f"Product detail: {detail_url}")

    print("\nFallback chain test...")
    chain_result = fetch_with_fallback(args.category_url, timeout=args.timeout, warmup=True)
    print(
        f"  chain pass={chain_result.ok} method={chain_result.method} "
        f"status={chain_result.status_code} bytes={len(chain_result.html)}"
    )

    payload = {
        "category_url": args.category_url,
        "detail_url": detail_url,
        "category_results": category_rows,
        "detail_results": detail_rows,
        "fallback_chain": {
            "pass": chain_result.ok,
            "method": chain_result.method,
            "status_code": chain_result.status_code,
            "bytes": len(chain_result.html),
        },
        "curl_profiles": list(CURL_IMPERSONATE_PROFILES),
    }

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {out}")

    cat_pass = sum(1 for r in category_rows if r["pass"])
    det_pass = sum(1 for r in detail_rows if r["pass"])
    print(f"\nSummary: category {cat_pass}/{len(category_rows)} passed, detail {det_pass}/{len(detail_rows)} passed")
    print(f"Fallback chain: {'PASS' if chain_result.ok else 'FAIL'}")

    return 0 if chain_result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
