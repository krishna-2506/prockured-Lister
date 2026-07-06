
from __future__ import annotations
import argparse, csv, hashlib, io, json, logging, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, quote_plus, urlparse
import requests
from rapidfuzz import fuzz
try:
    import imagehash
except ImportError:
    imagehash = None
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# ── Config ──────────────────────────────────────────────────────────────────
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
OUTPUT_DIR = "prockured_output"
MIN_IMG_SIZE = 80
MAX_GALLERY = 15
DOWNLOAD_WORKERS = 4
HTTP_TIMEOUT = 25
LOG = logging.getLogger("scraper")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})


# ── Data classes ────────────────────────────────────────────────────────────
@dataclass
class Product:
    row: int; brand: str; title: str

@dataclass
class ScrapeResult:
    product: Product
    source: str = ""
    matched_title: str = ""
    product_url: str = ""
    image_urls: List[Tuple[str, str]] = field(default_factory=list)
    price: str = "N/A"

@dataclass
class DownloadedImg:
    input_title: str = ""
    source: str = ""
    matched_title: str = ""
    local_path: str = ""
    width: int = 0
    height: int = 0
    error: str = ""
    image_url: str = ""
    price: str = "N/A"


# ── CSV Reader ──────────────────────────────────────────────────────────────
def read_csv(path: Path) -> List[Product]:
    products = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
        brand_col = next((cols[n] for n in ["brand", "brand name"] if n in cols), None)
        title_col = next((cols[n] for n in ["product title", "product name", "title", "name"] if n in cols), None)
        if not title_col:
            raise ValueError("CSV needs a 'Product Title' column")
        for i, row in enumerate(reader, 2):
            t = (row.get(title_col) or "").strip()
            b = (row.get(brand_col) or "").strip() if brand_col else ""
            if t:
                products.append(Product(row=i, brand=b, title=t))
    return products


# ── Helpers ─────────────────────────────────────────────────────────────────
def safe_name(text: str, maxlen: int = 70) -> str:
    t = re.sub(r'[\\/:*?"<>|,()&\[\]{}]+', ' ', str(text))
    t = re.sub(r'\s+', ' ', t).strip().strip("._- ")
    return (t or "product")[:maxlen].rstrip("._- ")

def safe_filename(text: str, maxlen: int = 70) -> str:
    t = "".join(c if c.isalnum() else "-" for c in text.lower())
    return re.sub(r'-+', '-', t).strip("-")[:maxlen]

def make_search_query(brand: str, title: str) -> str:
    """Build a search query. ALWAYS includes the brand."""
    t = title
    for noise in ["prockured", "- prockured", "(prockured)"]:
        t = re.sub(re.escape(noise), "", t, flags=re.IGNORECASE)
    t = re.sub(r'\s*-\s*', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if brand and brand.lower() not in t.lower():
        t = f"{brand} {t}"
    return t

def is_product_img(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    low = url.lower()
    bad = ["logo", "icon", "sprite", "placeholder", "blank", "banner",
           "favicon", "avatar", "badge", "cart", "stars", "rating",
           "transparent", "loading", "default", "payment", "social",
           "whatsapp", "facebook", "instagram", "youtube", "twitter",
           "arrow", "close", "search", "filter", "nav", "header", "footer",
           "pixel.gif", "1x1", "spacer", ".svg", ".gif"]
    return not any(b in low for b in bad)

def dedupe(urls: List[str]) -> List[str]:
    seen: Set[str] = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

def upgrade_amazon_url(url: str) -> str: 
    if "images/I/" in url:
        return re.sub(r'\._[A-Za-z0-9_,-]+\.', '.', url)
    return url

def _is_brand_match(brand: str, title: str) -> bool:
    if not brand: return True
    b_words = [w for w in re.split(r'[^a-zA-Z0-9]', brand.lower()) if len(w) > 2 and w not in ('pvt', 'ltd')]
    t_lower = title.lower()
    for bw in b_words:
        if bw in t_lower:
            return True
    return False



def extract_json_ld_images(html: str) -> List[str]:
    import json
    from bs4 import BeautifulSoup
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

def extract_amazon_images(html: str) -> List[str]:
    import re
    urls = []
    for marker in ['"colorImages"', "'colorImages'"]:
        s = html.find(marker)
        if s == -1: continue
        pos = html.find("initial", s); b = html.find("[", pos)
        if pos != -1 and b != -1:
            block = js_array_block(html, b)
            urls += re.findall(r'https://m\.media-amazon\.com/images/I/[^"\'\\\s]+', block)
    return urls


def normalize(text: str) -> str:
    import re
    t = re.sub(r'[^\w\s]', ' ', text.lower())
    return re.sub(r'\s+', ' ', t).strip()

def qty_tokens(text: str) -> set:
    import re
    tokens = set()
    matches = re.finditer(r'(\d+(?:\.\d+)?)\s*(kg|g|gm|ml|l|ltr|pack|pc|piece)s?\b', text.lower())
    for m in matches:
        tokens.add(m.group(0).replace(" ", ""))
    return tokens

def match_score(req_brand: str, req_title: str, cand_title: str) -> int:
    from rapidfuzz import fuzz
    req_norm = normalize(req_title)
    cand_norm = normalize(cand_title)
    if req_brand:
        req_norm = req_brand.lower() + " " + req_norm
    score = fuzz.token_sort_ratio(req_norm, cand_norm)
    
    # qty strict match
    r_qty = qty_tokens(req_title)
    c_qty = qty_tokens(cand_title)
    if r_qty:
        if not c_qty or not r_qty.issubset(c_qty):
            score -= 30
    return score

# ── Browser Manager ────────────────────────────────────────────────────────
class Browser:
    def __init__(self):
        self._pw = None; self._browser = None; self._ctx = None

    def start(self):
        if not sync_playwright:
            raise RuntimeError("pip install playwright && python -m playwright install chromium")
        self._pw = sync_playwright().__enter__()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            java_script_enabled=True,
        )
        LOG.info("Browser ready")

    def new_page(self):
        if not self._ctx: self.start()
        return self._ctx.new_page()

    def close(self):
        for obj in [self._ctx, self._browser]:
            try:
                if obj: obj.close()
            except: pass
        try:
            if self._pw: self._pw.stop()
        except: pass


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: HYPERPURE
# ══════════════════════════════════════════════════════════════════════════════

def scrape_hyperpure(query: str, brand: str, product_title: str, browser: Browser) -> Tuple[str, str, List[str], str]:
    search_url = f"https://www.hyperpure.com/in/search/{quote(query.replace(' ', '-'))}?&type=SEARCH&query={quote(query)}"
    page = browser.new_page()
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3500)
        
        soup = BeautifulSoup(page.content(), "html.parser")
        product_found = False
        title_text = ""
        for h3 in soup.find_all("h3"):
            title_text = h3.get_text(strip=True)
            if not title_text: continue
            
            # STRICT MATCH
            score = match_score(brand, product_title, title_text); print(f'score for {title_text}: {score}')
            if score < 70: continue
            
            fallback_img = ""
            try:
                # get the image from the search card directly just in case click fails
                card_img = page.locator(f'h3:has-text("{title_text}")').locator("xpath=../..").locator("img").first
                fallback_img = card_img.get_attribute("src")
            except: pass

            try:
                # Dismiss city modal that intercepts clicks
                page.locator('text="Bengaluru"').first.click(timeout=1000)
                page.wait_for_timeout(500)
            except: pass

            try:
                page.locator(f'h3:has-text("{title_text}")').first.click(force=True, timeout=5000)
                page.wait_for_timeout(3500)
                product_found = True
                break
            except Exception as e:
                print('CLICK ERROR:', repr(e))
                
        if product_found:
            html = page.content()
            images = extract_json_ld_images(html)
            
            # IF JSON-LD FAILS OR NAVIGATION FAILED, USE SEARCH CARD IMAGE. 
            # DO NOT EXTRACT ALL IMAGES ON PAGE (PREVENTS WINGREENS BUG!)
            if not images and fallback_img:
                images = [fallback_img]
                
            images = dedupe(images)[:5]  # MAX 5 IMAGES
            
            psoup = BeautifulSoup(html, "html.parser")
            price = "N/A"
            for span in psoup.find_all('span'):
                t = span.get_text(strip=True)
                if '₹' in t and len(t) < 15:
                    price = t.replace('₹', 'Rs. ')
                    break
                    
            prod_url = page.url
            page.close()
            return title_text, prod_url, images, price

        page.close()
        return "", "", [], "N/A"
    except Exception:
        try: page.close()
        except: pass
        return "", "", [], "N/A"


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 2: AMAZON INDIA
# ══════════════════════════════════════════════════════════════════════════════

def _get_brand_keywords(brand: str, title: str) -> List[str]:
    kws = []
    if brand: kws.append(brand.lower())
    for m in re.finditer(r'\(([^)]+)\)', title):
        inner = m.group(1).strip().lower()
        if len(inner) >= 3 and inner not in kws: kws.append(inner)
    lead = re.split(r'[\-\(]', title)[0].strip().lower()
    if lead and len(lead) >= 4 and lead not in kws: kws.append(lead)
    return kws


def scrape_amazon(query: str, brand: str, title: str, browser: Browser) -> Tuple[str, str, List[str]]:
    search_url = f"https://www.amazon.in/s?k={quote_plus(query)}"
    page = browser.new_page()
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        cards = page.query_selector_all('[data-component-type="s-search-result"]')
        candidates = []
        for card in cards[:10]:
            link_el = card.query_selector('a[href*="/dp/"]')
            if not link_el: continue
            title_el = card.query_selector('h2')
            c_title = title_el.inner_text() if title_el else ""
            candidates.append(("https://www.amazon.in" + link_el.get_attribute("href"), c_title))
        
        if not candidates:
            page.close(); return "", "", []
        
        target = candidates[0]
        target = candidates[0]
        best_score = 0
        for href, ctitle in candidates:
            score = match_score(brand, title, ctitle)
            if score > best_score:
                best_score = score
                target = (href, ctitle)
                
        if best_score < 70:
            page.close(); return "", "", []
        
        page.goto(target[0], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        images = dedupe(extract_amazon_images(page.content()))[:5]
        page.close()
        return target[1], target[0], images
    except Exception:
        try: page.close()
        except: pass
        return "", "", []


def _extract_amazon_gallery(html: str) -> List[str]:
    raw_urls = []
    for m in re.finditer(r'"hiRes"\s*:\s*"(https://[^"]+)"', html):
        raw_urls.append(m.group(1))
    for url in re.findall(r'https://m\.media-amazon\.com/images/I/[A-Za-z0-9._+%-]+\.(?:jpg|png|webp)', html):
        raw_urls.append(upgrade_amazon_url(url))
    return dedupe([u for u in raw_urls if is_product_img(u)])[:MAX_GALLERY]


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 3: FLIPKART
# ══════════════════════════════════════════════════════════════════════════════

def scrape_flipkart(query: str, browser: Browser) -> Tuple[str, str, List[str]]:
    search_url = f"https://www.flipkart.com/search?q={quote_plus(query)}"
    page = browser.new_page()
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(2500)
        link = page.query_selector('a[href*="/p/"]')
        if not link:
            page.close(); return "", "", []
        href = "https://www.flipkart.com" + link.get_attribute("href")
        page.goto(href, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(2500)
        
        soup = BeautifulSoup(page.content(), "html.parser")
        title = soup.select_one('h1 span')
        title_text = title.text if title else "Flipkart Product"
        
        images = []
        for img in soup.select('img[src*="rukminim"]'):
            src = re.sub(r'/image/\d+/\d+/', '/image/832/832/', img.get('src', ''))
            if is_product_img(src): images.append(src)
        
        page.close()
        return title_text, href, dedupe(images)
    except Exception:
        try: page.close()
        except: pass
        return "", "", []


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 4: BIGBASKET
# ══════════════════════════════════════════════════════════════════════════════

def scrape_bigbasket(query: str, browser: Browser) -> Tuple[str, str, List[str]]:
    search_url = f"https://www.bigbasket.com/ps/?q={quote_plus(query)}"
    page = browser.new_page()
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(3500)
        soup = BeautifulSoup(page.content(), "html.parser")
        
        product_link = soup.select_one('a[href*="/product/"]')
        href = "https://www.bigbasket.com" + product_link['href'] if product_link else search_url
        
        images = []
        for img in soup.select('img[src*="bbassets"]'):
            src = re.sub(r'/p/[smlx]+/', '/p/xxl/', img.get('src', ''))
            if is_product_img(src): images.append(src)
            
        page.close()
        return query, href, dedupe(images)[:MAX_GALLERY]
    except Exception:
        try: page.close()
        except: pass
        return "", "", []


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 5: GOOGLE
# ══════════════════════════════════════════════════════════════════════════════

def scrape_google_images(query: str, browser: Browser) -> List[str]:
    url = f"https://www.google.com/search?q={quote_plus(query + ' product')}&tbm=isch"
    page = browser.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)
        html = page.content()
        page.close()
        return dedupe(re.findall(r'https?://[^\s"\'<>]+?\.(?:jpg|png|webp)', html))[:8]
    except Exception:
        try: page.close()
        except: pass
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Download
# ══════════════════════════════════════════════════════════════════════════════

def download_image(match: ScrapeResult, output_dir: Path) -> List[DownloadedImg]:
    source_map = {"hyperpure": "h", "amazon": "a", "flipkart": "f", "bigbasket": "b", "google": "g"}
    base_name = safe_filename(match.product.title, 80)
    brand_safe = safe_filename(match.product.brand, 40) if match.product.brand else "Unknown"
    main_folder = output_dir / "images" / brand_safe / base_name
    
    results = []
    idx_map = {"hyperpure": 1, "amazon": 1, "flipkart": 1, "bigbasket": 1, "google": 1}
    
    for src_type, url in match.image_urls:
        initial = source_map.get(src_type, "x")
        idx = idx_map[src_type]
        idx_map[src_type] += 1
        
        dl = DownloadedImg(input_title=match.product.title, source=src_type, 
                           matched_title=match.matched_title, price=match.price, image_url=url)
        
        folder = main_folder
        if src_type == "amazon" and match.source == "hyperpure":
            folder = main_folder / "amazon"
            
        folder.mkdir(parents=True, exist_ok=True)
        
        try:
            r = SESSION.get(url, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                try:
                    if imagehash:
                        from PIL import Image
                        import io
                        img_obj = Image.open(io.BytesIO(r.content)).convert("RGB")
                        img_obj.load()
                        if img_obj.size[0] < 100 or img_obj.size[1] < 100:
                            raise ValueError("too small")
                        ph = imagehash.phash(img_obj)
                        if not hasattr(match, 'seen_hashes'):
                            match.seen_hashes = []
                        if any(abs(ph - old) <= 4 for old in match.seen_hashes):
                            raise ValueError("duplicate")
                        match.seen_hashes.append(ph)
                except ValueError as ve:
                    dl.error = str(ve)
                    continue
                except Exception as e:
                    pass

                from urllib.parse import urlparse
                path_part = urlparse(url).path
                ext = ".jpg"
                if "." in path_part.split("/")[-1]:
                    parsed_ext = "." + path_part.split("/")[-1].split(".")[-1].lower()
                    if parsed_ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
                        ext = parsed_ext
                
                path = folder / f"{base_name}-{initial}-{idx:02d}-prockured{ext}"
                path.write_bytes(r.content)
                dl.local_path = str(path)
            else: dl.error = f"HTTP {r.status_code}"
        except Exception as e: dl.error = str(e)
        results.append(dl)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

def process_product(product: Product, browser: Browser) -> ScrapeResult:
    result = ScrapeResult(product=product)
    query = make_search_query(product.brand, product.title)

    # 1. Hyperpure
    title, url, images, price = scrape_hyperpure(query, product.brand, product.title, browser)
    if images:
        result.source, result.matched_title, result.product_url, result.price = "hyperpure", title, url, price
        result.image_urls = [("hyperpure", img) for img in images]
        
        # ALSO fetch Amazon
        a_title, a_url, a_images = scrape_amazon(query, product.brand, product.title, browser)
        if a_images and match_score(product.brand, product.title, a_title) >= 70:
            result.image_urls.extend([("amazon", img) for img in a_images])
            
        return result

    # 2. Amazon
    title, url, images = scrape_amazon(query, product.brand, product.title, browser)
    if images and match_score(product.brand, product.title, title) >= 70:
        result.source, result.matched_title, result.product_url = "amazon", title, url
        result.image_urls = [("amazon", img) for img in images]
        return result

    # 3. Flipkart
    title, url, images = scrape_flipkart(query, browser)
    if images and match_score(product.brand, product.title, title) >= 70:
        result.source, result.matched_title, result.product_url = "flipkart", title, url
        result.image_urls = [("flipkart", img) for img in images]
        return result

    # 4. BigBasket
    title, url, images = scrape_bigbasket(query, browser)
    if images and match_score(product.brand, product.title, title) >= 70:
        result.source, result.matched_title, result.product_url = "bigbasket", title, url
        result.image_urls = [("bigbasket", img) for img in images]
        return result

    # 5. Google
    images = scrape_google_images(query, browser)
    if images:
        result.source, result.matched_title, result.product_url = "google", "Google Search", f"https://www.google.com/search?q={quote_plus(query)}"
        result.image_urls = [("google", img) for img in images]
    return result

def write_reports(output_dir: Path, products: List[Product], matches: List[ScrapeResult]):
    summary = []
    hp_prices = []
    for p in products:
        m = next((m for m in matches if m.product.title == p.title), None)
        summary.append({
            "brand": p.brand, "title": p.title, "matched": "Yes" if m else "No",
            "source": m.source if m else "", "images_found": len(m.image_urls) if m else 0,
            "matched_title": m.matched_title if m else "", "price": m.price if m else "N/A",
            "product_url": m.product_url if m else ""
        })
        if m and m.source == "hyperpure":
            hp_prices.append({
                "Brand": p.brand, "Product Title": p.title, "Matched Title": m.matched_title,
                "Price": m.price, "Product URL": m.product_url
            })
            
    with (output_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=summary[0].keys()); w.writeheader(); w.writerows(summary)
        
    if hp_prices:
        with (output_dir / "hyperpure_prices.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=hp_prices[0].keys()); w.writeheader(); w.writerows(hp_prices)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv")
    parser.add_argument("--output", default="prockured_output")
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Logging
    fh = logging.FileHandler(output_dir / "scraper.log", encoding="utf-8", mode="w")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(message)s"))
    if hasattr(sys.stdout, 'reconfigure'):
        try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except: pass
    LOG.handlers.clear()
    LOG.addHandler(fh)
    LOG.addHandler(ch)
    LOG.setLevel(logging.INFO)
    
    products = read_csv(Path(args.input_csv))
    if args.limit: products = products[:args.limit]
    
    LOG.info("=" * 60)
    LOG.info("  PROCKURED IMAGE SCRAPER v6")
    LOG.info("  Products: %d", len(products))
    LOG.info("  Output: %s", output_dir)
    LOG.info("  Pipeline: Hyperpure -> Amazon -> Flipkart -> BigBasket -> Google")
    LOG.info("=" * 60)
    
    browser = Browser()
    try: browser.start()
    except Exception as e:
        print(f"ERROR: {e}\nRun: pip install playwright && python -m playwright install chromium")
        sys.exit(1)
    
    results = []
    all_downloads = []
    for i, p in enumerate(products):
        LOG.info("\n[%d/%d] %s", i + 1, len(products), p.title[:80])
        try:
            res = process_product(p, browser)
            results.append(res)
            if res.image_urls and not args.no_download:
                dls = download_image(res, output_dir)
                all_downloads.extend(dls)
        except Exception as e:
            LOG.error("  ERROR: %s", str(e))
            results.append(ScrapeResult(product=p))
        if i < len(products) - 1: time.sleep(1)
    browser.close()
    
    matched = [r for r in results if r.image_urls]
    failed = [r for r in results if not r.image_urls]
                
    LOG.info("\nWriting reports...")
    write_reports(output_dir, products, results)
    
    # Write image reports
    img_rows = []
    for d in all_downloads:
        img_rows.append({
            "input_title": d.input_title, "source": d.source,
            "matched_title": d.matched_title, "image_url": d.image_url,
            "local_path": d.local_path, "width": d.width, "height": d.height,
            "price": d.price, "error": d.error
        })
    if img_rows:
        with (output_dir / "all_images.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=img_rows[0].keys()); w.writeheader(); w.writerows(img_rows)
    
    ok_dl = sum(1 for d in all_downloads if not d.error)
    LOG.info("")
    LOG.info("=" * 60)
    LOG.info("  ALL DONE!")
    LOG.info("  Products matched: %d/%d", len(matched), len(products))
    LOG.info("  Images downloaded: %d", ok_dl)
    LOG.info("  Output: %s", output_dir)
    LOG.info("=" * 60)

if __name__ == "__main__":
    main()
