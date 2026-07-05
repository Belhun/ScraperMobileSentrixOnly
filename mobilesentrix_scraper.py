"""
MobileSentrix end-to-end scraper.

Fetches category listings and product detail pages using curl_cffi to bypass
bot protection on mobilesentrix.com.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from fetch_layer import HAS_CURL, fetch_html as _fetch_html_layer, fetch_with_fallback
PARSER = "lxml"
SCREEN_ASSEMBLY_RE = re.compile(r"(OLED|LCD).*(Assembly|assembly)", re.IGNORECASE)
BADGE_PATH_RE = re.compile(r"/(?:wysiwyg|opt-badges|Badges)/", re.IGNORECASE)

BADGE_ALIASES = {
    "aftermarket": "AM",
    "oem pull a": "PULL A",
    "aq7 aftermarket": "AQ7",
    "x07 aftermarket pro": "XO7 2.0",
    "assembled": "ASSEMBLED",
    "refurbished": "REFURB",
    "service pack": "SERVICE PACK",
    "apple genuine": "GENUINE Apple",
}


@dataclass
class CategoryProduct:
    title: str
    url: str
    price_text: str = ""
    price_value: Optional[float] = None
    badge: str = ""
    badge_label: str = ""
    compatible_title: str = ""
    color_info: str = ""
    image_url: str = ""
    category_url: str = ""
    page_number: int = 1


@dataclass
class ProductDetail:
    title: str
    url: str
    sku: str = ""
    price_text: str = ""
    price_value: Optional[float] = None
    rating: str = ""
    color_info: str = ""
    badge: str = ""
    description_bullets: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    image_urls: List[str] = field(default_factory=list)
    stock_status: str = ""
    category_path: str = ""
    scraped_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_price(text: str) -> tuple[Optional[float], str]:
    if not text:
        return None, ""
    match = re.search(r"\$?\s*([\d,]+\.?\d*)", text.replace(",", ""))
    if not match:
        return None, clean_text(text)
    try:
        return float(match.group(1)), clean_text(text)
    except ValueError:
        return None, clean_text(text)


def normalize_badge(alt_text: str) -> str:
    raw = clean_text(alt_text)
    if raw.lower().startswith("quality - "):
        raw = raw[10:].strip()
    return BADGE_ALIASES.get(raw.lower(), raw)


def _jsonld_product(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue

        candidates: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            candidates.append(data)
            graph = data.get("@graph")
            if isinstance(graph, list):
                candidates.extend(item for item in graph if isinstance(item, dict))
        elif isinstance(data, list):
            candidates.extend(item for item in data if isinstance(item, dict))

        for obj in candidates:
            obj_type = obj.get("@type")
            if obj_type == "Product" or (
                isinstance(obj_type, list) and "Product" in obj_type
            ):
                return obj
    return None


def extract_sku(soup: BeautifulSoup, jsonld: Optional[Dict[str, Any]] = None) -> str:
    sku_el = soup.select_one("span[itemprop='sku'], .copy-text[itemprop='sku']")
    if sku_el:
        raw = clean_text(sku_el.get_text())
        return re.sub(r"(?i)copy$", "", raw).strip()

    sku_block = soup.select_one(".skudisplayweb, .skudisplayMobile")
    if sku_block:
        raw = clean_text(sku_block.get_text())
        raw = re.sub(r"(?i)^sku", "", raw)
        return re.sub(r"(?i)copy$", "", raw).strip()

    if jsonld and jsonld.get("sku"):
        return clean_text(str(jsonld["sku"]))
    return ""


def extract_color_info(container: BeautifulSoup) -> str:
    color_img = container.select_one(
        ".detail-cl-badges img, .cl-badges img, img[alt*='Color' i]"
    )
    if color_img:
        alt = clean_text(color_img.get("alt", ""))
        if alt:
            return alt.upper() if alt.lower() == "all colors" else alt
    return ""


def extract_badge_label(container: BeautifulSoup) -> str:
    badge_img = container.select_one(
        "img.badgesImg, img.product-badges, img.product-budges, .media-badges-r img"
    )
    if badge_img:
        return clean_text(badge_img.get("alt", "") or badge_img.get("title", ""))
    return ""


def extract_badge(container: BeautifulSoup) -> str:
    return normalize_badge(extract_badge_label(container))


def fetch_html(url: str, timeout: int = 30, warmup: bool = True) -> str:
    """Fetch page HTML using layered bypass fallbacks."""
    return _fetch_html_layer(url, timeout=timeout, warmup=warmup)


def get_last_fetch_method(url: str, timeout: int = 30) -> str:
    """Return which bypass method succeeded for debugging."""
    result = fetch_with_fallback(url, timeout=timeout, warmup=True)
    return result.method if result.ok else f"failed:{result.method}"


def category_page_url(category_url: str, page_number: int) -> str:
    """Build paginated category URL (?p=N). Page 1 uses the base URL."""
    if page_number <= 1:
        return category_url

    parsed = urlparse(category_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["p"] = [str(page_number)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )


def parse_category_products(
    soup: BeautifulSoup,
    category_url: str,
    page_number: int = 1,
) -> List[CategoryProduct]:
    products: List[CategoryProduct] = []
    seen_urls: set[str] = set()

    container = soup.select_one(
        ".products-grid, .category-products, ul.product-listing, .products"
    )
    cards = container.select("li.item") if container else soup.select("li.item")

    for card in cards:
        link = card.select_one("a.product-image[href], a[href]")
        if not link:
            continue

        href = link.get("href", "").strip()
        if not href or href.startswith("#"):
            continue

        product_url = urljoin(category_url, href)
        if product_url in seen_urls:
            continue
        seen_urls.add(product_url)

        compatible_title = clean_text(link.get("title", ""))
        title_el = card.select_one("h2.product-name, .product-name")
        title = clean_text(title_el.get_text()) if title_el else ""
        if not title:
            title = compatible_title or clean_text(link.get_text())
        if not title:
            continue
        if not compatible_title:
            compatible_title = title

        price_text = ""
        price_value = None
        price_block = card.select_one(".price-qty-block, .price-box")
        if price_block:
            amount = price_block.select_one("span.regular-price, span.price, .amount")
            price_text = clean_text(amount.get_text()) if amount else ""
            price_value, price_text = parse_price(price_text)

        image_url = ""
        for img in card.select("img.small-img, img.lazyimage, img[data-original], img[src]"):
            src = img.get("data-original") or img.get("src") or ""
            if src and not BADGE_PATH_RE.search(src) and "All-Colors" not in src:
                image_url = urljoin(category_url, src)
                break

        badge_label = extract_badge_label(card)

        products.append(
            CategoryProduct(
                title=title,
                url=product_url,
                price_text=price_text,
                price_value=price_value,
                badge=normalize_badge(badge_label),
                badge_label=badge_label,
                compatible_title=compatible_title,
                color_info=extract_color_info(card),
                image_url=image_url,
                category_url=category_url,
                page_number=page_number,
            )
        )

    return products


def filter_screen_assemblies(
    products: List[CategoryProduct],
    min_price: float = 13.93,
    max_price: float = 300.60,
) -> List[CategoryProduct]:
    filtered: List[CategoryProduct] = []
    for product in products:
        if not SCREEN_ASSEMBLY_RE.search(product.title):
            continue
        if product.price_value is None:
            filtered.append(product)
            continue
        if min_price <= product.price_value <= max_price:
            filtered.append(product)
    return filtered


def parse_product_detail(soup: BeautifulSoup, product_url: str) -> ProductDetail:
    jsonld = _jsonld_product(soup) or {}

    title_el = soup.select_one("h1.page-title span, h1.page-title, h1")
    title = clean_text(title_el.get_text()) if title_el else ""
    title = re.sub(r"(?i)\bcopy$", "", title).strip()
    if not title:
        title = clean_text(jsonld.get("name", ""))

    price_text = ""
    price_value = None

    meta_price = soup.select_one("meta[itemprop='price']")
    if meta_price and meta_price.get("content"):
        try:
            price_value = float(meta_price["content"])
            price_text = f"${price_value:.2f}"
        except ValueError:
            pass

    if price_value is None:
        offers = jsonld.get("offers")
        if isinstance(offers, dict) and offers.get("price") is not None:
            try:
                price_value = float(offers["price"])
                price_text = f"${price_value:.2f}"
            except (TypeError, ValueError):
                pass

    if price_value is None:
        price_box = soup.select_one(
            ".product-info-main .price-box .price, .product-info-price .price"
        )
        if price_box:
            price_text = clean_text(price_box.get_text())
            price_value, price_text = parse_price(price_text)

    rating = ""
    rating_el = soup.select_one(".tot_review, h4.tot_review")
    if rating_el:
        rating = clean_text(rating_el.get_text())

    if not rating:
        agg = jsonld.get("aggregateRating")
        if isinstance(agg, dict) and agg.get("ratingValue") is not None:
            try:
                rating_value = float(agg["ratingValue"])
                count = agg.get("reviewCount", "")
                rating = f"{rating_value} Out Of 5 Rating ({count} reviews)"
            except (TypeError, ValueError):
                pass

    description_bullets: List[str] = []
    for selector in (
        ".product-description .std li",
        ".box-description .std li",
        ".std ul li",
    ):
        for bullet in soup.select(selector):
            text = clean_text(bullet.get_text())
            if text and text not in description_bullets:
                description_bullets.append(text)
        if description_bullets:
            break

    tags: List[str] = []
    for tag_el in soup.select(".product-tags a, .tags a"):
        tag_text = clean_text(tag_el.get_text())
        if tag_text and tag_text not in tags:
            tags.append(tag_text)

    image_urls: List[str] = []
    seen_images: set[str] = set()
    for img in soup.select(
        ".gallery-placeholder img, .fotorama img, .product-image-photo, "
        ".product-image img, .MagicToolboxContainer img"
    ):
        src = img.get("data-src") or img.get("src") or ""
        if not src or BADGE_PATH_RE.search(src) or "All-Colors" in src:
            continue
        full_src = urljoin(product_url, src)
        if full_src not in seen_images:
            seen_images.add(full_src)
            image_urls.append(full_src)

    if not image_urls:
        json_image = jsonld.get("image")
        if isinstance(json_image, str):
            image_urls = [json_image]
        elif isinstance(json_image, list):
            image_urls = [img for img in json_image if isinstance(img, str)]

    category_path = ""
    breadcrumbs = soup.select(".breadcrumbs a")
    if breadcrumbs:
        category_path = " > ".join(clean_text(a.get_text()) for a in breadcrumbs[1:])

    return ProductDetail(
        title=title,
        url=product_url,
        sku=extract_sku(soup, jsonld),
        price_text=price_text,
        price_value=price_value,
        rating=rating,
        color_info=extract_color_info(soup),
        badge=extract_badge(soup.select_one(".product.media") or soup) or extract_badge(soup),
        description_bullets=description_bullets,
        tags=tags,
        image_urls=image_urls,
        stock_status="",
        category_path=category_path,
        scraped_at=datetime.now(timezone.utc).isoformat(),
        metadata={"host": urlparse(product_url).hostname or ""},
    )


def scrape_category_pages(
    category_url: str,
    max_pages: Optional[int] = None,
    delay_seconds: float = 0.2,
) -> List[CategoryProduct]:
    """Scrape all pagination pages for a category listing."""
    all_products: List[CategoryProduct] = []
    seen_urls: set[str] = set()
    page_number = 1

    while max_pages is None or page_number <= max_pages:
        page_url = category_page_url(category_url, page_number)
        html = fetch_html(page_url, warmup=(page_number == 1))
        soup = BeautifulSoup(html, PARSER)
        page_products = parse_category_products(soup, category_url, page_number=page_number)

        new_products = [p for p in page_products if p.url not in seen_urls]
        if not new_products:
            break

        for product in new_products:
            seen_urls.add(product.url)
            all_products.append(product)

        page_number += 1
        if delay_seconds:
            time.sleep(delay_seconds)

    return all_products


def scrape_category(
    category_url: str,
    screen_assemblies_only: bool = False,
    max_pages: Optional[int] = None,
    paginate: bool = True,
) -> List[CategoryProduct]:
    if paginate:
        products = scrape_category_pages(category_url, max_pages=max_pages)
    else:
        html = fetch_html(category_url)
        soup = BeautifulSoup(html, PARSER)
        products = parse_category_products(soup, category_url, page_number=1)

    if screen_assemblies_only:
        products = filter_screen_assemblies(products)
    return products


def scrape_product_detail(product_url: str) -> ProductDetail:
    html = fetch_html(product_url)
    soup = BeautifulSoup(html, PARSER)
    return parse_product_detail(soup, product_url)


# Backwards-compatible alias
scrape_product = scrape_product_detail


def scrape_category_with_details(
    category_url: str,
    screen_assemblies_only: bool = False,
    max_details: Optional[int] = None,
    max_pages: Optional[int] = None,
    paginate: bool = True,
    delay_seconds: float = 0.15,
) -> Dict[str, Any]:
    category_products = scrape_category(
        category_url,
        screen_assemblies_only=screen_assemblies_only,
        max_pages=max_pages,
        paginate=paginate,
    )
    details: List[ProductDetail] = []

    targets = category_products[:max_details] if max_details else category_products

    for product in targets:
        try:
            detail = scrape_product_detail(product.url)
            if not detail.price_value and product.price_value:
                detail.price_value = product.price_value
                detail.price_text = product.price_text
            if not detail.badge and product.badge:
                detail.badge = product.badge
            if not detail.color_info and product.color_info:
                detail.color_info = product.color_info
            details.append(detail)
        except Exception as exc:
            details.append(
                ProductDetail(
                    title=product.title,
                    url=product.url,
                    price_text=product.price_text,
                    price_value=product.price_value,
                    badge=product.badge,
                    color_info=product.color_info,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    metadata={"error": str(exc)},
                )
            )
        if delay_seconds:
            time.sleep(delay_seconds)

    page_numbers = sorted({p.page_number for p in category_products})

    return {
        "category_url": category_url,
        "listing_count": len(category_products),
        "pages_scraped": len(page_numbers),
        "listings": [asdict(p) for p in category_products],
        "details": [asdict(d) for d in details],
        "category_product_count": len(category_products),
        "category_products": [asdict(p) for p in category_products],
        "details_scraped": len(details),
        "product_details": [asdict(d) for d in details],
    }


def to_json(data: Dict[str, Any], indent: int = 2) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False)
