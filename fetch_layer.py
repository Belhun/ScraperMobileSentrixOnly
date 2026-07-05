"""
Layered HTTP fetch with Cloudflare / bot-protection bypass fallbacks.

Tries multiple transport methods in order until a valid HTML response is returned.
Used by mobilesentrix_scraper.py and scraper_engine.py.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from http.cookiejar import Cookie, LWPCookieJar
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Optional dependencies
try:
    from curl_cffi import requests as curl_requests

    HAS_CURL = True
except ImportError:
    HAS_CURL = False
    curl_requests = None  # type: ignore

try:
    import cloudscraper

    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    import undetected_chromedriver as uc

    HAS_UC = True
except ImportError:
    HAS_UC = False

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager

    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

DEFAULT_COOKIE_FILE = Path(__file__).resolve().parent / ".mobilesentrix_cookies.txt"
HOMEPAGE_URL = "https://www.mobilesentrix.com/"

CURL_IMPERSONATE_PROFILES = [
    "chrome120",
    "chrome119",
    "chrome116",
    "chrome110",
    "chrome107",
    "chrome104",
    "edge101",
    "edge99",
    "safari17_0",
    "safari15_5",
]

USER_AGENT_PROFILES: List[Dict[str, str]] = [
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-CH-UA": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-CH-UA": '"Google Chrome";v="119", "Chromium";v="119", "Not?A_Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
    },
]

CF_BLOCKING_PATTERNS = [
    "checking your browser before accessing",
    "cf-browser-verification",
    "__cf_chl_jschl_tk__",
    "please wait while we check your browser",
    "just a moment",
    "enable javascript and cookies",
]

PRODUCT_PAGE_MARKERS = [
    "products-grid",
    "product-listing",
    "category-products",
    "page-title",
    "product-info-main",
    "h1.page-title",
    "mobilesentrix",
    "li.item",
]


@dataclass
class FetchResult:
    """Normalized fetch response."""

    url: str
    html: str
    status_code: int
    method: str
    ttfb_ms: float = 0.0
    total_ms: float = 0.0
    cf_detected: bool = False
    error: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            self.status_code == 200
            and bool(self.html)
            and len(self.html) > 500
            and not self.cf_detected
            and _looks_like_content(self.html)
        )


def _proxy_url() -> Optional[str]:
    return os.environ.get("PROXY_URL") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")


def _proxy_dict() -> Optional[Dict[str, str]]:
    proxy = _proxy_url()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def detect_cloudflare(html: str) -> bool:
    """True only for actual Cloudflare challenge/block pages, not normal CF-backed sites."""
    if not html:
        return False
    lowered = html.lower()
    if _looks_like_content(html):
        return False
    if len(html) > 50_000:
        return False
    hits = sum(1 for marker in CF_BLOCKING_PATTERNS if marker in lowered)
    return hits >= 1 and (
        "checking your browser" in lowered
        or "cf-browser-verification" in lowered
        or "just a moment" in lowered
    )


def is_blocked_status(status_code: int, html: str) -> bool:
    if status_code in (403, 429, 503):
        return not _looks_like_content(html)
    return False


def _looks_like_content(html: str) -> bool:
    if not html or len(html) < 500:
        return False
    lowered = html.lower()
    return any(marker in lowered for marker in PRODUCT_PAGE_MARKERS)


def _jitter_delay(min_s: float = 0.05, max_s: float = 0.2) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0) -> None:
    delay = min(cap, base * (2 ** attempt)) + random.uniform(0, 0.3)
    time.sleep(delay)


class CookieStore:
    """Persist cookies across fetch attempts."""

    def __init__(self, path: Path = DEFAULT_COOKIE_FILE):
        self.path = path
        self._jar = LWPCookieJar(str(path))
        if path.exists():
            try:
                self._jar.load(ignore_discard=True, ignore_expires=True)
            except Exception as exc:
                logger.debug("Could not load cookie jar: %s", exc)

    def apply_to_requests(self, session: requests.Session) -> None:
        for cookie in self._jar:
            session.cookies.set_cookie(cookie)

    def update_from_requests(self, session: requests.Session) -> None:
        self._jar.clear()
        for cookie in session.cookies:
            self._jar.set_cookie(cookie)
        self.save()

    def update_from_list(self, cookies: List[Dict[str, Any]]) -> None:
        for item in cookies:
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            domain = item.get("domain") or ".mobilesentrix.com"
            path = item.get("path") or "/"
            cookie = Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=True,
                secure=bool(item.get("secure")),
                expires=item.get("expires"),
                discard=False,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": item.get("httpOnly")} if item.get("httpOnly") else {},
            )
            self._jar.set_cookie(cookie)
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._jar.save(ignore_discard=True, ignore_expires=True)
        except Exception as exc:
            logger.debug("Could not save cookie jar: %s", exc)

    def as_header(self) -> str:
        parts = []
        for cookie in self._jar:
            parts.append(f"{cookie.name}={cookie.value}")
        return "; ".join(parts)


_COOKIE_STORE: Optional[CookieStore] = None


def get_cookie_store() -> CookieStore:
    global _COOKIE_STORE
    if _COOKIE_STORE is None:
        _COOKIE_STORE = CookieStore()
    return _COOKIE_STORE


def _build_headers(referer: Optional[str] = None) -> Dict[str, str]:
    profile = random.choice(USER_AGENT_PROFILES).copy()
    if referer:
        profile["Referer"] = referer
        profile["Sec-Fetch-Site"] = "same-origin"
    return profile


def fetch_plain_requests(url: str, timeout: int = 30, referer: Optional[str] = None) -> FetchResult:
    start = time.time()
    store = get_cookie_store()
    session = requests.Session()
    store.apply_to_requests(session)
    headers = _build_headers(referer=referer)
    proxies = _proxy_dict()
    try:
        _jitter_delay()
        response = session.get(url, headers=headers, timeout=timeout, allow_redirects=True, proxies=proxies)
        ttfb = time.time()
        html = response.text
        total = time.time()
        store.update_from_requests(session)
        cf = detect_cloudflare(html) or is_blocked_status(response.status_code, html)
        return FetchResult(
            url=str(response.url),
            html=html,
            status_code=response.status_code,
            method="requests",
            ttfb_ms=round((ttfb - start) * 1000, 2),
            total_ms=round((total - start) * 1000, 2),
            cf_detected=cf,
        )
    except Exception as exc:
        return FetchResult(url=url, html="", status_code=0, method="requests", error=str(exc))


def fetch_cloudscraper(url: str, timeout: int = 30, referer: Optional[str] = None) -> FetchResult:
    if not HAS_CLOUDSCRAPER:
        return FetchResult(url=url, html="", status_code=0, method="cloudscraper", error="cloudscraper not installed")
    start = time.time()
    headers = _build_headers(referer=referer)
    proxies = _proxy_dict()
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        _jitter_delay()
        response = scraper.get(url, headers=headers, timeout=timeout, proxies=proxies)
        ttfb = time.time()
        html = response.text
        total = time.time()
        cf = detect_cloudflare(html) or is_blocked_status(response.status_code, html)
        return FetchResult(
            url=str(response.url),
            html=html,
            status_code=response.status_code,
            method="cloudscraper",
            ttfb_ms=round((ttfb - start) * 1000, 2),
            total_ms=round((total - start) * 1000, 2),
            cf_detected=cf,
        )
    except Exception as exc:
        return FetchResult(url=url, html="", status_code=0, method="cloudscraper", error=str(exc))


def fetch_httpx(url: str, timeout: int = 30, referer: Optional[str] = None, http2: bool = True) -> FetchResult:
    if not HAS_HTTPX:
        return FetchResult(url=url, html="", status_code=0, method="httpx", error="httpx not installed")
    start = time.time()
    headers = _build_headers(referer=referer)
    proxy = _proxy_url()
    method_name = "httpx_http2" if http2 else "httpx_http1"
    try:
        with httpx.Client(http2=http2, headers=headers, proxy=proxy, follow_redirects=True, timeout=timeout) as client:
            _jitter_delay()
            response = client.get(url)
            ttfb = time.time()
            html = response.text
            total = time.time()
            cf = detect_cloudflare(html) or is_blocked_status(response.status_code, html)
            return FetchResult(
                url=str(response.url),
                html=html,
                status_code=response.status_code,
                method=method_name,
                ttfb_ms=round((ttfb - start) * 1000, 2),
                total_ms=round((total - start) * 1000, 2),
                cf_detected=cf,
            )
    except Exception as exc:
        return FetchResult(url=url, html="", status_code=0, method=method_name, error=str(exc))


def fetch_curl_cffi(
    url: str,
    timeout: int = 30,
    impersonate: str = "chrome120",
    referer: Optional[str] = None,
    http_version: Optional[Any] = None,
) -> FetchResult:
    if not HAS_CURL:
        return FetchResult(url=url, html="", status_code=0, method=f"curl_cffi:{impersonate}", error="curl_cffi not installed")
    start = time.time()
    headers = _build_headers(referer=referer)
    cookie_header = get_cookie_store().as_header()
    if cookie_header:
        headers["Cookie"] = cookie_header
    proxies = _proxy_dict()
    method_name = f"curl_cffi:{impersonate}"
    if http_version is not None:
        method_name += f":h{http_version}"
    try:
        kwargs: Dict[str, Any] = {
            "impersonate": impersonate,
            "headers": headers,
            "timeout": timeout,
            "allow_redirects": True,
        }
        if proxies:
            kwargs["proxies"] = proxies
        if http_version is not None:
            kwargs["http_version"] = http_version

        _jitter_delay()
        response = curl_requests.get(url, **kwargs)
        ttfb = time.time()
        html = response.text
        total = time.time()

        if hasattr(response, "cookies"):
            try:
                store = get_cookie_store()
                for name, value in response.cookies.items():
                    store.update_from_list(
                        [{"name": name, "value": value, "domain": ".mobilesentrix.com", "path": "/"}]
                    )
            except Exception:
                pass

        cf = detect_cloudflare(html) or is_blocked_status(response.status_code, html)
        return FetchResult(
            url=str(response.url),
            html=html,
            status_code=response.status_code,
            method=method_name,
            ttfb_ms=round((ttfb - start) * 1000, 2),
            total_ms=round((total - start) * 1000, 2),
            cf_detected=cf,
        )
    except Exception as exc:
        return FetchResult(url=url, html="", status_code=0, method=method_name, error=str(exc))


def fetch_curl_cffi_rotating(url: str, timeout: int = 30, referer: Optional[str] = None) -> FetchResult:
    """Try multiple curl_cffi TLS/browser fingerprints."""
    last = FetchResult(url=url, html="", status_code=0, method="curl_cffi_rotating", error="no profiles tried")
    for profile in CURL_IMPERSONATE_PROFILES:
        result = fetch_curl_cffi(url, timeout=timeout, impersonate=profile, referer=referer)
        if result.ok:
            result.method = f"curl_cffi_rotating:{profile}"
            return result
        last = result
        if result.status_code in (403, 429, 503):
            _backoff_delay(CURL_IMPERSONATE_PROFILES.index(profile))
    return last


def fetch_playwright(url: str, timeout: int = 30, referer: Optional[str] = None) -> FetchResult:
    if not HAS_PLAYWRIGHT:
        return FetchResult(url=url, html="", status_code=0, method="playwright", error="playwright not installed")
    start = time.time()
    proxy = _proxy_url()
    launch_kwargs: Dict[str, Any] = {"headless": True}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=USER_AGENT_PROFILES[0]["User-Agent"],
                locale="en-US",
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()
            if referer:
                page.set_extra_http_headers({"Referer": referer})
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(1500)
            html = page.content()
            final_url = page.url
            cookies = context.cookies()
            get_cookie_store().update_from_list(cookies)
            browser.close()
        ttfb = time.time()
        total = time.time()
        cf = detect_cloudflare(html)
        return FetchResult(
            url=final_url,
            html=html,
            status_code=200 if html and not cf else 403,
            method="playwright",
            ttfb_ms=round((ttfb - start) * 1000, 2),
            total_ms=round((total - start) * 1000, 2),
            cf_detected=cf,
        )
    except Exception as exc:
        return FetchResult(url=url, html="", status_code=0, method="playwright", error=str(exc))


def fetch_selenium(url: str, timeout: int = 30, referer: Optional[str] = None) -> FetchResult:
    if not HAS_SELENIUM:
        return FetchResult(url=url, html="", status_code=0, method="selenium", error="selenium not installed")
    start = time.time()
    driver = None
    try:
        options = ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"--user-agent={USER_AGENT_PROFILES[0]['User-Agent']}")
        proxy = _proxy_url()
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(timeout)
        if referer:
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {"Referer": referer}})
        _jitter_delay(0.1, 0.3)
        driver.get(url)
        time.sleep(2)
        html = driver.page_source
        final_url = driver.current_url
        cookies = driver.get_cookies()
        get_cookie_store().update_from_list(cookies)
        total = time.time()
        cf = detect_cloudflare(html)
        return FetchResult(
            url=final_url,
            html=html,
            status_code=200 if html and not cf else 403,
            method="selenium",
            ttfb_ms=round((total - start) * 1000, 2),
            total_ms=round((total - start) * 1000, 2),
            cf_detected=cf,
        )
    except Exception as exc:
        return FetchResult(url=url, html="", status_code=0, method="selenium", error=str(exc))
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def fetch_undetected_chrome(url: str, timeout: int = 30, referer: Optional[str] = None) -> FetchResult:
    if not HAS_UC:
        return FetchResult(url=url, html="", status_code=0, method="undetected_chrome", error="undetected-chromedriver not installed")
    start = time.time()
    driver = None
    try:
        options = uc.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"--user-agent={USER_AGENT_PROFILES[0]['User-Agent']}")
        proxy = _proxy_url()
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")
        driver = uc.Chrome(options=options, use_subprocess=True)
        driver.set_page_load_timeout(timeout)
        if referer:
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {"Referer": referer}})
        _jitter_delay(0.1, 0.3)
        driver.get(url)
        time.sleep(2)
        html = driver.page_source
        final_url = driver.current_url
        cookies = driver.get_cookies()
        get_cookie_store().update_from_list(cookies)
        ttfb = time.time()
        total = time.time()
        cf = detect_cloudflare(html)
        return FetchResult(
            url=final_url,
            html=html,
            status_code=200 if html and not cf else 403,
            method="undetected_chrome",
            ttfb_ms=round((ttfb - start) * 1000, 2),
            total_ms=round((total - start) * 1000, 2),
            cf_detected=cf,
        )
    except Exception as exc:
        return FetchResult(url=url, html="", status_code=0, method="undetected_chrome", error=str(exc))
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def fetch_flaresolverr(url: str, timeout: int = 60) -> FetchResult:
    """Use FlareSolverr if FLARESOLVERR_URL is set and service responds."""
    base = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
    start = time.time()
    payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}
    proxy = _proxy_url()
    if proxy:
        payload["proxy"] = {"url": proxy}
    try:
        probe = requests.get(base.replace("/v1", ""), timeout=3)
        if probe.status_code >= 500:
            return FetchResult(url=url, html="", status_code=0, method="flaresolverr", error="FlareSolverr unavailable")
    except Exception:
        return FetchResult(url=url, html="", status_code=0, method="flaresolverr", error="FlareSolverr not running")

    try:
        response = requests.post(base, json=payload, timeout=timeout + 10)
        data = response.json()
        if data.get("status") != "ok":
            return FetchResult(
                url=url,
                html="",
                status_code=0,
                method="flaresolverr",
                error=data.get("message", "FlareSolverr error"),
            )
        solution = data.get("solution", {})
        html = solution.get("response", "")
        status = int(solution.get("status", 0) or 0)
        final_url = solution.get("url", url)
        cookies = solution.get("cookies", [])
        if cookies:
            get_cookie_store().update_from_list(cookies)
        total = time.time()
        cf = detect_cloudflare(html)
        return FetchResult(
            url=final_url,
            html=html,
            status_code=status,
            method="flaresolverr",
            ttfb_ms=round((total - start) * 1000, 2),
            total_ms=round((total - start) * 1000, 2),
            cf_detected=cf,
        )
    except Exception as exc:
        return FetchResult(url=url, html="", status_code=0, method="flaresolverr", error=str(exc))


def warmup_session(homepage: str = HOMEPAGE_URL, timeout: int = 30) -> FetchResult:
    """Visit homepage to seed cookies before category/product requests."""
    logger.info("Warming up session via homepage: %s", homepage)
    return fetch_curl_cffi(homepage, timeout=timeout, impersonate="chrome120")


# Ordered fallback chain (fast methods first, browsers last)
DEFAULT_FETCH_CHAIN: List[Callable[..., FetchResult]] = []


def _init_default_chain() -> List[Callable[..., FetchResult]]:
    chain: List[Callable[..., FetchResult]] = []

    if HAS_CURL:
        chain.append(lambda url, timeout=30, referer=None: fetch_curl_cffi(url, timeout, "chrome120", referer))

    if HAS_CLOUDSCRAPER:
        chain.append(fetch_cloudscraper)

    if HAS_CURL:
        chain.append(lambda url, timeout=30, referer=None: fetch_curl_cffi(url, timeout, "chrome119", referer))
        chain.append(lambda url, timeout=30, referer=None: fetch_curl_cffi(url, timeout, "chrome116", referer))
        chain.append(lambda url, timeout=30, referer=None: fetch_curl_cffi(url, timeout, "chrome110", referer))
        chain.append(lambda url, timeout=30, referer=None: fetch_curl_cffi(url, timeout, "edge101", referer))
        chain.append(lambda url, timeout=30, referer=None: fetch_curl_cffi(url, timeout, "safari17_0", referer))
        chain.append(fetch_curl_cffi_rotating)

    if HAS_HTTPX:
        chain.append(lambda url, timeout=30, referer=None: fetch_httpx(url, timeout, referer, http2=True))
        chain.append(lambda url, timeout=30, referer=None: fetch_httpx(url, timeout, referer, http2=False))

    chain.append(fetch_plain_requests)

    if HAS_CURL:
        # HTTP version variants as late curl attempts
        try:
            from curl_cffi import CurlHttpVersion

            chain.append(
                lambda url, timeout=30, referer=None: fetch_curl_cffi(
                    url, timeout, "chrome120", referer, http_version=CurlHttpVersion.V2_0
                )
            )
            chain.append(
                lambda url, timeout=30, referer=None: fetch_curl_cffi(
                    url, timeout, "chrome120", referer, http_version=CurlHttpVersion.V1_1
                )
            )
        except Exception:
            pass

    chain.append(fetch_flaresolverr)

    if HAS_PLAYWRIGHT:
        chain.append(fetch_playwright)

    if HAS_SELENIUM:
        chain.append(fetch_selenium)

    if HAS_UC:
        chain.append(fetch_undetected_chrome)

    return chain


def get_default_chain() -> List[Callable[..., FetchResult]]:
    global DEFAULT_FETCH_CHAIN
    if not DEFAULT_FETCH_CHAIN:
        DEFAULT_FETCH_CHAIN = _init_default_chain()
    return DEFAULT_FETCH_CHAIN


def fetch_with_fallback(
    url: str,
    timeout: int = 30,
    referer: Optional[str] = None,
    warmup: bool = True,
    max_retries: int = 2,
    chain: Optional[List[Callable[..., FetchResult]]] = None,
) -> FetchResult:
    """
    Try each fetch method in chain until one returns valid content.
    Retries transient 403/429/503 with exponential backoff per method.
    """
    methods = chain or get_default_chain()
    parsed = urlparse(url)
    site_referer = referer or (f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc else HOMEPAGE_URL)

    if warmup and parsed.netloc and "mobilesentrix" in parsed.netloc:
        warm = warmup_session(f"{parsed.scheme}://{parsed.netloc}/", timeout=timeout)
        if warm.ok:
            logger.debug("Session warmup succeeded via %s", warm.method)

    last_result = FetchResult(url=url, html="", status_code=0, method="none", error="all methods failed")

    for method_fn in methods:
        method_name = getattr(method_fn, "__name__", "lambda")
        for attempt in range(max_retries + 1):
            try:
                result = method_fn(url, timeout=timeout, referer=site_referer)
            except TypeError:
                # Some methods don't accept referer
                result = method_fn(url, timeout=timeout)
            except Exception as exc:
                result = FetchResult(url=url, html="", status_code=0, method=method_name, error=str(exc))

            if result.ok:
                logger.info(
                    "Fetch OK via %s for %s (status=%s, %d bytes)",
                    result.method,
                    url,
                    result.status_code,
                    len(result.html),
                )
                return result

            last_result = result
            if result.status_code in (403, 429, 503) and attempt < max_retries:
                logger.debug(
                    "Retry %s attempt %d for %s (status=%s)",
                    result.method,
                    attempt + 1,
                    url,
                    result.status_code,
                )
                _backoff_delay(attempt)
            else:
                break

        logger.debug(
            "Method %s failed for %s: status=%s cf=%s err=%s",
            result.method,
            url,
            result.status_code,
            result.cf_detected,
            result.error,
        )

    return last_result


def fetch_html(url: str, timeout: int = 30, warmup: bool = True) -> str:
    """Convenience wrapper used by mobilesentrix_scraper."""
    result = fetch_with_fallback(url, timeout=timeout, warmup=warmup)
    if not result.ok:
        raise RuntimeError(
            f"All fetch methods failed for {url} (last={result.method}, "
            f"status={result.status_code}, cf={result.cf_detected}, err={result.error})"
        )
    return result.html


def fetch_result_to_legacy_dict(result: FetchResult) -> Dict[str, Any]:
    """Convert FetchResult to scraper_engine get_html_with_timing shape."""
    return {
        "url": result.url,
        "html": result.html,
        "ttfb_ms": result.ttfb_ms,
        "total_ms": result.total_ms,
        "status_code": result.status_code,
        "cf_detected": result.cf_detected,
        "error": result.error,
        "method": result.method,
    }


# Individual method registry for bypass testing
BYPASS_METHODS: Dict[str, Callable[..., FetchResult]] = {}


def _register_bypass_methods() -> Dict[str, Callable[..., FetchResult]]:
    methods: Dict[str, Callable[..., FetchResult]] = {
        "requests": fetch_plain_requests,
        "cloudscraper": fetch_cloudscraper,
        "httpx_http2": lambda url, timeout=30, referer=None: fetch_httpx(url, timeout, referer, True),
        "httpx_http1": lambda url, timeout=30, referer=None: fetch_httpx(url, timeout, referer, False),
        "curl_cffi_rotating": fetch_curl_cffi_rotating,
        "playwright": fetch_playwright,
        "selenium": fetch_selenium,
        "undetected_chrome": fetch_undetected_chrome,
        "flaresolverr": fetch_flaresolverr,
        "warmup_homepage": lambda url, timeout=30, referer=None: warmup_session(
            HOMEPAGE_URL, timeout
        ),
    }
    for profile in CURL_IMPERSONATE_PROFILES:
        methods[f"curl_cffi_{profile}"] = (
            lambda url, timeout=30, referer=None, p=profile: fetch_curl_cffi(url, timeout, p, referer)
        )
    return methods


def get_bypass_methods() -> Dict[str, Callable[..., FetchResult]]:
    global BYPASS_METHODS
    if not BYPASS_METHODS:
        BYPASS_METHODS = _register_bypass_methods()
    return BYPASS_METHODS


def build_curl_session(impersonate: str = "chrome120", verify_ssl: bool = True):
    """Build a curl_cffi session for scraper_engine compatibility."""
    if not HAS_CURL:
        session = requests.Session()
        session.verify = verify_ssl
        session._is_curl_session = False  # type: ignore[attr-defined]
        return session, False

    session = curl_requests.Session(impersonate=impersonate)
    session.verify = verify_ssl
    session._is_curl_session = True  # type: ignore[attr-defined]
    session._impersonate = impersonate  # type: ignore[attr-defined]
    return session, True
