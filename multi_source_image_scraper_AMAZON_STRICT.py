#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
multi_source_image_scraper_strict_v2.py

STRICT multi-source product image scraper/downloader for ecommerce listings. This version avoids random banners/ads/recommendation images.

Supported sources:
- Hyperpure
- BigBasket
- Amazon India
- JioMart
- Flipkart

Install:
    py -m pip install pandas openpyxl requests beautifulsoup4 rapidfuzz pillow imagehash playwright tqdm
    py -m playwright install chromium

Run:
    py multi_source_image_scraper.py "sauces_and_seasoning_section.xlsx"

Useful:
    py multi_source_image_scraper.py "sauces_and_seasoning_section.xlsx" --limit 10
    py multi_source_image_scraper.py "sauces_and_seasoning_section.xlsx" --no-download
    py multi_source_image_scraper.py "sauces_and_seasoning_section.xlsx" --sources hyperpure,bigbasket,amazon
    py multi_source_image_scraper.py "sauces_and_seasoning_section.xlsx" --headful

Input columns accepted:
- Product Name / Name / Title
- Brand
- SKU / ID / Product ID / Code (optional)
- Slug / URL Slug / Product URL (optional)
- Image URL (optional)

Outputs:
- image_scraper_output/image_links.xlsx
- image_scraper_output/catalog_review.xlsx
- image_scraper_output/download_report.xlsx
- image_scraper_output/images/<product folders>/
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import io
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from PIL import Image
from rapidfuzz import fuzz
from tqdm import tqdm

try:
    import imagehash
except Exception:
    imagehash = None

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_SOURCES = ["hyperpure", "bigbasket", "amazon", "jiomart", "flipkart"]
SOURCE_CODE = {"hyperpure": "h", "bigbasket": "b", "amazon": "a", "jiomart": "j", "flipkart": "f"}
SOURCE_LABEL = {"hyperpure": "Hyperpure", "bigbasket": "BigBasket", "amazon": "Amazon", "jiomart": "JioMart", "flipkart": "Flipkart"}
DOMAINS = {"hyperpure": "hyperpure.com", "bigbasket": "bigbasket.com", "amazon": "amazon.in", "jiomart": "jiomart.com", "flipkart": "flipkart.com"}
SEARCH_URLS = {
    "hyperpure": ["https://www.hyperpure.com/in/search?query={query}"],
    "bigbasket": ["https://www.bigbasket.com/ps/?q={query}"],
    "amazon": ["https://www.amazon.in/s?k={query}"],
    "jiomart": ["https://www.jiomart.com/search/{query}?text={query}"],
    "flipkart": ["https://www.flipkart.com/search?q={query}"],
}

GENERIC_WORDS = {
    "the", "and", "or", "with", "without", "pack", "of", "for", "food", "foods",
    "fresh", "premium", "classic", "professional", "original", "new", "best",
    "kg", "gm", "g", "ml", "l", "ltr", "litre", "liter", "jar", "bottle",
    "pc", "pcs", "piece", "pieces", "sachet", "sachets"
}
LOW_WEIGHT_WORDS = {
    "powder": 0.45, "whole": 0.45, "masala": 0.45, "sauce": 0.45, "paste": 0.45,
    "dressing": 0.45, "dip": 0.45, "professional": 0.45, "premium": 0.35,
    "small": 0.65, "big": 0.65, "red": 0.70, "green": 0.70,
    "black": 0.70, "white": 0.70, "yellow": 0.70
}
SYNONYMS = {
    "amchoor": {"amchur", "amchoor", "aamchur", "drymango", "mango"},
    "amchur": {"amchur", "amchoor", "aamchur", "drymango", "mango"},
    "chilli": {"chilli", "chili", "mirch"},
    "chili": {"chilli", "chili", "mirch"},
    "ketchup": {"ketchup", "catsup"},
    "mayonnaise": {"mayonnaise", "mayo"},
    "mayo": {"mayonnaise", "mayo"},
    "soyabean": {"soyabean", "soya", "soy"},
    "soya": {"soyabean", "soya", "soy"},
    "soy": {"soyabean", "soya", "soy"},
    "barbeque": {"barbeque", "bbq", "barbecue"},
    "bbq": {"barbeque", "bbq", "barbecue"},
    "cheesy": {"cheesy", "cheese"},
    "cheese": {"cheesy", "cheese"},
    "imli": {"imli", "tamarind"},
    "tamarind": {"imli", "tamarind"},
    "keora": {"keora", "kewra"},
    "kewra": {"keora", "kewra"},
    "peri": {"peri", "piri"},
    "piri": {"peri", "piri"},
}
BAD_IMAGE_WORDS = ["sprite", "loader", "placeholder", "blank", "logo", "icon", "favicon", "banner", "transparent", "play-icon", "stars", "rating", "cart", "default"]
PAGE_TIMEOUT_MS = 25000
REQUEST_TIMEOUT = 30

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})

@dataclass
class ProductRequest:
    row_index: int
    sku: str
    product_name: str
    brand: str
    slug: str = ""
    input_image_url: str = ""

@dataclass
class Candidate:
    source: str
    title: str
    brand: str
    url: str
    card_text: str = ""
    image_urls: List[str] = field(default_factory=list)
    score: float = 0.0
    details: str = ""

@dataclass
class DownloadRow:
    sku: str
    product_name: str
    brand: str
    source: str
    image_url: str
    saved_file: str
    width: int = 0
    height: int = 0
    duplicate_skipped: bool = False
    error: str = ""

# ---------------------------- helpers ----------------------------

def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(output_dir / "scraper.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )

def sleep_polite(a: float = 0.4, b: float = 1.2) -> None:
    time.sleep(random.uniform(a, b))

def safe_filename(text: str, max_len: int = 120) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "_", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:max_len] or "unknown")

def normalize(text: str) -> str:
    text = str(text or "").lower()
    reps = {
        "&": " and ", "amchoor": "amchur", "aamchur": "amchur", "grams": "g", "gram": "g", "gms": "g", "gm": "g",
        "kgs": "kg", "kilograms": "kg", "kilogram": "kg", "litres": "l", "litre": "l", "liter": "l", "liters": "l", "ltr": "l",
        "millilitre": "ml", "milliliter": "ml", "chilly": "chilli", "chili": "chilli", "barbecue": "barbeque", "bbq": "barbeque",
        "mayonaisse": "mayonnaise", "mayo": "mayonnaise", "soy sauce": "soya sauce", "soyabean sauce": "soya sauce",
        "tomato catsup": "tomato ketchup", "piri piri": "peri peri",
    }
    for old, new in reps.items():
        text = text.replace(old, new)
    text = re.sub(r"(\d)\s+(kg|g|ml|l)\b", r"\1\2", text)
    text = re.sub(r"[^a-z0-9.*+ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def full_token_present(token: str, haystack: str) -> bool:
    token = normalize(token)
    haystack = normalize(haystack)
    if not token:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", haystack) is not None

def token_found(token: str, haystack: str) -> bool:
    if full_token_present(token, haystack):
        return True
    for syn in SYNONYMS.get(normalize(token), set()):
        if full_token_present(syn, haystack):
            return True
    return False

def words(text: str) -> List[str]:
    return [x for x in normalize(text).split() if x]

def qty_tokens(text: str) -> set[str]:
    text = normalize(text)
    out: set[str] = set()
    def canon(num: str) -> str:
        try:
            f = float(num)
            return str(int(f)) if f.is_integer() else str(f).rstrip("0").rstrip(".")
        except Exception:
            return str(num)
    for a, b, unit in re.findall(r"(\d+(?:\.\d+)?)\s*\+\s*(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", text):
        if unit == "kg": out.add(canon(str((float(a) + float(b)) * 1000)) + "g")
        elif unit == "l": out.add(canon(str((float(a) + float(b)) * 1000)) + "ml")
        else: out.update({canon(a) + "+" + canon(b) + unit, canon(str(float(a) + float(b))) + unit})
    for a, b in re.findall(r"(\d+)\s*[*x]\s*(\d+)", text):
        out.add(f"{a}*{b}")
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", text):
        if unit == "kg": out.update({canon(str(float(num) * 1000)) + "g", canon(num) + "kg"})
        elif unit == "l": out.update({canon(str(float(num) * 1000)) + "ml", canon(num) + "l"})
        else: out.add(canon(num) + unit)
    for num in re.findall(r"(?<!\d)\.(\d+)\s*g\b", text):
        out.add("0." + num + "g")
    for num in re.findall(r"(\d+)\s*(?:pc|pcs|piece|pieces)\b", text):
        out.add(f"{num}pc")
    return out

def remove_qty(text: str) -> str:
    text = normalize(text)
    text = re.sub(r"\d+(?:\.\d+)?\s*\+\s*\d+(?:\.\d+)?\s*(kg|g|ml|l)\b", " ", text)
    text = re.sub(r"\d+\s*[*x]\s*\d+", " ", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*(kg|g|ml|l)\b", " ", text)
    text = re.sub(r"(?<!\d)\.\d+\s*g\b", " ", text)
    text = re.sub(r"\d+\s*(?:pc|pcs|piece|pieces)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def product_tokens(product_name: str, brand: str = "") -> List[str]:
    brand_words = set(words(brand))
    toks = []
    for tok in re.split(r"[^a-z0-9]+", remove_qty(product_name)):
        if tok and not tok.isdigit() and tok not in brand_words and tok not in GENERIC_WORDS and tok not in toks:
            toks.append(tok)
    return toks

def token_weight(tok: str) -> float:
    return LOW_WEIGHT_WORDS.get(tok, 1.0)

def brand_match(brand: str, candidate_text: str) -> bool:
    brand = str(brand or "").strip()
    if not brand:
        return True
    n_text = normalize(candidate_text)
    n_brand = normalize(brand)
    if full_token_present(n_brand, n_text):
        return True
    b_words = [w for w in words(brand) if w not in {"and", "the", "foods", "food", "pvt", "ltd"}]
    if len(b_words) >= 2:
        return all(full_token_present(w, n_text) for w in b_words[:2])
    return bool(b_words and full_token_present(b_words[0], n_text))

def match_score(product: ProductRequest, candidate_title: str, candidate_brand: str = "", candidate_url: str = "") -> Tuple[float, str]:
    combined = f"{candidate_brand} {candidate_title} {candidate_url}"
    if not brand_match(product.brand, combined):
        return 0.0, f"brand mismatch: needed={product.brand}"
    toks = product_tokens(product.product_name, product.brand)
    if not toks:
        return 0.0, "no product tokens"
    matched, missing = [], []
    total = sum(token_weight(t) for t in toks)
    got = 0.0
    for t in toks:
        if token_found(t, combined):
            matched.append(t); got += token_weight(t)
        else:
            missing.append(t)
    token_pct = (got / total) * 100 if total else 0.0
    fuzzy = fuzz.token_set_ratio(normalize(remove_qty(product.product_name)), normalize(remove_qty(candidate_title)))
    q_req = qty_tokens(product.product_name)
    q_cand = qty_tokens(candidate_title + " " + candidate_url)
    qty_ok = bool(q_req and (q_req & q_cand))
    score = (token_pct * 0.55) + (fuzzy * 0.35) + (15 if qty_ok else 0) + (5 if candidate_brand and brand_match(product.brand, candidate_brand) else 0)
    core = [t for t in toks if token_weight(t) >= 1.0]
    if core and not any(t in matched for t in core):
        score = 0.0
    details = f"score={score:.1f}; token_pct={token_pct:.1f}; fuzzy={fuzzy:.1f}; matched={matched}; missing={missing}; qty_req={sorted(q_req)}; qty_cand={sorted(q_cand)}; qty_ok={qty_ok}"
    return score, details

def fix_url(url: str, base: str = "") -> str:
    url = html_lib.unescape(str(url or "").strip()).replace("\\/", "/")
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        base_map = {"hyperpure": "https://www.hyperpure.com", "bigbasket": "https://www.bigbasket.com", "amazon": "https://www.amazon.in", "jiomart": "https://www.jiomart.com", "flipkart": "https://www.flipkart.com"}
        return urljoin(base_map.get(base, ""), url)
    return url

def clean_image_url(url: str, source: str) -> str:
    url = fix_url(url, source)
    if source == "amazon":
        url = re.sub(r"\._[A-Z0-9_,]+_\.", ".", url)
    if source == "bigbasket":
        url = url.replace("/p/s/", "/p/l/").replace("/p/m/", "/p/l/")
    if source == "flipkart":
        url = re.sub(r"/\d+/\d+/", "/832/832/", url)
    return url

def image_url_allowed(url: str, source: str) -> bool:
    low = str(url or "").lower()
    if not low.startswith("http") or any(b in low for b in BAD_IMAGE_WORDS):
        return False
    if source == "hyperpure": return "assets.hyperpure.com" in low and "/data/images/products/" in low
    if source == "bigbasket": return "bbassets.com" in low and "/media/uploads/p/" in low
    if source == "amazon": return ("media-amazon" in low or "ssl-images-amazon" in low) and "/images/i/" in low
    if source == "jiomart": return "jiomart.com" in low and ("images/product" in low or "/images/" in low)
    if source == "flipkart": return ("rukminim" in low or "flixcart.com" in low) and "image" in low
    return True

def unique_keep_order(items: Iterable[str]) -> List[str]:
    out, seen = [], set()
    for x in items:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out

# ---------------------------- input ----------------------------

def find_column(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for n in names:
        if n.strip().lower() in lookup:
            return lookup[n.strip().lower()]
    return None

def read_products(input_file: Path, limit: Optional[int] = None) -> List[ProductRequest]:
    df = pd.read_excel(input_file)
    name_col = find_column(df, ["Product Name", "Name", "Title", "product_name"])
    brand_col = find_column(df, ["Brand", "brand"])
    sku_col = find_column(df, ["SKU", "ID", "Product ID", "Reference ID", "Model", "Code"])
    slug_col = find_column(df, ["Slug", "slug", "Product URL", "URL Slug", "url_slug"])
    image_col = find_column(df, ["Image URL", "Image", "Main Image", "URL"])
    if not name_col:
        raise ValueError("Input Excel must contain Product Name / Name / Title column.")
    out = []
    for i, row in df.iterrows():
        name = str(row.get(name_col, "") or "").strip()
        if not name or name.lower() == "nan":
            continue
        brand = str(row.get(brand_col, "") or "").strip() if brand_col else ""
        sku = str(row.get(sku_col, "") or "").strip() if sku_col else str(i + 1)
        slug = str(row.get(slug_col, "") or "").strip() if slug_col else ""
        img = str(row.get(image_col, "") or "").strip() if image_col else ""
        out.append(ProductRequest(i + 2, sku if sku and sku.lower() != "nan" else str(i+1), name, "" if brand.lower()=="nan" else brand, "" if slug.lower()=="nan" else slug, "" if img.lower()=="nan" else img))
        if limit and len(out) >= limit:
            break
    return out

# ---------------------------- playwright ----------------------------

def require_playwright():
    if sync_playwright is None:
        raise RuntimeError("Playwright not installed. Run: py -m pip install playwright && py -m playwright install chromium")

def open_page(context, url: str, wait_ms: int = 2500):
    page = context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(wait_ms)
        return page
    except Exception as e:
        logging.warning("Could not open %s: %s", url, e)
        try: page.close()
        except Exception: pass
        return None

def page_text(page) -> str:
    parts = []
    try: parts.append(page.title())
    except Exception: pass
    for sel in ["h1", "#productTitle", ".product-title", "[data-testid*=title]", "meta[property='og:title']", "meta[name='description']", "meta[property='og:description']"]:
        try:
            if sel.startswith("meta"):
                vals = page.locator(sel).evaluate_all("(els) => els.map(e => e.content || '')")
            else:
                vals = page.locator(sel).evaluate_all("(els) => els.map(e => e.innerText || '')")
            parts.extend([str(v) for v in vals if v])
        except Exception: pass
    try: parts.append(page.url)
    except Exception: pass
    return " ".join(parts)

# ---------------------------- search ----------------------------

def product_link_allowed(url: str, source: str) -> bool:
    low = url.lower()
    if DOMAINS[source] not in urlparse(low).netloc:
        return False
    if any(x in low for x in ["/login", "/cart", "/privacy", "/terms", "/about", "/help", "/faq", "/account", "/checkout"]):
        return False
    if source == "hyperpure":
        parts = urlparse(low).path.strip("/").split("/")
        if len(parts) != 2 or parts[0] not in {"in", "en"}:
            return False
        bad_slugs = {"search", "cart", "login", "brands", "brand", "categories", "category", "offers", "deals"}
        if parts[1] in bad_slugs:
            return False
        return True
    if source == "bigbasket": return "/pd/" in low
    if source == "amazon": return "/dp/" in low
    if source == "jiomart": return "/p/" in low
    if source == "flipkart": return "/p/" in low or "pid=" in low
    return True

def extract_title_from_card_text(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text: return ""
    return re.split(r"(₹|MRP|ADD|Add|Out of Stock|Sponsored|Rating|Reviews)", text)[0].strip()[:220]

def href_to_title(href: str) -> str:
    p = urlparse(href).path.split("/")[-1] or urlparse(href).path
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", p)).strip()[:200]

def direct_hyperpure_candidate(product: ProductRequest) -> Optional[Candidate]:
    if not product.slug:
        return None
    url = product.slug if product.slug.startswith("http") else f"https://www.hyperpure.com/in/{product.slug.lstrip('/')}"
    imgs = [product.input_image_url] if product.input_image_url.startswith("http") else []
    sc, det = match_score(product, product.product_name, product.brand, url)
    return Candidate("hyperpure", product.product_name, product.brand, url, image_urls=imgs, score=sc, details="direct input slug/image; " + det)


def actual_product_title(page, source: str) -> str:
    """Extract only the real product title, not page body/recommendations."""
    selectors = []
    if source == "amazon":
        selectors = ["#productTitle", "span#productTitle", "meta[property='og:title']"]
    elif source == "bigbasket":
        selectors = ["h1", "[data-testid*='title']", ".Description___StyledH-sc-82a36a-2"]
    else:
        selectors = ["h1", "meta[property='og:title']", "[data-testid*='title']", ".product-title"]

    for sel in selectors:
        try:
            if sel.startswith("meta"):
                val = page.locator(sel).first.get_attribute("content") or ""
            else:
                val = page.locator(sel).first.inner_text(timeout=2500) or ""
            val = re.sub(r"\s+", " ", val).strip()
            if val:
                # Clean common title suffixes.
                val = re.sub(r"\s*:\s*Amazon\.in.*$", "", val, flags=re.I)
                val = re.sub(r"\s*-\s*Amazon\.in.*$", "", val, flags=re.I)
                val = re.sub(r"\s*\|\s*.*$", "", val).strip()
                return val[:260]
        except Exception:
            continue

    try:
        title = page.title()
        title = re.sub(r"\s*:\s*Amazon\.in.*$", "", title, flags=re.I)
        title = re.sub(r"\s*-\s*Amazon\.in.*$", "", title, flags=re.I)
        return re.sub(r"\s+", " ", title).strip()[:260]
    except Exception:
        return ""


def amazon_strict_accept(product: ProductRequest, title: str, url: str, base_score: float) -> Tuple[bool, str]:
    """
    Amazon must be stricter because search pages often contain sponsored/other-brand products.
    Rules:
    - Brand must be present in the real Amazon product title.
    - Main product tokens must be present in the real title.
    - Quantity must match when requested quantity exists.
    - Higher minimum score than other sources.
    """
    title_norm = normalize(title)
    if not title_norm:
        return False, "amazon reject: empty product title"

    if product.brand and not brand_match(product.brand, title):
        return False, f"amazon reject: brand not in title. needed={product.brand}; title={title}"

    toks = product_tokens(product.product_name, product.brand)
    core = [t for t in toks if token_weight(t) >= 1.0]
    matched_core = [t for t in core if token_found(t, title_norm)]
    required_core = min(2, len(core)) if core else 0

    if required_core and len(matched_core) < required_core:
        return False, f"amazon reject: weak product identity. matched_core={matched_core}; required={required_core}; title={title}"

    q_req = qty_tokens(product.product_name)
    q_cand = qty_tokens(title + " " + url)
    if q_req and not (q_req & q_cand):
        return False, f"amazon reject: quantity mismatch. needed={sorted(q_req)}; title_qty={sorted(q_cand)}; title={title}"

    if base_score < 82:
        return False, f"amazon reject: score too low {base_score:.1f}; title={title}"

    return True, "amazon strict accepted"


def search_amazon_source(context, product: ProductRequest, max_links: int) -> List[Candidate]:
    """
    Amazon-specific search parser.
    Uses only actual search-result cards with ASIN and product title.
    Does not collect random footer/category/recommendation links.
    """
    query = quote_plus(f"{product.brand} {product.product_name}".strip())
    search_url = SEARCH_URLS["amazon"][0].format(query=query)
    logging.info("Searching amazon STRICT: %s", search_url)

    page = open_page(context, search_url, 3500)
    if not page:
        return []

    try:
        for _ in range(2):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(800)
    except Exception:
        pass

    try:
        cards = page.locator("div[data-component-type='s-search-result'][data-asin]").evaluate_all("""
        (cards) => cards.map(card => {
            const asin = card.getAttribute('data-asin') || '';
            const linkEl =
                card.querySelector('h2 a[href*="/dp/"]') ||
                card.querySelector('a[href*="/dp/"]');

            const titleEl =
                card.querySelector('h2 span') ||
                card.querySelector('h2') ||
                card.querySelector('span.a-size-base-plus') ||
                card.querySelector('span.a-size-medium');

            const imgEl = card.querySelector('img.s-image');

            const title = titleEl ? (titleEl.innerText || titleEl.textContent || '') : '';
            const href = linkEl ? (linkEl.href || '') : '';
            const img = imgEl ? (imgEl.currentSrc || imgEl.src || '') : '';
            const text = (card.innerText || '').slice(0, 1500);
            return {asin, title, href, img, text};
        })
        """)
    except Exception:
        cards = []

    try:
        page.close()
    except Exception:
        pass

    found: List[Candidate] = []
    for item in cards:
        asin = str(item.get("asin") or "").strip()
        href = fix_url(item.get("href", ""), "amazon")
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        text_card = str(item.get("text") or "")

        if not asin or not href or not title:
            continue

        # Normalize Amazon URL to clean /dp/ASIN.
        href = f"https://www.amazon.in/dp/{asin}"

        # First search-card gate: brand/title/quantity must already look right.
        score, details = match_score(product, title, "", href)
        ok, why = amazon_strict_accept(product, title, href, score)
        if not ok:
            logging.info("%s", why)
            continue

        img = clean_image_url(str(item.get("img") or ""), "amazon")
        imgs = [img] if image_url_allowed(img, "amazon") else []

        found.append(Candidate(
            source="amazon",
            title=title,
            brand="",
            url=href,
            card_text=text_card,
            image_urls=imgs,
            score=score,
            details=details + "; " + why,
        ))

    # Highest score first, no duplicates.
    uniq: Dict[str, Candidate] = {}
    for c in found:
        if c.url not in uniq or c.score > uniq[c.url].score:
            uniq[c.url] = c

    return sorted(uniq.values(), key=lambda c: c.score, reverse=True)[:max_links]


def search_source(context, product: ProductRequest, source: str, max_links: int) -> List[Candidate]:
    if source == "amazon":
        return search_amazon_source(context, product, max_links)

    query = quote_plus(f"{product.brand} {product.product_name}".strip())
    found = []
    for template in SEARCH_URLS[source]:
        search_url = template.format(query=query)
        logging.info("Searching %s: %s", source, search_url)
        page = open_page(context, search_url, 3500)
        if not page: continue
        try:
            for _ in range(2):
                page.mouse.wheel(0, 2200); page.wait_for_timeout(800)
        except Exception: pass
        try:
            links = page.locator("a[href]").evaluate_all("""
            (anchors) => anchors.map(a => {
                const card = a.closest('article, li, [data-component-type], [data-testid], [class*=product], [class*=Product], [class*=card], [class*=Card], div');
                const text = ((card && card.innerText) || a.innerText || a.getAttribute('aria-label') || '').slice(0, 1000);
                const img = (card && card.querySelector('img')) ? (card.querySelector('img').currentSrc || card.querySelector('img').src || '') : '';
                return {href: a.href || '', text, img};
            })
            """)
        except Exception:
            links = []
        try: page.close()
        except Exception: pass
        for item in links:
            href = fix_url(item.get("href", ""), source)
            if not href or not product_link_allowed(href, source): continue
            text = str(item.get("text", "") or "")
            title = extract_title_from_card_text(text) or href_to_title(href)
            img = clean_image_url(str(item.get("img", "") or ""), source)
            imgs = [img] if image_url_allowed(img, source) else []
            sc, det = match_score(product, title + " " + text, "", href)
            if sc > 0:
                found.append(Candidate(source, title, product.brand, href, text, imgs, sc, det))
        if found: break
    uniq: Dict[str, Candidate] = {}
    for c in found:
        if c.url not in uniq or c.score > uniq[c.url].score:
            uniq[c.url] = c
    return sorted(uniq.values(), key=lambda c: c.score, reverse=True)[:max_links]

def validate_candidate(context, product: ProductRequest, cand: Candidate, min_score: float) -> Optional[Candidate]:
    page = open_page(context, cand.url, 3000)
    if not page:
        return None

    try:
        txt = page_text(page)
        real_title = actual_product_title(page, cand.source) or cand.title

        # For Amazon, score ONLY the real product title + URL.
        # Do not use full page body because recommendations can contain the requested brand/product
        # and make a wrong product look correct.
        if cand.source == "amazon":
            score_text = real_title
            min_needed = max(min_score, 82.0)
        else:
            score_text = real_title + " " + txt
            min_needed = min_score

        sc, det = match_score(product, score_text, "", cand.url)

        if cand.source == "amazon":
            ok, why = amazon_strict_accept(product, real_title, cand.url, sc)
            det = det + "; " + why
            if not ok:
                logging.info("Reject Amazon %s | %s", cand.url, det)
                return None

        if sc < min_needed:
            logging.info("Reject %s | %s", cand.url, det)
            return None

        cand.title = real_title
        cand.score = sc
        cand.details = det
        cand.image_urls = unique_keep_order(cand.image_urls + extract_gallery(page, cand.source))
        return cand

    finally:
        try:
            page.close()
        except Exception:
            pass


def extract_json_ld_images(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    urls = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try: data = json.loads(raw)
        except Exception: continue
        for node in (data if isinstance(data, list) else [data]):
            if isinstance(node, dict) and "Product" in str(node.get("@type", "")):
                img = node.get("image")
                if isinstance(img, str): urls.append(img)
                elif isinstance(img, list): urls.extend([str(x) for x in img if x])
    return urls

def extract_next_data_images(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string: return []
    try: data = json.loads(tag.string)
    except Exception: return []
    urls = []
    def add(d):
        for key in ["image", "images", "Image", "Images", "image_url", "imageUrl", "ImagePath", "ProductImages", "media", "Media"]:
            val = d.get(key)
            if isinstance(val, str): urls.append(val)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str): urls.append(item)
                    elif isinstance(item, dict):
                        for k in ["url", "Url", "src", "image", "imageUrl", "ImagePath", "xxl", "xl", "l", "m", "s"]:
                            if item.get(k): urls.append(str(item[k]))
    def walk(obj):
        if isinstance(obj, dict):
            add(obj)
            for v in obj.values(): walk(v)
        elif isinstance(obj, list):
            for v in obj: walk(v)
    walk(data)
    return urls

def js_array_block(text: str, start: int, max_len: int = 350000) -> str:
    depth = 0; end = None; instr = False; quote = ""; esc = False
    for i in range(start, min(len(text), start + max_len)):
        ch = text[i]
        if instr:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == quote: instr = False
            continue
        if ch in ('"', "'"):
            instr = True; quote = ch
        elif ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1; break
    return text[start:end] if end else ""

def extract_amazon(html: str, page) -> List[str]:
    urls = []
    for marker in ['"colorImages"', "'colorImages'"]:
        s = html.find(marker)
        if s == -1: continue
        pos = html.find("initial", s); b = html.find("[", pos)
        if pos != -1 and b != -1:
            block = js_array_block(html, b)
            urls += re.findall(r'https://m\.media-amazon\.com/images/I/[^"\'\\,\s]+', block)
            urls += re.findall(r'https://images-na\.ssl-images-amazon\.com/images/I/[^"\'\\,\s]+', block)
            urls += re.findall(r'https://images-eu\.ssl-images-amazon\.com/images/I/[^"\'\\,\s]+', block)
    for raw in re.findall(r'data-a-dynamic-image="([^"]+)"', html):
        raw = raw.replace("&quot;", '"').replace("&amp;", "&")
        try: urls += list(json.loads(raw).keys())
        except Exception: pass
    try:
        vals = page.locator("#altImages img, #imageBlock img, #imgTagWrapperId img").evaluate_all("(imgs)=>imgs.map(img=>img.getAttribute('data-old-hires') || img.currentSrc || img.src || '')")
        urls += vals
    except Exception: pass
    return urls

def extract_gallery(page, source: str) -> List[str]:
    """
    STRICT gallery extraction.

    This version intentionally avoids generic visible-image scraping because it
    catches banners, ads, related products, recommendation cards, and lifestyle
    graphics. It only uses source-specific product-gallery data or structured
    product data.
    """
    try:
        html = page.content()
    except Exception:
        html = ""

    urls: List[str] = []

    if source == "hyperpure":
        # Safe source: JSON-LD Product image only.
        # Do NOT regex all Hyperpure product assets from the page, because
        # listing/brand pages can contain dozens of unrelated product cards.
        urls += extract_json_ld_images(html)

    elif source == "bigbasket":
        # BigBasket product images normally contain the /pd/<product_id>/ value.
        product_id = ""
        m = re.search(r"/pd/(\d+)/", page.url)
        if m:
            product_id = m.group(1)

        raw = extract_next_data_images(html)
        raw += re.findall(r'https://[^"\'\s><]*bbassets\.com/media/uploads/p/[^"\'\s><)]+', html)

        for u in raw:
            u = clean_image_url(u, source)
            if product_id and product_id not in u:
                continue
            urls.append(u)

    elif source == "amazon":
        # Amazon gallery only: colorImages/imageBlock/altImages/dynamic main image.
        # No global media-amazon fallback.
        urls += extract_amazon(html, page)

    elif source == "jiomart":
        # Structured product image + direct JioMart product image patterns only.
        urls += extract_json_ld_images(html)
        urls += re.findall(r'https://www\.jiomart\.com/images/product/[^"\'\s><)]+', html, flags=re.I)
        urls += re.findall(r'https://[^"\'\s><)]*jiomart\.com/images/product/[^"\'\s><)]+', html, flags=re.I)

    elif source == "flipkart":
        # Structured product image + Flipkart product image CDN only.
        urls += extract_json_ld_images(html)
        urls += re.findall(r'https://rukminim[0-9]*\.flixcart\.com/image/[^"\'\s><)]+', html, flags=re.I)
        urls += re.findall(r'https://[^"\'\s><)]*flixcart\.com/image/[^"\'\s><)]+', html, flags=re.I)

    cleaned = [clean_image_url(u, source) for u in urls]
    cleaned = [u for u in cleaned if image_url_allowed(u, source)]
    cleaned = unique_keep_order(cleaned)

    # Safety guard: if a page still yields an unusually high number of images,
    # it is probably not a product-gallery page. Keep first 15 and review the URL.
    if source == "amazon" and len(cleaned) > 10:
        logging.warning("High Amazon gallery count from %s: %s. Keeping first 10.", page.url, len(cleaned))
        cleaned = cleaned[:10]
    elif len(cleaned) > 15:
        logging.warning("Suspiciously high image count from %s page %s: %s. Keeping first 15.", source, page.url, len(cleaned))
        cleaned = cleaned[:15]

    return cleaned


# ---------------------------- download ----------------------------

def download_url(url: str) -> Tuple[Optional[bytes], str]:
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT, headers={"Referer": "https://www.google.com/"})
        if r.status_code == 200 and r.content: return r.content, ""
        return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)

def image_info(data: bytes):
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB"); img.load()
        w, h = img.size
        if w < 100 or h < 100: return None, None, (w, h), "too small"
        md5 = hashlib.md5(data).hexdigest()
        ph = imagehash.phash(img) if imagehash else hashlib.md5(img.resize((16,16)).tobytes()).hexdigest()
        return md5, ph, (w, h), None
    except Exception as e:
        return None, None, None, str(e)

def is_duplicate(phash: Any, existing: List[Any], threshold: int = 4) -> bool:
    if phash is None: return False
    for old in existing:
        try:
            if imagehash and (phash - old) <= threshold: return True
            if not imagehash and phash == old: return True
        except Exception: pass
    return False

def image_ext(data: bytes, url: str) -> str:
    try:
        fmt = (Image.open(io.BytesIO(data)).format or "").lower()
        if fmt == "jpeg": return ".jpg"
        if fmt in {"jpg", "png", "webp"}: return "." + fmt
    except Exception: pass
    p = urlparse(url).path.lower()
    for ext in [".jpg", ".jpeg", ".png", ".webp"]:
        if p.endswith(ext): return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"

def download_images(product: ProductRequest, candidates: List[Candidate], image_root: Path, no_download: bool) -> Tuple[List[DownloadRow], Dict[str, List[str]]]:
    folder = image_root / safe_filename(f"{product.sku} - {product.product_name}", 140)
    folder.mkdir(parents=True, exist_ok=True)
    rows: List[DownloadRow] = []
    link_map: Dict[str, List[str]] = {}
    seen_md5, seen_ph = set(), []
    counts: Dict[str, int] = {}
    source_rank = {s: i for i, s in enumerate(DEFAULT_SOURCES)}
    candidates = sorted(candidates, key=lambda c: (source_rank.get(c.source, 99), -c.score))
    for cand in candidates:
        source = cand.source
        urls = unique_keep_order([clean_image_url(u, source) for u in cand.image_urls if image_url_allowed(clean_image_url(u, source), source)])
        if not urls: continue
        link_map[source] = unique_keep_order(link_map.get(source, []) + urls)
        if no_download: continue
        for url in urls:
            data, err = download_url(url)
            if not data:
                rows.append(DownloadRow(product.sku, product.product_name, product.brand, source, url, "", error=err)); continue
            md5, ph, size, v_err = image_info(data)
            if v_err or not md5 or ph is None or not size:
                rows.append(DownloadRow(product.sku, product.product_name, product.brand, source, url, "", error=v_err or "invalid image")); continue
            if md5 in seen_md5 or is_duplicate(ph, seen_ph):
                rows.append(DownloadRow(product.sku, product.product_name, product.brand, source, url, "", size[0], size[1], True, "duplicate skipped")); continue
            seen_md5.add(md5); seen_ph.append(ph)
            code = SOURCE_CODE[source]
            counts[code] = counts.get(code, 0) + 1
            file_path = folder / f"{safe_filename(product.product_name, 90)}_{code}_{counts[code]:02d}{image_ext(data, url)}"
            file_path.write_bytes(data)
            rows.append(DownloadRow(product.sku, product.product_name, product.brand, source, url, str(file_path), size[0], size[1]))
            sleep_polite(0.1, 0.4)
    return rows, link_map

# ---------------------------- main workflow ----------------------------

def scrape_product(context, product: ProductRequest, sources: List[str], min_score: float, max_candidates: int) -> List[Candidate]:
    final = []
    for source in sources:
        logging.info("%s | %s", product.product_name, source)
        candidates = []
        if source == "hyperpure":
            direct = direct_hyperpure_candidate(product)
            if direct: candidates.append(direct)
        candidates += search_source(context, product, source, max_candidates)
        candidates = sorted(candidates, key=lambda c: c.score, reverse=True)
        valid = None
        for cand in candidates[:max_candidates]:
            if cand.score < max(20, min_score - 30): continue
            valid = validate_candidate(context, product, cand, min_score)
            if valid: break
            sleep_polite()
        if valid:
            final.append(valid)
            logging.info("Matched %s from %s: %s", product.product_name, source, valid.details)
        else:
            logging.info("No valid match for %s from %s", product.product_name, source)
    return final

def build_rows(product: ProductRequest, candidates: List[Candidate], downloads: List[DownloadRow], link_map: Dict[str, List[str]]):
    review = []
    for c in candidates:
        review.append({"SKU": product.sku, "Product Name": product.product_name, "Brand": product.brand, "Source": SOURCE_LABEL[c.source], "Matched Title": c.title, "URL": c.url, "Match Score": round(c.score, 2), "Match Details": c.details, "Images Found": len(c.image_urls), "Image URLs": "\n".join(c.image_urls)})
    d_rows = [asdict(d) for d in downloads]
    ok = [d for d in downloads if d.saved_file and not d.error]
    summary = {"SKU": product.sku, "Product Name": product.product_name, "Brand": product.brand, "Matched Sources": ", ".join(sorted({SOURCE_LABEL[c.source] for c in candidates})), "Total Image Links": sum(len(v) for v in link_map.values()), "Images Downloaded": len(ok), "Folder": str(Path(ok[0].saved_file).parent) if ok else ""}
    for s, urls in link_map.items(): summary[f"{SOURCE_LABEL[s]} Links"] = len(urls)
    link_row = {"SKU": product.sku, "Product Name": product.product_name, "Brand": product.brand}
    for s, urls in link_map.items():
        for i, u in enumerate(urls, 1): link_row[f"{SOURCE_LABEL[s]} Image {i}"] = u
    return summary, review, d_rows, link_row

def write_reports(output_dir: Path, summaries: List[dict], reviews: List[dict], downloads: List[dict], links: List[dict]):
    output_dir.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_dir / "download_report.xlsx", engine="openpyxl") as w:
        pd.DataFrame(summaries).to_excel(w, index=False, sheet_name="Summary")
        pd.DataFrame(downloads).to_excel(w, index=False, sheet_name="Downloaded Images")
    with pd.ExcelWriter(output_dir / "catalog_review.xlsx", engine="openpyxl") as w:
        pd.DataFrame(reviews).to_excel(w, index=False, sheet_name="Matched Catalog")
    with pd.ExcelWriter(output_dir / "image_links.xlsx", engine="openpyxl") as w:
        pd.DataFrame(links).to_excel(w, index=False, sheet_name="Image Links")

def main():
    parser = argparse.ArgumentParser(description="Multi-source product image scraper")
    parser.add_argument("input_excel")
    parser.add_argument("--output", default="image_scraper_output")
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=62.0)
    parser.add_argument("--max-candidates", type=int, default=6)
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()
    input_file = Path(args.input_excel).resolve()
    output_dir = Path(args.output).resolve()
    setup_logging(output_dir)
    if not input_file.exists(): raise FileNotFoundError(input_file)
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip().lower() in DEFAULT_SOURCES]
    if not sources: raise ValueError("No valid sources selected")
    products = read_products(input_file, args.limit)
    logging.info("Loaded %d products", len(products))
    require_playwright()
    summaries, reviews, downloads, links = [], [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900}, locale="en-IN")
        try:
            for product in tqdm(products, desc="Scraping products"):
                try:
                    cands = scrape_product(context, product, sources, args.min_score, args.max_candidates)
                    dls, link_map = download_images(product, cands, output_dir / "images", args.no_download)
                    summary, rev, d_rows, link_row = build_rows(product, cands, dls, link_map)
                    summaries.append(summary); reviews.extend(rev); downloads.extend(d_rows); links.append(link_row)
                    write_reports(output_dir, summaries, reviews, downloads, links)
                except Exception as e:
                    logging.exception("Failed product %s: %s", product.product_name, e)
                    summaries.append({"SKU": product.sku, "Product Name": product.product_name, "Brand": product.brand, "Error": str(e)})
                    write_reports(output_dir, summaries, reviews, downloads, links)
                sleep_polite(0.6, 1.5)
        finally:
            context.close(); browser.close()
    write_reports(output_dir, summaries, reviews, downloads, links)
    logging.info("DONE. Reports: %s | Images: %s", output_dir, output_dir / "images")

if __name__ == "__main__":
    main()
