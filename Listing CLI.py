import os
import re
import argparse
import sys
import time
import queue
import traceback
import csv
import math
import hashlib
import json
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field

import pyperclip
try:
    from pynput import keyboard
except ImportError:
    keyboard = None
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

CDP_URL = "http://127.0.0.1:9222"

# Run-folder based paths. Keep this script, batch_products.json, CSVs, images,
# or prockured_output folder in the same folder you run the script from.
# You can still override any path with environment variables if needed.
RUN_DIR = Path(os.environ.get("PROCKURED_RUN_DIR", Path.cwd())).resolve()
SCRIPT_DIR = Path(__file__).resolve().parent

def _first_existing_path(*paths):
    for path in paths:
        p = Path(path)
        if p.exists():
            return p
    return Path(paths[0])

DEFAULT_OUTPUT_DIR = Path(os.environ.get(
    "PROCKURED_OUTPUT_DIR",
    str(_first_existing_path(RUN_DIR, SCRIPT_DIR))
))
DEFAULT_IMAGE_ROOT = Path(os.environ.get("PROCKURED_IMAGE_ROOT", str(RUN_DIR / "images"))).resolve()
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
MAX_MEDIA_UPLOADS = int(os.environ.get("PROCKURED_MAX_MEDIA_UPLOADS", "25"))
BASE_MARKUP_MIN = 10
BASE_MARKUP_MAX = 30

ADMIN_PRODUCTS_URL = os.environ.get("PROCKURED_ADMIN_PRODUCTS_URL", "https://store.prockured.com/admin/products")
DEFAULT_BATCH_JSON = Path(os.environ.get(
    "PROCKURED_BATCH_JSON",
    str(_first_existing_path(RUN_DIR / "batch_products.json", SCRIPT_DIR / "batch_products.json"))
))
DEFAULT_BATCH_REPORT_DIR = Path(os.environ.get("PROCKURED_BATCH_REPORT_DIR", str(RUN_DIR / "batch_reports")))

command_queue = queue.Queue()
stop_requested = False
current_data = None


def log(msg=""):
    print(msg, flush=True)


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def norm_key(key: str) -> str:
    return re.sub(r"\s+", " ", (key or "").strip()).lower()


@dataclass
class ProductData:
    basics: dict = field(default_factory=dict)
    attributes: list = field(default_factory=list)
    filter_attributes: list = field(default_factory=list)
    variant_attributes: list = field(default_factory=list)
    variant_filter_attributes: list = field(default_factory=list)
    variant_pricing: list = field(default_factory=list)
    seo: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)


@dataclass
class BatchRunOptions:
    sections: set = field(default_factory=set)
    lookup: str = "sku"
    force_media_root: Path | None = None
    media_sort: str | None = None
    media_min_score: int | None = None
    resume_report: Path | None = None
    explicit_variations: bool = False


BASICS_KEYS = [
    "Product Type",
    "Brand",
    "Product Name",
    "Slug",
    "Product Tags",
    "Description",
    "Short Description",
    "Model Name",
]
SEO_KEYS = ["SEO Title", "SEO Description", "SEO Keywords"]
MEDIA_KEYS = [
    "Main Image", "Image 1", "Image 2", "Image 3", "Image URL", "Image URLs",
    "Image Folder", "Folder", "Replace Existing Images", "Replace Images",
    "Alt Text Suffix", "Alt Text Prefix"
]
PRICING_KEYS = ["Price", "Sale Price", "Actual Price", "Discount Price", "Base Price", "MRP", "Compare Price", "Cost Price", "GST"]


def parse_key_value_section(lines, known_keys):
    """Parse key/value sections while preserving paragraph breaks for Description.

    Important for Prockured: the long Description textarea must keep blank lines
    between paragraphs exactly as copied from ChatGPT. Other fields are kept
    compact so tags/SEO/short description do not accidentally get extra spacing.
    """
    result_lines = {}
    current_key = None
    key_pattern = re.compile(r"^(.+?)\s*[:：]\s*(.*)$")
    known_norm = {norm_key(k): k for k in known_keys}
    preserve_blank_keys = {"Description"}

    for raw in lines:
        # Keep raw right-side spacing only; left indentation from copy blocks is not useful.
        raw_line = raw.rstrip()
        stripped = raw_line.strip()

        if not stripped:
            if current_key in preserve_blank_keys:
                # Preserve real blank lines in the long Description only.
                result_lines.setdefault(current_key, []).append("")
            continue

        m = key_pattern.match(stripped)
        if m:
            candidate_key = norm_key(m.group(1))
            if candidate_key in known_norm:
                current_key = known_norm[candidate_key]
                first_value = m.group(2).strip()
                result_lines[current_key] = [first_value] if first_value else []
                continue

        if current_key:
            if current_key in preserve_blank_keys:
                # Preserve paragraph structure, but remove accidental outer indentation.
                result_lines.setdefault(current_key, []).append(stripped)
            else:
                # Compact continuation lines for non-description fields.
                if result_lines.get(current_key):
                    result_lines[current_key][-1] = (result_lines[current_key][-1].rstrip() + " " + stripped).strip()
                else:
                    result_lines[current_key] = [stripped]

    result = {}
    for key, value_lines in result_lines.items():
        if key in preserve_blank_keys:
            value = "\n".join(value_lines).strip()
            # Collapse 3+ blank lines to exactly one empty line between paragraphs.
            value = re.sub(r"\n{3,}", "\n\n", value)
        else:
            value = " ".join([v.strip() for v in value_lines if v.strip()]).strip()
        if value:
            result[key] = value

    return result

def parse_attributes(lines):
    attrs = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\u2022]\s*", "", line)
        if ":" not in line and "：" not in line:
            continue
        if "：" in line and ":" not in line:
            name, value = line.split("：", 1)
        else:
            name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name and value:
            attrs.append((name, value))
    return attrs




def split_variant_values(value: str) -> list:
    """Split variant values like '100ML, 130ML, 200ML' into separate tag values."""
    if value is None:
        return []
    parts = [p.strip() for p in re.split(r"\s*,\s*", str(value)) if p.strip()]
    return parts or [str(value).strip()]


def parse_variant_pricing(lines):
    """Parse lines like:
    Packing Type : Pack of 3000 | Capacity : 100ML | Price : 6540 + Tax
    Returns [{attributes:{...}, price_raw:'...', price:6540.0}]
    """
    rows = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\u2022]\s*", "", line)
        parts = [p.strip() for p in line.split("|") if p.strip()]
        attrs = {}
        price_raw = ""
        price = None
        for part in parts:
            if ":" not in part and "：" not in part:
                continue
            if "：" in part and ":" not in part:
                k, v = part.split("：", 1)
            else:
                k, v = part.split(":", 1)
            key = k.strip()
            val = v.strip()
            if not key or not val:
                continue
            if norm_key(key) in {"price", "sale price", "discount price", "actual price"}:
                price_raw = val
                price = parse_price_value(val)
            else:
                attrs[key] = val
        if attrs:
            rows.append({"attributes": attrs, "price_raw": price_raw, "price": price})
    return rows


def is_variable_product(data: ProductData) -> bool:
    pt = norm_key((data.basics or {}).get("Product Type", ""))
    return (
        "variable" in pt
        or bool(data.variant_attributes)
        or bool(data.variant_filter_attributes)
        or bool(data.variant_pricing)
    )

def parse_clipboard_text(text: str) -> ProductData:
    data = ProductData()
    sections = {
        "BASICS": [],
        "ATTRIBUTES": [],
        "FILTER ATTRIBUTES": [],
        "VARIANT ATTRIBUTES": [],
        "VARIANT FILTER ATTRIBUTES": [],
        "VARIANT PRICING": [],
        "SEO": [],
        "MEDIA": [],
        "PRICING": [],
    }
    current = None

    aliases = {
        "[BASICS]": "BASICS", "BASICS": "BASICS",
        "[ATTRIBUTES]": "ATTRIBUTES", "ATTRIBUTES": "ATTRIBUTES",
        "[FILTER ATTRIBUTES]": "FILTER ATTRIBUTES", "FILTER ATTRIBUTES": "FILTER ATTRIBUTES",
        "[VARIANT ATTRIBUTES]": "VARIANT ATTRIBUTES", "VARIANT ATTRIBUTES": "VARIANT ATTRIBUTES",
        "[VARIANT FILTER ATTRIBUTES]": "VARIANT FILTER ATTRIBUTES",
        "VARIANT FILTER ATTRIBUTES": "VARIANT FILTER ATTRIBUTES",
        "[VARIANT PRICING]": "VARIANT PRICING", "VARIANT PRICING": "VARIANT PRICING",
        "[SEO]": "SEO", "SEO": "SEO",
        "[MEDIA]": "MEDIA", "MEDIA": "MEDIA",
        "[PRICING]": "PRICING", "PRICING": "PRICING",
    }

    for raw in text.splitlines():
        upper = raw.strip().upper()
        if upper in aliases:
            current = aliases[upper]
            continue
        if current:
            sections[current].append(raw)

    data.basics = parse_key_value_section(sections["BASICS"], BASICS_KEYS)
    data.attributes = parse_attributes(sections["ATTRIBUTES"])
    data.filter_attributes = parse_attributes(sections["FILTER ATTRIBUTES"])
    data.variant_attributes = parse_attributes(sections["VARIANT ATTRIBUTES"])
    data.variant_filter_attributes = parse_attributes(sections["VARIANT FILTER ATTRIBUTES"])
    data.variant_pricing = parse_variant_pricing(sections["VARIANT PRICING"])
    data.seo = parse_key_value_section(sections["SEO"], SEO_KEYS)
    data.media = parse_key_value_section(sections["MEDIA"], MEDIA_KEYS)
    data.pricing = parse_key_value_section(sections["PRICING"], PRICING_KEYS)
    return data

def page_is_alive(page):
    try:
        return page is not None and (not page.is_closed()) and page.evaluate("() => document.readyState") in ["loading", "interactive", "complete"]
    except Exception:
        return False


def score_product_page(page):
    """Prefer the actual Prockured product edit page, not old/closed/list pages."""
    try:
        if page.is_closed():
            return -999
        url = page.url or ""
        if "store.prockured.com" not in url or "/admin" not in url:
            return -999
        score = 10
        if "/admin/products" in url:
            score += 20
        try:
            txt = page.locator("text=Edit Product").first
            if txt.count() > 0:
                score += 20
        except Exception:
            pass
        try:
            if page.locator("text=Basic Information").count() > 0:
                score += 10
        except Exception:
            pass
        try:
            if page.locator("text=Search Optimizer").count() > 0:
                score += 10
        except Exception:
            pass
        return score
    except Exception:
        return -999


def find_product_page(browser):
    candidates = []
    for context in browser.contexts:
        for page in context.pages:
            sc = score_product_page(page)
            if sc > -999:
                candidates.append((sc, page))
    if not candidates:
        raise RuntimeError("No live Prockured admin page found. Open Brave automation window and open the product edit page.")
    candidates.sort(key=lambda x: x[0], reverse=True)
    page = candidates[0][1]
    page.set_default_timeout(8000)
    return page


def connect_page(pw, old_browser=None):
    """Return a fresh live Prockured admin page.

    CLI V1.0.1 connection behaviour:
    - Reuse a live CDP browser/page when possible.
    - Show progress before connecting so the terminal never appears frozen.
    - Fail quickly with a clear remote-debugging message when port 9222 is unavailable.
    - If Brave is connected but no Prockured admin tab is open, create one and navigate
      to the Products page automatically.
    """
    browsers_to_try = []
    if old_browser is not None:
        browsers_to_try.append(old_browser)

    for browser in browsers_to_try:
        try:
            page = find_product_page(browser)
            if page_is_alive(page):
                log(f"Reusing connected Prockured page: {page.url}")
                return browser, page
        except Exception:
            pass

    log(f"Connecting to Brave/Chrome automation port: {CDP_URL}")
    log("The browser must be running with --remote-debugging-port=9222.")
    try:
        browser = pw.chromium.connect_over_cdp(CDP_URL, timeout=10000)
    except Exception as e:
        raise RuntimeError(
            "Could not connect to the browser automation port at " + CDP_URL + ". "
            "Start Brave with --remote-debugging-port=9222, sign in to Prockured, "
            "and run the command again. Original error: " + str(e)
        ) from e

    log("Browser automation connection established.")

    try:
        page = find_product_page(browser)
        log(f"Found open Prockured admin page: {page.url}")
        return browser, page
    except Exception:
        pass

    # Connected browser exists, but there is no suitable Prockured admin tab.
    contexts = list(browser.contexts)
    if not contexts:
        raise RuntimeError("Browser connected, but no browser context is available.")

    context = contexts[0]
    log("No Prockured admin tab was found. Opening the Products page automatically...")
    try:
        page = context.new_page()
        page.set_default_timeout(8000)
        page.goto(ADMIN_PRODUCTS_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    except Exception as e:
        raise RuntimeError(
            "Browser connected, but the Prockured Products page could not be opened. "
            "Confirm that you are signed in to store.prockured.com. Original error: " + str(e)
        ) from e

    if "store.prockured.com" not in (page.url or "") or "/admin" not in (page.url or ""):
        raise RuntimeError(
            f"The browser opened '{page.url}' instead of the Prockured admin page. "
            "Sign in to Prockured in the automation browser and run the batch again."
        )

    log(f"Opened Prockured admin page: {page.url}")
    return browser, page


def click_tab(page, tab_name):
    """Open a product edit tab reliably.

    Prockured UI sometimes shows the first tab as "Basic" but older script calls it
    "Basics". This function treats Basic/Basics as the same tab, so batch mode can
    always return from Pricing/SEO/Attributes back to the Basic tab before selecting
    Category and clicking Update Product.
    """
    log(f"Opening {tab_name} tab...")
    wanted_raw = (tab_name or "").strip()
    wanted_lower = wanted_raw.lower()
    aliases = [wanted_raw]
    if wanted_lower in {"basic", "basics"}:
        aliases = ["Basic", "Basics"]

    # Scroll top first because the tab bar is at the top of the edit screen.
    try:
        page.evaluate("() => { window.scrollTo({top: 0, left: 0, behavior: 'instant'}); }")
        time.sleep(0.2)
    except Exception:
        pass

    # Try exact accessible/tab/button clicks first.
    for alias in aliases:
        patterns = [
            re.compile(rf"^\s*{re.escape(alias)}\s*$", re.I),
        ]
        for pat in patterns:
            attempts = [
                lambda pat=pat: page.get_by_role("tab", name=pat).first.click(timeout=1500, force=True),
                lambda pat=pat: page.get_by_role("button", name=pat).first.click(timeout=1500, force=True),
                lambda alias=alias: page.get_by_text(alias, exact=True).first.click(timeout=1500, force=True),
            ]
            for attempt in attempts:
                try:
                    attempt()
                    time.sleep(0.7)
                    return True
                except Exception:
                    pass

    # CSS/text fallbacks for existing behavior.
    selectors = []
    for alias in aliases:
        selectors.extend([
            f"text=/{re.escape(alias)}/i",
            f"button:has-text('{alias}')",
            f"a:has-text('{alias}')",
            f"[role='tab']:has-text('{alias}')",
        ])
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(force=True)
                time.sleep(0.7)
                return True
        except Exception:
            continue

    # JS fallback: click visible element whose text is exactly one of the aliases.
    ok = page.evaluate(
        r"""(aliases) => {
            const wanted = aliases.map(a => String(a || '').trim().toLowerCase()).filter(Boolean);
            function visible(el){
                const r=el.getBoundingClientRect();
                const s=window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display !== 'none' && s.visibility !== 'hidden';
            }
            const els = [...document.querySelectorAll('button,a,[role="tab"],div,span')]
                .filter(visible)
                .map(el => ({el, text:(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim().toLowerCase(), r:el.getBoundingClientRect(), role:el.getAttribute('role')||''}))
                .filter(o => wanted.includes(o.text))
                .sort((a,b) => {
                    const ar = a.role === 'tab' ? 0 : 10;
                    const br = b.role === 'tab' ? 0 : 10;
                    return ar - br || a.r.top - b.r.top || a.r.left - b.r.left;
                });
            if (els.length) {
                els[0].el.scrollIntoView({block:'center', inline:'nearest'});
                els[0].el.click();
                return true;
            }
            return false;
        }""",
        aliases,
    )
    time.sleep(0.7)
    return bool(ok)


def set_nearest_field_by_label(page, label_names, value, allow_textarea=True, allow_input=True, clear_only=False):
    """Set the nearest visible input/textarea under an exact label.

    This intentionally uses exact label matching and optional field-type limits.
    It avoids the earlier issue where Product Tags could spill into SKU.
    """
    if isinstance(label_names, str):
        label_names = [label_names]
    value = "" if clear_only else (value or "")
    return page.evaluate(
        r"""({labels, value, allowTextarea, allowInput}) => {
            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function normalize(s) {
                return (s || '')
                    .replace(/\*/g, '')
                    .replace(/\s+/g, ' ')
                    .trim()
                    .toUpperCase();
            }
            function setValue(el, val) {
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.focus();
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }
            const wanted = labels.map(normalize);
            const labelSelector = 'label, div, span, p, h1, h2, h3, h4, h5, strong';
            const possibleLabels = [...document.querySelectorAll(labelSelector)]
                .filter(isVisible)
                .map(el => ({el, text: normalize(el.innerText || el.textContent), rect: el.getBoundingClientRect()}))
                .filter(o => wanted.includes(o.text))
                // Avoid huge parent containers that contain the whole form.
                .filter(o => (o.rect.width * o.rect.height) < 120000)
                .sort((a,b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);

            const fieldSelector = [
                allowInput ? 'input:not([type=hidden]):not([type=checkbox]):not([type=radio])' : '',
                allowTextarea ? 'textarea' : ''
            ].filter(Boolean).join(',');

            let fields = [...document.querySelectorAll(fieldSelector)]
                .filter(isVisible)
                .filter(el => !el.disabled && !el.readOnly)
                .map(el => ({el, rect: el.getBoundingClientRect()}));

            for (const lab of possibleLabels) {
                const below = fields
                    .filter(f => f.rect.top >= lab.rect.bottom - 10)
                    .filter(f => f.rect.top <= lab.rect.bottom + 260)
                    .map(f => {
                        const dy = Math.max(0, f.rect.top - lab.rect.bottom);
                        const dx = Math.abs(f.rect.left - lab.rect.left);
                        return { ...f, score: dy * 10 + dx };
                    })
                    .sort((a,b) => a.score - b.score);
                if (below.length) {
                    setValue(below[0].el, value);
                    return {ok:true, label: lab.text, tag: below[0].el.tagName, placeholder: below[0].el.placeholder || '', y: Math.round(below[0].rect.top), valueSet: value};
                }
            }
            return {ok:false, reason:'field-not-found', labels};
        }""",
        {"labels": label_names, "value": value, "allowTextarea": allow_textarea, "allowInput": allow_input},
    )


def set_field_in_labeled_section(page, label_names, value, allow_textarea=True, allow_input=True, clear_only=False):
    """Set/clear the field inside the exact label section only.

    This prevents Product Tags from jumping into SKU and prevents Slug/SKU mistakes.
    It looks for the exact label, then chooses the visible input/textarea between that label
    and the next known field label below it.
    """
    if isinstance(label_names, str):
        label_names = [label_names]
    value = "" if clear_only else (value or "")
    js = r"""({labels, value, allowTextarea, allowInput}) => {
            const sectionLabels = [
                'PRODUCT NAME', 'SLUG', 'PRODUCT TAGS', 'SKU', 'CATEGORY', 'BRAND',
                'DESCRIPTION', 'SHORT DESCRIPTION', 'SEO TITLE', 'SEO DESCRIPTION', 'SEO KEYWORDS'
            ];
            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function normalize(s) {
                return (s || '')
                    .replace(/\*/g, '')
                    .replace(/\s+/g, ' ')
                    .trim()
                    .toUpperCase();
            }
            function setValue(el, val) {
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.focus();
                if (el.isContentEditable) {
                    el.innerText = val || '';
                    el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:val || ''}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    el.blur();
                    return;
                }
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                setter.call(el, val || '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }
            const wanted = labels.map(normalize);
            const labelSelector = 'label, div, span, p, h1, h2, h3, h4, h5, strong';
            const allLabels = [...document.querySelectorAll(labelSelector)]
                .filter(isVisible)
                .map(el => ({el, text: normalize(el.innerText || el.textContent), rect: el.getBoundingClientRect()}))
                .filter(o => o.text && o.text.length <= 80)
                .filter(o => (o.rect.width * o.rect.height) < 160000)
                .sort((a,b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);

            const lab = allLabels.find(o => wanted.includes(o.text));
            if (!lab) return {ok:false, reason:'label-not-found', labels};

            const nextKnown = allLabels.find(o =>
                o.rect.top > lab.rect.top + 8 &&
                sectionLabels.includes(o.text) &&
                !wanted.includes(o.text)
            );
            const bottomLimit = nextKnown ? nextKnown.rect.top - 4 : lab.rect.bottom + 360;

            const fieldSelectors = [];
            if (allowInput) fieldSelectors.push('input:not([type=hidden]):not([type=checkbox]):not([type=radio])');
            if (allowTextarea) fieldSelectors.push('textarea');
            fieldSelectors.push('[contenteditable="true"]');

            let fields = [...document.querySelectorAll(fieldSelectors.join(','))]
                .filter(isVisible)
                .filter(el => !el.disabled && !el.readOnly)
                .map(el => ({el, rect: el.getBoundingClientRect(), ph: el.placeholder || '', tag: el.tagName}))
                .filter(f => f.rect.top >= lab.rect.bottom - 12 && f.rect.top < bottomLimit)
                .sort((a,b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);

            if (!fields.length) {
                fields = [...document.querySelectorAll(fieldSelectors.join(','))]
                    .filter(isVisible)
                    .filter(el => !el.disabled && !el.readOnly)
                    .map(el => ({el, rect: el.getBoundingClientRect(), ph: el.placeholder || '', tag: el.tagName}))
                    .filter(f => f.rect.top >= lab.rect.top && f.rect.top < bottomLimit)
                    .sort((a,b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
            }

            if (!fields.length) return {ok:false, reason:'field-not-found-in-section', label:lab.text, bottomLimit:Math.round(bottomLimit)};
            setValue(fields[0].el, value);
            return {
                ok:true,
                label:lab.text,
                tag:fields[0].tag,
                placeholder:fields[0].ph,
                y:Math.round(fields[0].rect.top),
                nextLabel: nextKnown ? nextKnown.text : null,
                valueSet:value
            };
        }"""
    return page.evaluate(js, {"labels": label_names, "value": value, "allowTextarea": allow_textarea, "allowInput": allow_input})

def fill_by_placeholder(page, placeholder_part, value):
    res = page.evaluate(
        """({placeholderPart, value}) => {
            function isVisible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            }
            function setValue(el, val) {
                el.scrollIntoView({block:'center'});
                el.focus();
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }
            const needle = placeholderPart.toLowerCase();
            const el = [...document.querySelectorAll('input:not([type=hidden]), textarea')]
                .filter(isVisible)
                .find(e => (e.placeholder || '').toLowerCase().includes(needle));
            if (!el) return {ok:false};
            setValue(el, value || '');
            return {ok:true, tag:el.tagName, placeholder:el.placeholder};
        }""",
        {"placeholderPart": placeholder_part, "value": value or ""},
    )
    return res


def clear_sku_only(page):
    log("Clearing SKU only...")
    res = set_field_in_labeled_section(page, ["SKU"], "", allow_textarea=False, allow_input=True, clear_only=True)
    log(f"SKU clear result: {res}")


def _click_field_below_label(page, label_text: str, preferred_placeholder: str = ""):
    """Click the actual field below an exact label. Works for custom selects/comboboxes."""
    return page.evaluate(
        r"""({labelText, preferredPlaceholder}) => {
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            }
            function norm(s) {
                return (s || '').replace(/\*/g, '').replace(/\s+/g, ' ').trim().toUpperCase();
            }
            const wanted = norm(labelText);
            const labels = [...document.querySelectorAll('label, div, span, p, strong')]
                .filter(visible)
                .map(el => ({el, text:norm(el.innerText || el.textContent), r:el.getBoundingClientRect()}))
                .filter(o => o.text === wanted)
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
            if (!labels.length) return {ok:false, reason:'label-not-found', label:labelText};
            const lab = labels[0];

            const selectors = [
                'input:not([type=hidden]):not([type=checkbox]):not([type=radio])',
                'select',
                '[role="combobox"]',
                'button',
                '[role="button"]',
                'div'
            ].join(',');

            let fields = [...document.querySelectorAll(selectors)]
                .filter(visible)
                .filter(el => !el.disabled && !el.readOnly)
                .map(el => ({
                    el,
                    r:el.getBoundingClientRect(),
                    ph:el.getAttribute('placeholder') || '',
                    text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(),
                    tag:el.tagName,
                    role:el.getAttribute('role') || '',
                    aria:el.getAttribute('aria-label') || ''
                }))
                .filter(o => o.r.top >= lab.r.bottom - 20 && o.r.top < lab.r.bottom + 220)
                .filter(o => (o.r.width * o.r.height) < 200000)
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);

            if (preferredPlaceholder) {
                const needle = preferredPlaceholder.toLowerCase();
                const byPlaceholder = fields.find(o => (o.ph || '').toLowerCase().includes(needle));
                if (byPlaceholder) fields = [byPlaceholder, ...fields.filter(x => x !== byPlaceholder)];
            }

            if (!fields.length) return {ok:false, reason:'field-not-found', label:labelText};
            const f = fields[0];
            f.el.scrollIntoView({block:'center', inline:'nearest'});
            f.el.click();
            return {
                ok:true,
                label:labelText,
                tag:f.tag,
                role:f.role,
                placeholder:f.ph,
                text:f.text,
                y:Math.round(f.r.top)
            };
        }""",
        {"labelText": label_text, "preferredPlaceholder": preferred_placeholder},
    )


def _click_visible_option(page, option_text: str, exact: bool = True):
    """Click a visible dropdown option/button by exact text, then by best contains fallback."""
    option_text = (option_text or '').strip()
    if not option_text:
        return False

    # First use Playwright roles/text for common custom dropdowns.
    tries = []
    if exact:
        tries.extend([
            lambda: page.get_by_role("option", name=re.compile(rf"^\s*{re.escape(option_text)}\s*$", re.I)).first.click(timeout=1800, force=True),
            lambda: page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(option_text)}\s*$", re.I)).first.click(timeout=1800, force=True),
            lambda: page.get_by_text(option_text, exact=True).last.click(timeout=1800, force=True),
        ])
    tries.extend([
        lambda: page.get_by_role("option", name=re.compile(re.escape(option_text), re.I)).first.click(timeout=1800, force=True),
        lambda: page.get_by_role("button", name=re.compile(re.escape(option_text), re.I)).first.click(timeout=1800, force=True),
    ])
    for attempt in tries:
        try:
            attempt()
            page.wait_for_timeout(350)
            return True
        except Exception:
            pass

    # JS fallback: choose the smallest visible element whose text matches.
    try:
        clicked = page.evaluate(
            r"""({text, exact}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                const wanted = text.trim().toLowerCase();
                const els = [...document.querySelectorAll('[role="option"], button, [role="button"], div, span')]
                    .filter(visible)
                    .map(el => ({
                        el,
                        t:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(),
                        r:el.getBoundingClientRect()
                    }))
                    .filter(o => o.t && o.t.length <= 120)
                    .filter(o => exact ? o.t.toLowerCase() === wanted : o.t.toLowerCase().includes(wanted))
                    .sort((a,b) => (a.r.width*a.r.height) - (b.r.width*b.r.height));
                if (els.length) { els[0].el.click(); return {ok:true, text:els[0].t}; }
                return {ok:false};
            }""",
            {"text": option_text, "exact": exact},
        )
        if clicked and clicked.get('ok'):
            page.wait_for_timeout(350)
            return True
    except Exception:
        pass
    return False


def fill_brand_dropdown(page, brand: str):
    """Fill Brand combobox properly: click Search brand field, type brand, click matching option."""
    brand = (brand or '').strip()
    if not brand:
        return

    log(f"Filling Basics → Brand dropdown: {brand}")

    # Main path: actual searchable brand textbox has placeholder 'Search brand...'.
    opened = False
    try:
        brand_box = page.get_by_placeholder(re.compile(r"search\s+brand", re.I)).first
        if brand_box.count() > 0:
            brand_box.click(timeout=2500, force=True)
            page.wait_for_timeout(250)
            # Many custom comboboxes use an input inside the dropdown; fill it if possible.
            try:
                brand_box.fill(brand, timeout=1500)
            except Exception:
                page.keyboard.press("Control+A")
                page.keyboard.insert_text(brand)
            opened = True
    except Exception:
        opened = False

    if not opened:
        res = _click_field_below_label(page, "BRAND", preferred_placeholder="Search brand")
        log(f"  Brand field open result: {res}")
        if not res or not res.get('ok'):
            return
        page.wait_for_timeout(250)
        try:
            page.keyboard.press("Control+A")
            page.keyboard.insert_text(brand)
        except Exception:
            pass

    page.wait_for_timeout(800)

    # Select exact dropdown result like 'Amul'.
    clicked = _click_visible_option(page, brand, exact=True)
    if not clicked:
        clicked = _click_visible_option(page, brand, exact=False)
    if not clicked:
        log("  Brand option not clicked by text. Pressing Enter fallback.")
        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
        except Exception:
            pass
    else:
        log(f"  Brand selected: {brand}")


def fill_product_type_dropdown(page, product_type: str):
    """Select Product Type dropdown value: Simple Product / Variable Product / Group-Bundle Product."""
    product_type = (product_type or '').strip()
    if not product_type:
        return

    log(f"Filling Basics → Product Type dropdown: {product_type}")

    # If it is a real <select>, set it directly by option text/value.
    try:
        direct = page.evaluate(
            r"""(productType) => {
                function visible(el) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                function norm(s) { return (s || '').replace(/\*/g,'').replace(/\s+/g,' ').trim().toUpperCase(); }
                const labels = [...document.querySelectorAll('label, div, span, p, strong')]
                    .filter(visible)
                    .map(el => ({el, text:norm(el.innerText || el.textContent), r:el.getBoundingClientRect()}))
                    .filter(o => o.text === 'PRODUCT TYPE')
                    .sort((a,b) => a.r.top - b.r.top);
                if (!labels.length) return {ok:false, reason:'label-not-found'};
                const lab = labels[0];
                const selects = [...document.querySelectorAll('select')]
                    .filter(visible)
                    .map(el => ({el, r:el.getBoundingClientRect()}))
                    .filter(o => o.r.top >= lab.r.bottom - 20 && o.r.top < lab.r.bottom + 220)
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (!selects.length) return {ok:false, reason:'no-select'};
                const sel = selects[0].el;
                const wanted = productType.trim().toLowerCase();
                const opt = [...sel.options].find(o => (o.text || '').trim().toLowerCase() === wanted) ||
                            [...sel.options].find(o => (o.text || '').trim().toLowerCase().includes(wanted)) ||
                            [...sel.options].find(o => (o.value || '').trim().toLowerCase().includes(wanted));
                if (!opt) return {ok:false, reason:'option-not-found'};
                sel.value = opt.value;
                sel.dispatchEvent(new Event('input', {bubbles:true}));
                sel.dispatchEvent(new Event('change', {bubbles:true}));
                return {ok:true, method:'select', selected:opt.text || opt.value};
            }""",
            product_type,
        )
        if direct and direct.get('ok'):
            log(f"  Product Type selected directly: {direct}")
            page.wait_for_timeout(700)
            return
    except Exception:
        pass

    # Custom select path: click Product Type field and choose visible option.
    res = _click_field_below_label(page, "PRODUCT TYPE")
    log(f"  Product Type field open result: {res}")
    if not res or not res.get('ok'):
        return
    page.wait_for_timeout(500)

    clicked = _click_visible_option(page, product_type, exact=True)
    if not clicked:
        clicked = _click_visible_option(page, product_type, exact=False)
    if not clicked:
        log("  Product Type option not clicked by text. Keyboard fallback.")
        try:
            page.keyboard.insert_text(product_type)
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")
            page.wait_for_timeout(700)
        except Exception:
            pass
    else:
        log(f"  Product Type selected: {product_type}")


def fill_basics(page, data: ProductData):
    click_tab(page, "Basics")
    b = data.basics
    log("Filling Basics. Brand/Category/SKU will NOT be touched. Slug will be cleared only. Product Tags will stay in Product Tags only.")

    if b.get("Product Type"):
        fill_product_type_dropdown(page, b.get("Product Type"))
        time.sleep(0.5)

    # Brand is intentionally NOT automated now.
    # You will select Brand manually, so the script does not risk touching Category/Brand dropdowns.
    if b.get("Brand"):
        log("Skipping Brand automation. Please select Brand manually.")
        time.sleep(0.10)

    if b.get("Product Name"):
        log("Filling Basics → Product Name")
        res = set_field_in_labeled_section(page, ["PRODUCT NAME", "Product Name"], b.get("Product Name", ""), allow_textarea=False, allow_input=True)
        log(f"  result: {res}")
        time.sleep(0.25)

    log("Clearing Slug so Prockured can auto-generate it.")
    res = set_field_in_labeled_section(page, ["SLUG", "Slug"], "", allow_textarea=False, allow_input=True, clear_only=True)
    log(f"  Slug clear result: {res}")
    time.sleep(0.25)

    if b.get("Product Tags"):
        log("Filling Basics → Product Tags")
        # Allow input and textarea, but only inside the Product Tags section and before the SKU label.
        res = set_field_in_labeled_section(page, ["PRODUCT TAGS", "Product Tags"], b.get("Product Tags", ""), allow_textarea=True, allow_input=True)
        log(f"  result: {res}")
        time.sleep(0.25)

    if b.get("Description"):
        log("Filling Basics → Description")
        res = set_field_in_labeled_section(page, ["DESCRIPTION", "Description"], b.get("Description", ""), allow_textarea=True, allow_input=False)
        log(f"  result: {res}")
        time.sleep(0.25)

    if b.get("Short Description"):
        log("Filling Basics → Short Description")
        res = set_field_in_labeled_section(page, ["SHORT DESCRIPTION", "Short Description"], b.get("Short Description", ""), allow_textarea=True, allow_input=False)
        log(f"  result: {res}")
        time.sleep(0.25)

    if b.get("Model Name"):
        model_name = str(b.get("Model Name") or "").strip()
        log(f"Filling Basics → Model Name: {model_name}")
        res = set_field_in_labeled_section(
            page,
            ["MODEL NAME", "Model Name"],
            model_name,
            allow_textarea=False,
            allow_input=True,
        )
        if not res or not res.get("ok"):
            res = fill_by_placeholder(page, "optional model identifier", model_name)
        log(f"  Model Name result: {res}")
        time.sleep(0.25)

    log("Basics fill done.")

def visible_fields(page):
    return page.evaluate(
        r"""() => {
            function isVisible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            }
            return [...document.querySelectorAll('input:not([type=hidden]), textarea')]
                .filter(isVisible)
                .map((el, i) => {
                    const r = el.getBoundingClientRect();
                    let label = '';
                    const labels = [...document.querySelectorAll('label, div, span, p, h1, h2, h3, h4, strong')]
                        .filter(isVisible)
                        .map(x => ({t:(x.innerText||x.textContent||'').trim().replace(/\s+/g,' '), r:x.getBoundingClientRect()}))
                        .filter(x => x.t && x.r.top < r.top && Math.abs(x.r.left - r.left) < 80)
                        .sort((a,b) => (r.top-a.r.top) - (r.top-b.r.top));
                    if (labels[0]) label = labels[0].t;
                    return {index:i+1, tag:el.tagName, type:el.type||'', placeholder:el.placeholder||'', value:el.value||'', label, x:Math.round(r.left), y:Math.round(r.top), w:Math.round(r.width), h:Math.round(r.height)};
                });
        }"""
    )


def fill_seo(page, data: ProductData):
    click_tab(page, "SEO")
    s = data.seo
    title = s.get("SEO Title", "")
    desc = s.get("SEO Description", "")
    keywords = s.get("SEO Keywords", "")
    log("Filling SEO by confirmed visible field order:")
    log("  1st field = SEO Title")
    log("  2nd field = SEO Description")
    log("  3rd field = SEO Keywords")

    res = page.evaluate(
        """({title, desc, keywords}) => {
            function isVisible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            }
            function setValue(el, val) {
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.focus();
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                setter.call(el, val || '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }
            const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea')]
                .filter(isVisible)
                .filter(el => !el.disabled && !el.readOnly)
                .map(el => ({el, r: el.getBoundingClientRect(), ph: el.placeholder || ''}))
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
            const details = fields.map((f, i) => ({index:i+1, tag:f.el.tagName, placeholder:f.ph, y:Math.round(f.r.top), oldValue:f.el.value||''}));
            if (fields.length >= 1) setValue(fields[0].el, title);
            if (fields.length >= 2) setValue(fields[1].el, desc);
            if (fields.length >= 3) setValue(fields[2].el, keywords);
            return {count: fields.length, details};
        }""",
        {"title": title, "desc": desc, "keywords": keywords},
    )
    for f in res.get("details", []):
        log(f"  field #{f['index']}: {f['tag']} placeholder='{f['placeholder']}' y={f['y']}")
    log("SEO fill done.")



# ----------------------------
# Attribute automation
# Restored from the earlier working attribute engine.
# This uses Playwright locators for the actual Name/Value inputs instead of the newer JS-only method.
# ----------------------------

CLEAR_ATTRIBUTES_BY_DEFAULT = True
PRESS_ENTER_FOR_ATTRIBUTE_VALUE = True


def click_add_attribute(page):
    """Click the Add Attribute button reliably."""
    try:
        page.get_by_role("button", name=re.compile(r"add\s+attribute", re.I)).click()
    except Exception:
        try:
            page.locator("button").filter(has_text=re.compile(r"Add\s+Attribute", re.I)).last.click()
        except Exception:
            # JS fallback for custom button markup.
            ok = page.evaluate(
                r"""() => {
                    function visible(el) {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    }
                    const els = [...document.querySelectorAll('button, div, span')].filter(visible);
                    const el = els.find(e => /add\s+attribute/i.test((e.innerText || e.textContent || '').trim()));
                    if (el) { el.click(); return true; }
                    return false;
                }"""
            )
            if not ok:
                raise RuntimeError("Could not click Add Attribute button.")
    page.wait_for_timeout(600)


def find_empty_name_input(page):
    """Return a usable attribute Name input and its bounding box.

    v16 fix:
    Prockured's Create Product page can keep a default/stale attribute card where
    the Name field already contains something like "Product Type" but the value
    field is still empty. The older engine only accepted empty Name fields whose
    placeholder contained Name/Material, so it could fail before filling even the
    first attribute. This version finds attribute cards from their value input,
    then returns either an empty Name field or a safe reusable blank card.
    """
    try:
        info = page.evaluate(
            r"""() => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }
                function norm(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
                function rectObj(el) {
                    const r = el.getBoundingClientRect();
                    return {x:r.x, y:r.y, left:r.left, top:r.top, right:r.right, bottom:r.bottom, width:r.width, height:r.height};
                }
                function inputIndex(el) {
                    return [...document.querySelectorAll('input')].indexOf(el);
                }
                function isValueInput(el) {
                    const ph = norm(el.getAttribute('placeholder'));
                    const aria = norm(el.getAttribute('aria-label'));
                    return ph.includes('add value') || ph.includes('press enter') || aria.includes('value');
                }
                function isTextInput(el) {
                    const type = norm(el.getAttribute('type'));
                    return el.tagName === 'INPUT' && !['hidden','checkbox','radio','file'].includes(type);
                }
                function findBlock(valueInput) {
                    let node = valueInput.parentElement;
                    for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
                        const r = node.getBoundingClientRect();
                        const text = norm(node.innerText || node.textContent);
                        const inputs = [...node.querySelectorAll('input')].filter(visible);
                        const hasNameLike = inputs.some(inp => isTextInput(inp) && !isValueInput(inp));
                        const hasValue = inputs.some(inp => isTextInput(inp) && isValueInput(inp));
                        const hasToggles = text.includes('filters') && text.includes('variants');
                        const buttons = [...node.querySelectorAll('button')].filter(visible);
                        const hasTrash = buttons.some(btn => {
                            const t = norm(btn.innerText || btn.textContent);
                            const aria = norm(btn.getAttribute('aria-label'));
                            const title = norm(btn.getAttribute('title'));
                            const html = norm(btn.innerHTML || '');
                            const br = btn.getBoundingClientRect();
                            return aria.includes('delete') || title.includes('delete') || html.includes('trash') || html.includes('lucide-trash') || (!!btn.querySelector('svg') && !t && br.width <= 90 && br.height <= 90);
                        });
                        if (hasNameLike && hasValue && (hasToggles || hasTrash) && r.width > 300 && r.height > 80 && r.height < 900) {
                            return node;
                        }
                    }
                    return null;
                }
                function hasRealValueChip(block) {
                    const boilerplate = new Set(['filters','variants','add value and press enter (e.g. red)','name (e.g. material)']);
                    const valueInputs = [...block.querySelectorAll('input')]
                        .filter(visible)
                        .filter(inp => isTextInput(inp) && isValueInput(inp));
                    if (valueInputs.some(inp => norm(inp.value))) return true;

                    // Chips are small text elements inside the value area. Ignore labels and button names.
                    const texts = [...block.querySelectorAll('span, div, button')]
                        .filter(visible)
                        .map(el => norm(el.innerText || el.textContent || ''))
                        .filter(t => t && t.length < 100)
                        .filter(t => !boilerplate.has(t))
                        .filter(t => !t.includes('add value') && !t.includes('press enter'))
                        .filter(t => !t.includes('filters') && !t.includes('variants'));
                    // A non-empty name field also appears as text in some wrappers; do not treat it as a chip.
                    return texts.length > 1;
                }

                const allInputs = [...document.querySelectorAll('input')];
                const valueInputs = allInputs.filter(visible).filter(inp => isTextInput(inp) && isValueInput(inp));
                const candidates = [];

                for (const valueInput of valueInputs) {
                    const block = findBlock(valueInput);
                    if (!block) continue;
                    const br = rectObj(block);
                    const textInputs = [...block.querySelectorAll('input')]
                        .filter(visible)
                        .filter(inp => isTextInput(inp) && !isValueInput(inp))
                        .map(inp => ({el:inp, r:rectObj(inp), value:(inp.value || '').trim(), ph:inp.getAttribute('placeholder') || '', index:inputIndex(inp)}))
                        .filter(o => o.index >= 0)
                        .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                    if (!textInputs.length) continue;
                    const name = textInputs[0];
                    const empty = !name.value;
                    const reusable = empty || !hasRealValueChip(block);
                    if (reusable) {
                        candidates.push({
                            index:name.index,
                            empty,
                            reusable,
                            value:name.value,
                            placeholder:name.ph,
                            x:name.r.x,
                            y:name.r.y,
                            width:name.r.width,
                            height:name.r.height,
                            blockTop:br.top
                        });
                    }
                }

                candidates.sort((a,b) => {
                    // Empty name fields are best. Reusable stale blank cards are second.
                    if (a.empty !== b.empty) return a.empty ? -1 : 1;
                    return a.blockTop - b.blockTop || a.x - b.x;
                });

                return candidates[0] || null;
            }"""
        )
        if not info:
            return None, None
        loc = page.locator("input").nth(int(info["index"]))
        box = {"x": info["x"], "y": info["y"], "width": info["width"], "height": info["height"]}
        if info.get("value"):
            log(f"  Reusing blank/stale Attribute Name field currently showing '{info.get('value')}'.")
        return loc, box
    except Exception as e:
        log(f"  find_empty_name_input failed: {e}")
        return None, None


def find_value_input_for_name(page, name_box):
    """Find the value input belonging to the same attribute block as the given name input."""
    try:
        info = page.evaluate(
            r"""(nameBox) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }
                function norm(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
                function rectObj(el) {
                    const r = el.getBoundingClientRect();
                    return {x:r.x, y:r.y, left:r.left, top:r.top, right:r.right, bottom:r.bottom, width:r.width, height:r.height};
                }
                function inputIndex(el) { return [...document.querySelectorAll('input')].indexOf(el); }
                function isValueInput(el) {
                    const ph = norm(el.getAttribute('placeholder'));
                    const aria = norm(el.getAttribute('aria-label'));
                    return ph.includes('add value') || ph.includes('press enter') || aria.includes('value');
                }
                function isTextInput(el) {
                    const type = norm(el.getAttribute('type'));
                    return el.tagName === 'INPUT' && !['hidden','checkbox','radio','file'].includes(type);
                }
                function nearBox(el) {
                    const r = el.getBoundingClientRect();
                    return Math.abs(r.top - nameBox.y) <= 40 && Math.abs(r.left - nameBox.x) <= 160;
                }
                const nameInput = [...document.querySelectorAll('input')]
                    .filter(visible)
                    .filter(inp => isTextInput(inp) && !isValueInput(inp))
                    .map(inp => ({el:inp, r:rectObj(inp), score:Math.abs(inp.getBoundingClientRect().top - nameBox.y) + Math.abs(inp.getBoundingClientRect().left - nameBox.x)}))
                    .filter(o => nearBox(o.el))
                    .sort((a,b) => a.score - b.score)[0]?.el || null;
                if (!nameInput) return null;

                let block = null;
                let node = nameInput.parentElement;
                for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
                    const r = node.getBoundingClientRect();
                    const text = norm(node.innerText || node.textContent);
                    const valueInputs = [...node.querySelectorAll('input')]
                        .filter(visible)
                        .filter(inp => isTextInput(inp) && isValueInput(inp));
                    if (valueInputs.length && (text.includes('filters') || text.includes('variants') || r.height > 80) && r.width > 300 && r.height < 900) {
                        block = node;
                        break;
                    }
                }
                if (!block) return null;

                const values = [...block.querySelectorAll('input')]
                    .filter(visible)
                    .filter(inp => isTextInput(inp) && isValueInput(inp))
                    .map(inp => ({el:inp, r:rectObj(inp), index:inputIndex(inp)}))
                    .filter(o => o.index >= 0)
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (!values.length) return null;
                const v = values[0];
                return {index:v.index, x:v.r.x, y:v.r.y, width:v.r.width, height:v.r.height};
            }""",
            name_box,
        )
        if not info:
            return None, None
        loc = page.locator("input").nth(int(info["index"]))
        box = {"x": info["x"], "y": info["y"], "width": info["width"], "height": info["height"]}
        return loc, box
    except Exception as e:
        log(f"  find_value_input_for_name failed: {e}")
        return None, None


def click_first_attribute_delete_button(page) -> bool:
    """Delete one visible attribute card from the Attributes tab, if one exists."""
    js = r"""
    () => {
        function visible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const st = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
        }
        function norm(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
        function isValueInput(el) {
            const ph = norm(el.getAttribute('placeholder'));
            const aria = norm(el.getAttribute('aria-label'));
            return ph.includes('add value') || ph.includes('press enter') || aria.includes('value');
        }
        function isTextInput(el) {
            const type = norm(el.getAttribute('type'));
            return el.tagName === 'INPUT' && !['hidden','checkbox','radio','file'].includes(type);
        }
        function clickEl(el) {
            el.scrollIntoView({block:'center', inline:'nearest'});
            const r = el.getBoundingClientRect();
            const x = r.left + r.width / 2;
            const y = r.top + r.height / 2;
            try { el.click(); return true; } catch(e) {}
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, clientX:x, clientY:y}));
            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, clientX:x, clientY:y}));
            el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, clientX:x, clientY:y}));
            return true;
        }
        function findBlock(valueInput) {
            let node = valueInput.parentElement;
            for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
                const r = node.getBoundingClientRect();
                const text = norm(node.innerText || node.textContent);
                const inputs = [...node.querySelectorAll('input')].filter(visible);
                const hasNameLike = inputs.some(inp => isTextInput(inp) && !isValueInput(inp));
                const hasValue = inputs.some(inp => isTextInput(inp) && isValueInput(inp));
                const hasToggles = text.includes('filters') && text.includes('variants');
                const buttons = [...node.querySelectorAll('button')].filter(visible);
                const hasTrash = buttons.some(btn => {
                    const t = norm(btn.innerText || btn.textContent);
                    const aria = norm(btn.getAttribute('aria-label'));
                    const title = norm(btn.getAttribute('title'));
                    const html = norm(btn.innerHTML || '');
                    const br = btn.getBoundingClientRect();
                    return aria.includes('delete') || title.includes('delete') || html.includes('trash') || html.includes('lucide-trash') || (!!btn.querySelector('svg') && !['filters','variants'].includes(t) && br.width <= 90 && br.height <= 90);
                });
                if (hasNameLike && hasValue && (hasToggles || hasTrash) && r.width > 300 && r.height > 80 && r.height < 900) {
                    return node;
                }
            }
            return null;
        }

        const valueInput = [...document.querySelectorAll('input')]
            .filter(visible)
            .filter(inp => isTextInput(inp) && isValueInput(inp))
            .sort((a,b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top || a.getBoundingClientRect().left - b.getBoundingClientRect().left)[0];
        if (!valueInput) return false;

        const block = findBlock(valueInput);
        if (!block) return false;

        const buttons = [...block.querySelectorAll('button')]
            .filter(visible)
            .map(btn => {
                const r = btn.getBoundingClientRect();
                const t = norm(btn.innerText || btn.textContent);
                const aria = norm(btn.getAttribute('aria-label'));
                const title = norm(btn.getAttribute('title'));
                const html = norm(btn.innerHTML || '');
                const hasSvg = !!btn.querySelector('svg');
                let score = 0;
                if (aria.includes('delete') || title.includes('delete') || t.includes('delete')) score -= 1000;
                if (html.includes('trash') || html.includes('lucide-trash')) score -= 800;
                if (hasSvg) score -= 100;
                if (t.includes('filters') || t.includes('variants') || t.includes('add attribute')) score += 2000;
                score -= r.left / 20;       // right-most icon is usually delete
                score += (r.width * r.height) / 1000;
                return {btn, text:t, aria, title, hasSvg, x:r.left, y:r.top, w:r.width, h:r.height, score};
            })
            .filter(o => !o.text.includes('filters') && !o.text.includes('variants') && !o.text.includes('add attribute'))
            .filter(o => o.hasSvg || o.aria.includes('delete') || o.title.includes('delete') || o.text.includes('delete'))
            .sort((a,b) => a.score - b.score);

        if (!buttons.length) return false;
        clickEl(buttons[0].btn);
        return true;
    }
    """
    try:
        return bool(page.evaluate(js))
    except Exception as e:
        log(f"  Attribute delete click failed: {e}")
        return False


def clear_existing_attributes(page, max_clicks: int = 80):
    log("Clearing old Attribute blocks...")
    deleted = 0
    for _ in range(max_clicks):
        if stop_requested:
            log("Stopped while clearing attributes.")
            break
        clicked = click_first_attribute_delete_button(page)
        if not clicked:
            break
        deleted += 1
        page.wait_for_timeout(350)
    log(f"Deleted/cleared attribute blocks: {deleted}")

def dismiss_name_dropdown(page):
    """Close the suggestion dropdown after typing attribute name."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    except Exception:
        pass
    try:
        page.evaluate("""() => { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); }""")
        page.wait_for_timeout(100)
    except Exception:
        pass
    # click a safe empty text area if present; this was part of the earlier stable method
    try:
        safe_text = page.get_by_text("Reuse existing attribute names and values", exact=False)
        if safe_text.count() > 0:
            safe_text.first.click(timeout=700, force=True)
            page.wait_for_timeout(120)
    except Exception:
        pass



def set_attribute_toggle_by_name(page, attribute_name: str, toggle_name: str = "Variants", desired: bool = True) -> bool:
    """Set a Filters/Variants checkbox for an attribute by attribute name.

    This is safer than coordinate-based clicking. It finds the attribute card whose
    Name input value equals the given attribute name, then selects the real checkbox
    on the same row as the exact toggle label. It only clicks when the checkbox is
    not already in the desired state, so it will not accidentally deselect Variants.
    """
    attribute_name = (attribute_name or "").strip()
    toggle_name = (toggle_name or "Variants").strip()
    if not attribute_name or not toggle_name:
        return False

    try:
        res = page.evaluate(
            r"""({attributeName, toggleName, desired}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
                }
                function norm(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
                function rect(el) { const r = el.getBoundingClientRect(); return {left:r.left, top:r.top, right:r.right, bottom:r.bottom, width:r.width, height:r.height, cx:r.left+r.width/2, cy:r.top+r.height/2}; }
                function sameLine(a, b, tol=34) { return Math.abs(a.cy - b.cy) <= tol; }
                function clickOnce(el) {
                    el.scrollIntoView({block:'center', inline:'nearest'});
                    const r = el.getBoundingClientRect();
                    const x = r.left + r.width / 2;
                    const y = r.top + r.height / 2;
                    try { el.click(); return true; } catch(e) {}
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                    el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                    el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                    return true;
                }
                function setNativeChecked(input, val) {
                    try {
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked').set;
                        setter.call(input, !!val);
                    } catch(e) {
                        input.checked = !!val;
                    }
                    input.dispatchEvent(new Event('input', {bubbles:true}));
                    input.dispatchEvent(new Event('change', {bubbles:true}));
                }
                function checkedState(el) {
                    if (!el) return false;
                    if (el.matches && el.matches('input[type="checkbox"]')) return !!el.checked;
                    const inner = el.querySelector && el.querySelector('input[type="checkbox"]');
                    if (inner) return !!inner.checked;
                    const ariaChecked = norm(el.getAttribute && el.getAttribute('aria-checked'));
                    const ariaPressed = norm(el.getAttribute && el.getAttribute('aria-pressed'));
                    return ariaChecked === 'true' || ariaPressed === 'true';
                }

                const wantedAttr = norm(attributeName);
                const wantedToggle = norm(toggleName);

                // Find the attribute Name input by its value, not by old screen coordinates.
                const nameInputs = [...document.querySelectorAll('input')]
                    .filter(visible)
                    .map(el => ({el, r:rect(el), value:norm(el.value), ph:norm(el.getAttribute('placeholder'))}))
                    .filter(o => o.value === wantedAttr || (o.value.includes(wantedAttr) && (o.ph.includes('name') || o.ph.includes('material'))))
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);

                if (!nameInputs.length) {
                    return {ok:false, reason:'attribute-name-input-not-found', attributeName};
                }

                const nameInput = nameInputs[0].el;

                // Find the smallest attribute card containing this Name input, a value field, and toggle labels.
                let block = null;
                let node = nameInput.parentElement;
                for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
                    const r = node.getBoundingClientRect();
                    const text = norm(node.innerText || node.textContent);
                    const hasValueInput = [...node.querySelectorAll('input')].some(inp => {
                        const ph = norm(inp.getAttribute('placeholder'));
                        return visible(inp) && (ph.includes('add value') || ph.includes('press enter'));
                    });
                    const hasToggleText = text.includes('filters') && text.includes('variants');
                    if (hasValueInput && hasToggleText && r.width > 350 && r.height > 80 && r.height < 900) {
                        block = node;
                        break;
                    }
                }
                if (!block) return {ok:false, reason:'attribute-block-not-found', attributeName};

                const labels = [...block.querySelectorAll('label, span, div, button')]
                    .filter(visible)
                    .map(el => ({el, text:norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title')), r:rect(el)}))
                    .filter(o => o.text === wantedToggle || o.text.endsWith(' ' + wantedToggle) || o.text.startsWith(wantedToggle + ' '))
                    .sort((a,b) => (a.r.width*a.r.height) - (b.r.width*b.r.height));

                const checkboxes = [...block.querySelectorAll('input[type="checkbox"]')]
                    .filter(visible)
                    .map(el => ({el, r:rect(el)}));

                let target = null;
                let matchedLabel = null;
                let method = '';

                // Best case: real checkbox with aria/name/title exactly Variants.
                target = checkboxes.find(o => {
                    const aria = norm(o.el.getAttribute('aria-label') || o.el.getAttribute('title') || o.el.name || o.el.id);
                    return aria === wantedToggle || aria.includes(wantedToggle);
                })?.el || null;
                if (target) method = 'checkbox-aria';

                // Normal Prockured UI: label text "Variants" is on the same row as the checkbox.
                if (!target && labels.length && checkboxes.length) {
                    let best = null;
                    for (const lab of labels) {
                        for (const cb of checkboxes) {
                            if (!sameLine(lab.r, cb.r)) continue;
                            const horizontalGap = Math.abs(lab.r.cx - cb.r.cx);
                            const cbBeforeLabel = cb.r.cx <= lab.r.cx + 15;
                            const score = horizontalGap + (cbBeforeLabel ? 0 : 120) + Math.abs(lab.r.cy - cb.r.cy) * 5;
                            if (!best || score < best.score) best = {checkbox:cb.el, label:lab, score};
                        }
                    }
                    if (best) {
                        target = best.checkbox;
                        matchedLabel = best.label.text;
                        method = 'nearest-checkbox-to-toggle-label';
                    }
                }

                // Fallback: if an exact label wraps a checkbox, use the wrapped checkbox.
                if (!target) {
                    for (const lab of labels) {
                        const wrapped = lab.el.querySelector && lab.el.querySelector('input[type="checkbox"]');
                        if (wrapped && visible(wrapped)) {
                            target = wrapped;
                            matchedLabel = lab.text;
                            method = 'wrapped-checkbox';
                            break;
                        }
                    }
                }

                // Last fallback: click the exact label/control, but only when it has no Filters text.
                if (!target && labels.length) {
                    const lab = labels.find(o => o.text === wantedToggle);
                    if (lab) {
                        target = lab.el;
                        matchedLabel = lab.text;
                        method = 'exact-toggle-label-control';
                    }
                }

                if (!target) {
                    return {ok:false, reason:'toggle-checkbox-not-found', attributeName, toggleName, labels:labels.map(l => l.text), checkboxCount:checkboxes.length};
                }

                const before = checkedState(target);
                if (before !== !!desired) {
                    if (target.matches && target.matches('input[type="checkbox"]')) {
                        clickOnce(target);
                        if (target.checked !== !!desired) setNativeChecked(target, !!desired);
                    } else {
                        clickOnce(target);
                    }
                }
                const after = checkedState(target);

                return {
                    ok: after === !!desired,
                    attributeName,
                    toggleName,
                    desired: !!desired,
                    beforeChecked: before,
                    afterChecked: after,
                    clicked: before !== !!desired,
                    method,
                    matchedLabel
                };
            }""",
            {"attributeName": attribute_name, "toggleName": toggle_name, "desired": bool(desired)},
        )
        log(f"  {toggle_name} checkbox for '{attribute_name}' result: {res}")
        page.wait_for_timeout(350)
        return bool(res and res.get("ok"))
    except Exception as e:
        log(f"  Could not set {toggle_name} for '{attribute_name}': {e}")
        return False


def set_variants_checkbox_for_attribute(page, name_box) -> bool:
    """Legacy coordinate method disabled.

    The old coordinate-based function could click the right Variants control and then
    misread/click again on some Prockured layouts. New code sets Variants by exact
    attribute name through set_attribute_toggle_by_name().
    """
    log("  Skipping legacy coordinate-based Variants click; using attribute-name verification instead.")
    return True


def ensure_variant_attributes_checked(page, data: ProductData):
    """Final safety pass for all attribute toggle classifications."""
    if not data:
        return

    toggle_plan = []
    for name, _value in data.filter_attributes:
        toggle_plan.append((name, True, False))
    for name, _value in data.variant_attributes:
        toggle_plan.append((name, False, True))
    for name, _value in data.variant_filter_attributes:
        toggle_plan.append((name, True, True))

    if not toggle_plan:
        return

    merged = {}
    for name, wants_filter, wants_variant in toggle_plan:
        key = norm_key(name)
        current = merged.setdefault(key, {"name": name, "filter": False, "variant": False})
        current["filter"] = current["filter"] or wants_filter
        current["variant"] = current["variant"] or wants_variant

    log("Verifying Filters/Variants checkboxes for classified attributes...")
    for item in merged.values():
        if stop_requested:
            log("Stopped while verifying attribute toggles.")
            return
        if item["filter"]:
            set_attribute_toggle_by_name(page, item["name"], "Filters", True)
        if item["variant"]:
            set_attribute_toggle_by_name(page, item["name"], "Variants", True)
    log("Attribute checkbox verification done.")

def fill_value_tags(value_input, value: str, split_values: bool, press_enter: bool = True):
    """Fill one or many tag values in Prockured attribute value input."""
    values = split_variant_values(value) if split_values else [value]
    for v in values:
        value_input.click()
        value_input.fill("")
        value_input.fill(v)
        time.sleep(0.15)
        if press_enter:
            value_input.press("Enter")
            time.sleep(0.25)


def fill_one_attribute(
    page,
    name: str,
    value: str,
    press_enter: bool = PRESS_ENTER_FOR_ATTRIBUTE_VALUE,
    is_variant: bool = False,
    is_filter: bool = False,
    split_values: bool = False,
) -> bool:
    name_input, name_box = find_empty_name_input(page)

    if name_input is None:
        click_add_attribute(page)
        name_input, name_box = find_empty_name_input(page)

    if name_input is None:
        log("  Could not find empty Attribute Name field.")
        return False

    flags = []
    if is_filter:
        flags.append("FILTER")
    if is_variant:
        flags.append("VARIANT")
    suffix = f"  [{' + '.join(flags)}]" if flags else ""
    log(f"Attribute → {name} : {value}{suffix}")

    name_input.scroll_into_view_if_needed()
    page.wait_for_timeout(120)

    try:
        fresh_box = name_input.bounding_box(timeout=1200)
        if fresh_box:
            name_box = fresh_box
    except Exception:
        pass

    name_input.click()
    page.wait_for_timeout(120)
    name_input.fill("")
    name_input.fill(name)
    page.wait_for_timeout(350)
    dismiss_name_dropdown(page)

    try:
        fresh_box = name_input.bounding_box(timeout=1200)
        if fresh_box:
            name_box = fresh_box
    except Exception:
        pass

    value_input, _value_box = find_value_input_for_name(page, name_box)
    if value_input is None:
        log("  Could not find value field for this attribute.")
        log("  Tip: keep the Attributes tab visible and do not touch the mouse while it is running.")
        return False

    value_input.scroll_into_view_if_needed()
    value_input.click()
    page.wait_for_timeout(120)
    value_input.fill("")
    fill_value_tags(value_input, value, split_values=split_values, press_enter=press_enter)

    if is_filter:
        set_attribute_toggle_by_name(page, name, "Filters", True)
    if is_variant:
        set_attribute_toggle_by_name(page, name, "Variants", True)
    return True

def remove_empty_attribute_blocks(page, max_remove: int = 20):
    """Remove empty attribute cards left by old runs or UI shifts.

    Only deletes a card when the attribute Name input is empty AND no value chips/tags
    are present. It avoids touching filled attributes.
    """
    log("Cleaning leftover empty Attribute blocks...")
    removed = 0
    js = r"""
    () => {
        const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const st = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
        };
        const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();

        const nameInputs = Array.from(document.querySelectorAll('input'))
            .filter(visible)
            .filter(el => {
                const ph = norm(el.getAttribute('placeholder'));
                return ph.includes('name') || ph.includes('material');
            })
            .filter(el => !(el.value || '').trim());

        for (const input of nameInputs) {
            let node = input.parentElement;
            for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
                const r = node.getBoundingClientRect();
                if (r.width < 250 || r.height < 100 || r.height > 900) continue;

                const valueInputs = Array.from(node.querySelectorAll('input'))
                    .filter(visible)
                    .filter(el => {
                        const ph = norm(el.getAttribute('placeholder'));
                        return ph.includes('add value') || ph.includes('press enter');
                    });
                if (!valueInputs.length) continue;

                const hasTypedValue = valueInputs.some(el => (el.value || '').trim());
                const text = norm(node.innerText || node.textContent || '');
                // If the card has only boilerplate UI text and no chips, it is safe to remove.
                const chipLike = Array.from(node.querySelectorAll('span, div, button'))
                    .filter(visible)
                    .map(el => norm(el.innerText || el.textContent || ''))
                    .filter(t => t && !['filters','variants','add value and press enter (e.g. red)','name (e.g. material)'].includes(t))
                    .filter(t => !t.includes('add value and press enter') && !t.includes('name (e.g. material)') && !t.includes('filters') && !t.includes('variants'));

                const hasRealChip = chipLike.some(t => t.length > 0 && t.length < 80);
                if (hasTypedValue || hasRealChip) continue;

                const buttons = Array.from(node.querySelectorAll('button')).filter(visible);
                const deleteButtons = buttons.filter(btn => {
                    const t = norm(btn.innerText || btn.textContent);
                    const aria = norm(btn.getAttribute('aria-label'));
                    const title = norm(btn.getAttribute('title'));
                    const html = norm(btn.innerHTML || '');
                    const br = btn.getBoundingClientRect();
                    const hasSvg = !!btn.querySelector('svg');
                    return aria.includes('delete') || title.includes('delete') || t.includes('delete') || html.includes('trash') || (hasSvg && br.width <= 90 && br.height <= 90);
                });
                if (deleteButtons.length) {
                    deleteButtons[deleteButtons.length - 1].click();
                    return true;
                }
            }
        }
        return false;
    }
    """
    for _ in range(max_remove):
        try:
            clicked = bool(page.evaluate(js))
        except Exception:
            clicked = False
        if not clicked:
            break
        removed += 1
        page.wait_for_timeout(200)
    log(f"Removed leftover empty Attribute blocks: {removed}")

def fill_attributes(page, data: ProductData, one=False, clear=True):
    """Fill the four JSON attribute classes and merge duplicate names safely.

    attributes                    -> no checkbox
    filter_attributes             -> Filters
    variant_attributes            -> Variants
    variant_filter_attributes     -> Filters + Variants
    """
    merged = {}

    def add_pairs(pairs, wants_filter=False, wants_variant=False, split_values=False):
        for name, raw_value in pairs:
            key = norm_key(name)
            if not key:
                continue
            item = merged.setdefault(key, {
                "name": name,
                "values": [],
                "is_filter": False,
                "is_variant": False,
                "split_values": False,
            })
            item["is_filter"] = item["is_filter"] or wants_filter
            item["is_variant"] = item["is_variant"] or wants_variant
            item["split_values"] = item["split_values"] or split_values
            incoming = split_variant_values(raw_value) if split_values else [str(raw_value).strip()]
            seen = {norm_key(v) for v in item["values"]}
            for value in incoming:
                if value and norm_key(value) not in seen:
                    item["values"].append(value)
                    seen.add(norm_key(value))

    add_pairs(data.attributes, False, False, False)
    add_pairs(data.filter_attributes, True, False, True)
    add_pairs(data.variant_attributes, False, True, True)
    add_pairs(data.variant_filter_attributes, True, True, True)

    combined = []
    for item in merged.values():
        item["value"] = ", ".join(item["values"])
        combined.append(item)

    if not combined:
        log("No attributes loaded. Skipping Attributes.")
        return True

    click_tab(page, "Attributes")
    attrs = combined[:1] if one else combined
    log(
        "Attributes to fill: "
        f"normal={len(data.attributes)} filter={len(data.filter_attributes)} "
        f"variant={len(data.variant_attributes)} "
        f"variant+filter={len(data.variant_filter_attributes)} "
        f"merged_total={len(attrs)}"
    )

    if clear and not one:
        clear_existing_attributes(page)
        page.wait_for_timeout(500)

    for index, item in enumerate(attrs, start=1):
        if stop_requested:
            log("Stopped current automation during Attributes.")
            return False

        ok = fill_one_attribute(
            page,
            item["name"],
            item["value"],
            press_enter=PRESS_ENTER_FOR_ATTRIBUTE_VALUE,
            is_variant=item["is_variant"],
            is_filter=item["is_filter"],
            split_values=item["split_values"],
        )
        if not ok:
            log(
                f"Attribute fill failed at {index}/{len(attrs)}: {item['name']}. "
                "The flow will not move to Variations."
            )
            return False
        page.wait_for_timeout(300)

    remove_empty_attribute_blocks(page)
    ensure_variant_attributes_checked(page, data)
    log("Attributes fill done. All checkbox classifications were applied.")
    return True

def click_generate_variants(page):
    """Open Variations tab and click Generate Variants if available."""
    if not click_tab(page, "Variations"):
        click_tab(page, "Catalog")
    page.wait_for_timeout(900)
    log("Trying to click Generate Variants...")
    clicked = False
    try:
        page.get_by_role("button", name=re.compile(r"generate\s+variants", re.I)).click(timeout=3500)
        clicked = True
    except Exception:
        try:
            clicked = bool(page.evaluate(
                r"""() => {
                    function visible(el) {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                    }
                    const els = [...document.querySelectorAll('button, div, span')].filter(visible);
                    const el = els.find(e => /generate\s+variants/i.test((e.innerText||e.textContent||'').trim()));
                    if (el) { el.click(); return true; }
                    return false;
                }"""
            ))
        except Exception:
            clicked = False
    if clicked:
        log("Generate Variants clicked. Waiting for combinations...")
        page.wait_for_timeout(3500)
    else:
        log("Generate Variants button not found/clicked. Continuing to existing variant rows.")
    return clicked


def build_variant_price_entries(data: ProductData):
    product_name = (data.basics or {}).get("Product Name", "")
    entries = []
    for row in data.variant_pricing:
        price = row.get("price")
        if price is None:
            continue
        attrs = row.get("attributes") or {}
        key = " | ".join([f"{k}: {v}" for k, v in attrs.items()])
        base_price, sale_price, markup = compute_base_and_discount_prices(product_name + " " + key, price)
        entries.append({
            "attributes": attrs,
            "sale_price": sale_price,
            "base_price": base_price,
            "markup": round(markup, 2),
            "price_raw": row.get("price_raw", ""),
            "key": key,
        })
    return entries


def fill_variant_prices(page, data: ProductData):
    if not is_variable_product(data):
        log("Not a variable product. Skipping variant prices.")
        return
    entries = build_variant_price_entries(data)
    if not entries:
        log("No [VARIANT PRICING] rows found. Variants may be generated but prices will not be filled.")
        return
    if not click_tab(page, "Variations"):
        click_tab(page, "Catalog")
    page.wait_for_timeout(1000)
    log(f"Filling variant prices. Pricing rows loaded: {len(entries)}")
    res = page.evaluate(
        r"""({entries}) => {
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden' && s.opacity !== '0';
            }
            function norm(s) { return (s || '').toLowerCase().replace(/[^a-z0-9]+/g,' ').replace(/\s+/g,' ').trim(); }
            function exactText(el) { return (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim().toLowerCase(); }
            function setValue(el, val) {
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.focus();
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                setter.call(el, String(val));
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }
            function clickElement(el) {
                if (!el) return false;
                el.scrollIntoView({block:'center', inline:'nearest'});
                try { el.click(); return true; } catch(e) {}
                const r = el.getBoundingClientRect();
                const x = r.left + r.width / 2;
                const y = r.top + r.height / 2;
                for (const type of ['pointerdown','mousedown','mouseup','click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y, view:window}));
                }
                return true;
            }
            function findCards() {
                const all = [...document.querySelectorAll('div, section, article, li')].filter(visible);
                const starts = all.filter(el => /combination\s*#/i.test(el.innerText || el.textContent || ''));
                const cards = [];
                const seen = new Set();
                for (const st of starts) {
                    let node = st;
                    for (let depth=0; node && depth<8; depth++, node=node.parentElement) {
                        const inputs = [...node.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio])')].filter(visible);
                        const text = node.innerText || node.textContent || '';
                        const r = node.getBoundingClientRect();
                        if (inputs.length >= 3 && /active/i.test(text) && r.height < 650 && r.width > 400) {
                            if (!seen.has(node)) { seen.add(node); cards.push(node); }
                            break;
                        }
                    }
                }
                // de-dupe nested cards by keeping smaller containers first
                return cards.sort((a,b) => {
                    const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
                    return (ra.height*ra.width) - (rb.height*rb.width);
                });
            }
            function entryMatchesText(entry, textNorm) {
                const vals = Object.values(entry.attributes || {});
                if (!vals.length) return false;
                return vals.every(v => textNorm.includes(norm(v)));
            }
            function findActiveCheckbox(card) {
                const checks = [...card.querySelectorAll('input[type="checkbox"]')].filter(visible);
                const controls = [...card.querySelectorAll('label, span, div, button, p, strong')]
                    .filter(visible)
                    .map(el => ({el, text: exactText(el), r: el.getBoundingClientRect()}))
                    .filter(o => o.text === 'active' || o.text.includes('active'));

                // Best path: real checkbox in the same row as the visible Active label.
                const candidates = checks.map(cb => {
                    const r = cb.getBoundingClientRect();
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;
                    const parentText = exactText(cb.closest('label, button, div') || cb.parentElement || cb);
                    const nearbyActive = controls.filter(o => {
                        const oy = o.r.top + o.r.height / 2;
                        const ox = o.r.left + o.r.width / 2;
                        return Math.abs(oy - cy) <= 40 && ox >= cx - 80 && ox <= cx + 260;
                    });
                    const score = (parentText.includes('active') || nearbyActive.length ? 0 : 10000)
                        + Math.min(...(nearbyActive.map(o => Math.abs((o.r.top + o.r.height / 2) - cy)).concat([500])))
                        + r.top / 1000;
                    return {cb, r, parentText, nearbyText: nearbyActive.map(o => o.text).join(' | '), score};
                }).filter(o => o.parentText.includes('active') || o.nearbyText.includes('active'))
                  .sort((a,b) => a.score - b.score);

                if (candidates.length) return candidates[0].cb;

                // Fallback: visible text "Active" may be wrapped in a clickable label containing the checkbox.
                for (const c of controls) {
                    const label = c.el.closest('label');
                    if (label) {
                        const cb = label.querySelector('input[type="checkbox"]');
                        if (cb && visible(cb)) return cb;
                    }
                }
                return null;
            }
            function setCardActive(card, shouldBeActive) {
                const cb = findActiveCheckbox(card);
                if (cb) {
                    const before = !!cb.checked;
                    if (before !== shouldBeActive) {
                        clickElement(cb);
                    }
                    const after = !!cb.checked;
                    return {ok:true, method:'checkbox', before, after, changed: before !== after};
                }

                // Custom-control fallback only when it exposes a state. This avoids blind double-toggles.
                const activeControls = [...card.querySelectorAll('[role="checkbox"], button, label, div')]
                    .filter(visible)
                    .map(el => ({el, text: exactText(el), aria: (el.getAttribute('aria-checked') || el.getAttribute('aria-pressed') || '').toLowerCase()}))
                    .filter(o => (o.text === 'active' || o.text.includes('active')) && (o.aria === 'true' || o.aria === 'false'));
                if (activeControls.length) {
                    const target = activeControls[0];
                    const before = target.aria === 'true';
                    if (before !== shouldBeActive) clickElement(target.el);
                    const afterRaw = (target.el.getAttribute('aria-checked') || target.el.getAttribute('aria-pressed') || '').toLowerCase();
                    const after = afterRaw === 'true';
                    return {ok:true, method:'aria-control', before, after, changed: before !== after};
                }
                return {ok:false, reason:'active-checkbox-not-found'};
            }

            const cards = findCards();
            const usedEntries = new Set();
            const results = [];
            for (let i=0; i<cards.length; i++) {
                const card = cards[i];
                const text = card.innerText || card.textContent || '';
                const textNorm = norm(text);
                let matched = null;
                let matchedIndex = -1;
                for (let j=0; j<entries.length; j++) {
                    if (usedEntries.has(j)) continue;
                    if (entryMatchesText(entries[j], textNorm)) { matched = entries[j]; matchedIndex = j; break; }
                }
                const inputs = [...card.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio])')]
                    .filter(visible)
                    .filter(el => !el.disabled && !el.readOnly)
                    .map(el => ({el, r:el.getBoundingClientRect(), ph:el.placeholder || '', name:el.getAttribute('aria-label') || ''}))
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (matched && inputs.length >= 3) {
                    // Prockured variant order: SKU, Regular/Base, Sale/Discount.
                    // IMPORTANT: never touch the Active checkbox. Preserve the existing state.
                    setValue(inputs[1].el, matched.base_price);
                    setValue(inputs[2].el, matched.sale_price);
                    usedEntries.add(matchedIndex);
                    results.push({card:i+1, status:'priced-active-untouched', key:matched.key, base:matched.base_price, sale:matched.sale_price});
                } else if (matched && inputs.length < 3) {
                    // Do not change Active even when the price inputs cannot be located.
                    results.push({card:i+1, status:'matched-but-inputs-missing-active-untouched', key:matched.key, inputs:inputs.length});
                } else {
                    // Leave unmatched combinations exactly as they are. In particular,
                    // do not mark their Active checkbox as inactive.
                    results.push({card:i+1, status:'skipped-no-price-match-active-untouched', preview:text.slice(0,120)});
                }
            }
            return {cards:cards.length, priced:results.filter(r => r.status==='priced-active-untouched').length, results};
        }""",
        {"entries": entries},
    )
    log(f"Variant fill result: cards={res.get('cards')} priced={res.get('priced')}")
    for r in res.get("results", []):
        log(f"  {r}")
    log("Variant price fill done.")

def fill_variations(page, data: ProductData):
    if not is_variable_product(data):
        log("Not a variable product. Skipping Variations.")
        return
    click_generate_variants(page)
    fill_variant_prices(page, data)

# ----------------------------
# Media + Pricing integration
# ----------------------------

def normalize_match_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def simple_match_score(a: str, b: str) -> int:
    """Token-aware fuzzy score without requiring rapidfuzz."""
    from difflib import SequenceMatcher
    na = normalize_match_text(a)
    nb = normalize_match_text(b)
    if not na or not nb:
        return 0
    ratio = SequenceMatcher(None, na, nb).ratio() * 100
    ta = set(na.split())
    tb = set(nb.split())
    overlap = (len(ta & tb) / max(1, len(ta))) * 100
    return int(round((ratio * 0.55) + (overlap * 0.45)))


def read_current_product_name_from_page(page) -> str:
    """Read product name from the current product edit page without changing other fields."""
    try:
        click_tab(page, "Basics")
        res = page.evaluate(
            r"""() => {
                function isVisible(el) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                }
                function normalize(s) { return (s || '').replace(/\*/g,'').replace(/\s+/g,' ').trim().toUpperCase(); }
                const labels = [...document.querySelectorAll('label, div, span, p, strong')]
                    .filter(isVisible)
                    .map(el => ({el, text: normalize(el.innerText || el.textContent), r: el.getBoundingClientRect()}))
                    .filter(o => o.text === 'PRODUCT NAME')
                    .sort((a,b) => a.r.top - b.r.top);
                if (!labels.length) return '';
                const lab = labels[0];
                const inputs = [...document.querySelectorAll('input:not([type=hidden])')]
                    .filter(isVisible)
                    .map(el => ({el, r: el.getBoundingClientRect()}))
                    .filter(o => o.r.top >= lab.r.bottom - 10 && o.r.top < lab.r.bottom + 180)
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                return inputs.length ? (inputs[0].el.value || '') : '';
            }"""
        )
        return (res or "").strip()
    except Exception:
        return ""


def get_product_name_for_matching(page, data: ProductData) -> str:
    # Old logic preferred Product Name from loaded data, otherwise read it from the page.
    # Batch mode does NOT update Product Name, but it stores the JSON name under
    # __Batch Product Name so Media/Pricing can still use the old matching logic
    # without having to jump back to Basic just to read the name.
    name = ""
    if data and data.basics:
        name = (data.basics.get("Product Name") or data.basics.get("__Batch Product Name") or "")
    name = str(name or "").strip()
    if name:
        return name
    return read_current_product_name_from_page(page)


def parse_price_value(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.upper() in {"N/A", "NA", "NONE"}:
        return None
    # If a cell contains multiple prices, use the first numeric-looking one.
    s = s.replace("₹", " ").replace("Rs.", " ").replace("Rs", " ").replace(",", " ")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def stable_markup_percent(product_name: str) -> float:
    """Stable pseudo-random 10–20% markup. Same product => same markup every run."""
    key = normalize_match_text(product_name) or "product"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    n = int(digest[:8], 16) / 0xFFFFFFFF
    return BASE_MARKUP_MIN + (BASE_MARKUP_MAX - BASE_MARKUP_MIN) * n


def compute_base_and_discount_prices(product_name: str, sale_price: float):
    sale = int(math.ceil(float(sale_price)))
    markup = stable_markup_percent(product_name)
    base = int(math.ceil(sale * (1 + markup / 100)))
    if base <= sale:
        base = sale + 1
    return base, sale, markup


def find_price_from_loaded_data(data: ProductData) -> float | None:
    if not data:
        return None
    p = data.pricing or {}
    # Actual/sale/discount price should be used as sale price.
    for key in ["Sale Price", "Actual Price", "Discount Price", "Price", "MRP"]:
        if p.get(key):
            val = parse_price_value(p.get(key))
            if val is not None:
                return val
    return None


def csv_candidate_title(row: dict) -> str:
    for k in ["input_title", "Product Title", "product title", "title", "Title", "Product Name", "product_name", "Matched Title", "matched_title"]:
        if row.get(k):
            return str(row.get(k)).strip()
    return ""


def csv_candidate_price(row: dict) -> float | None:
    for k in ["Price", "price", "Sale Price", "sale_price", "Discount Price", "discount_price", "actual_price", "Actual Price"]:
        if row.get(k):
            val = parse_price_value(row.get(k))
            if val is not None:
                return val
    return None


def scan_csv_rows(output_dir: Path):
    csv_paths = []
    # Important reports created by your scraper.
    for name in ["hyperpure_prices.csv", "summary.csv", "all_images.csv"]:
        p = output_dir / name
        if p.exists():
            csv_paths.append(p)
    # Fallback: all CSVs in output folder.
    if output_dir.exists():
        for p in output_dir.rglob("*.csv"):
            if p not in csv_paths:
                csv_paths.append(p)

    rows = []
    for path in csv_paths:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    r = dict(row)
                    r["__csv_path"] = str(path)
                    rows.append(r)
        except Exception as e:
            log(f"Could not read CSV {path}: {e}")
    return rows


def find_best_price_in_csv(product_name: str, output_dir: Path = DEFAULT_OUTPUT_DIR):
    rows = scan_csv_rows(output_dir)
    best = None
    for row in rows:
        title = csv_candidate_title(row)
        price = csv_candidate_price(row)
        if not title or price is None:
            continue
        score = simple_match_score(product_name, title)
        if best is None or score > best["score"]:
            best = {"score": score, "title": title, "price": price, "row": row}
    return best


def collect_image_paths_from_csv(product_name: str, output_dir: Path = DEFAULT_OUTPUT_DIR):
    rows = scan_csv_rows(output_dir)
    best_title = None
    best_score = -1
    grouped = {}
    for row in rows:
        path = row.get("local_path") or row.get("Local Path") or row.get("image_path") or row.get("Image Path")
        if not path:
            continue
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.exists() or p.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        title = csv_candidate_title(row)
        if not title:
            continue
        score = simple_match_score(product_name, title)
        grouped.setdefault(title, []).append(p)
        if score > best_score:
            best_score = score
            best_title = title
    if best_title and best_score >= 50:
        paths = grouped.get(best_title, [])
        # De-dupe while preserving order.
        seen = set()
        out = []
        for p in paths:
            sp = str(p.resolve()).lower()
            if sp not in seen:
                seen.add(sp)
                out.append(p)
        return {"score": best_score, "matched_title": best_title, "paths": out}
    return None


def collect_image_paths_from_folders(product_name: str, image_root: Path = DEFAULT_IMAGE_ROOT):
    if not image_root.exists():
        return None
    folders = [p for p in image_root.rglob("*") if p.is_dir()]
    best = None
    for folder in folders:
        # Match against folder name and parent/name combination.
        match_text = f"{folder.parent.name} {folder.name}"
        score = simple_match_score(product_name, match_text)
        images = []
        try:
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                    images.append(p)
        except Exception:
            continue
        if not images:
            continue
        if best is None or score > best["score"]:
            # Sort by filename so h-01/a-01 etc stay predictable.
            images = sorted(images, key=lambda x: x.name.lower())
            best = {"score": score, "matched_folder": str(folder), "paths": images}
    if best and best["score"] >= 45:
        return best
    return best  # return even if low so terminal can show why it failed


def get_model_name_from_product_data(data: ProductData) -> str:
    if not data or not data.basics:
        return ""
    return str(data.basics.get("Model Name") or data.basics.get("__Batch Model Name") or "").strip()

MEDIA_SORT_ALIASES = {
    "brand": "brand",
    "model": "modelnumber",
    "modelname": "modelnumber",
    "model_name": "modelnumber",
    "modelnumber": "modelnumber",
    "model_number": "modelnumber",
    "sku": "sku",
    "name": "name",
    "title": "name",
    "productname": "name",
    "product_name": "name",
}


def parse_media_sort(value: str) -> list[str]:
    raw = re.split(r"[\/|,>]+", str(value or "modelnumber"))
    out = []
    for part in raw:
        key = re.sub(r"[^a-z0-9_]+", "", part.strip().lower())
        canonical = MEDIA_SORT_ALIASES.get(key)
        if canonical and canonical not in out:
            out.append(canonical)
    return out or ["modelnumber"]


def media_property_value(data: ProductData, prop: str) -> str:
    media = data.media or {}
    brand = str(media.get("__Brand") or data.basics.get("__Batch Brand") or "").strip()
    product_name = str(media.get("__Product Name") or data.basics.get("Product Name") or "").strip()
    if prop == "brand":
        return brand
    if prop == "modelnumber":
        return str(media.get("__Model Number") or get_model_name_from_product_data(data)).strip()
    if prop == "sku":
        return str(media.get("__SKU") or data.basics.get("__Batch SKU") or "").strip()
    if prop == "name":
        if brand and product_name and not normalize_match_text(product_name).startswith(normalize_match_text(brand)):
            return f"{brand} {product_name}".strip()
        return product_name or brand
    return ""


def _folder_images(folder: Path) -> list[Path]:
    try:
        return sorted(
            [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and p.stat().st_size > 0],
            key=lambda p: p.name.lower(),
        )
    except Exception:
        return []


def _find_case_insensitive_child(parent: Path, wanted: str) -> Path | None:
    wanted_norm = normalize_model_name(wanted)
    if not wanted_norm or not parent.exists():
        return None
    try:
        for child in parent.iterdir():
            if child.is_dir() and normalize_model_name(child.name) == wanted_norm:
                return child
    except Exception:
        return None
    return None


def _find_exact_media_path(root: Path, values: list[str]) -> Path | None:
    current = root
    for value in values:
        child = current / value
        if child.is_dir():
            current = child
            continue
        child = _find_case_insensitive_child(current, value)
        if child is None:
            return None
        current = child
    return current if _folder_images(current) else None


def _score_media_folder(folder: Path, root: Path, values: list[str]) -> int:
    try:
        parts = list(folder.relative_to(root).parts)
    except Exception:
        parts = [folder.name]
    if not parts:
        parts = [folder.name]

    tail = parts[-len(values):] if len(parts) >= len(values) else parts
    component_scores = []
    aligned_values = values[-len(tail):]
    for expected, actual in zip(aligned_values, tail):
        component_scores.append(simple_match_score(expected, actual))

    if not component_scores:
        component_score = 0
    elif len(component_scores) == 1:
        component_score = component_scores[0]
    else:
        last_weight = 0.70
        earlier_weight = 0.30 / (len(component_scores) - 1)
        component_score = sum(
            score * (last_weight if i == len(component_scores) - 1 else earlier_weight)
            for i, score in enumerate(component_scores)
        )

    full_expected = " ".join(values)
    full_actual = " ".join(parts)
    full_score = simple_match_score(full_expected, full_actual)

    images = _folder_images(folder)
    filename_score = 0
    if images and values:
        filename_score = max(simple_match_score(values[-1], p.stem) for p in images[:25])

    score = round(component_score * 0.75 + full_score * 0.20 + filename_score * 0.05)
    expected_norms = [normalize_model_name(v) for v in values]
    tail_norms = [normalize_model_name(v) for v in tail]
    if len(tail_norms) == len(expected_norms) and tail_norms == expected_norms:
        return 100
    if expected_norms and expected_norms[-1] in [normalize_model_name(p) for p in parts]:
        score = max(score, 95)
    return max(0, min(100, int(score)))

def find_best_images(product_name: str, data: ProductData):
    media = data.media or {}

    folder_raw = media.get("Image Folder", "") or media.get("Folder", "") or ""
    if folder_raw:
        folder = Path(folder_raw)
        paths = _folder_images(folder) if folder.is_dir() else []
        if paths:
            return {"source": "explicit-folder", "score": 100, "matched": str(folder), "paths": paths}

    root = Path(media.get("__Media Root") or DEFAULT_IMAGE_ROOT)
    strategy = parse_media_sort(media.get("__Media Sort") or "modelnumber")
    try:
        min_score = int(float(media.get("__Media Min Score") or 85))
    except Exception:
        min_score = 85

    values = [media_property_value(data, prop) for prop in strategy]
    if any(not value for value in values):
        missing_props = [prop for prop, value in zip(strategy, values) if not value]
        log(f"Media matching cannot use {strategy}; missing values for: {', '.join(missing_props)}")
        return None
    if not root.exists():
        log(f"Media root does not exist: {root}")
        return None

    exact = _find_exact_media_path(root, values)
    if exact:
        return {
            "source": f"exact-{ '/'.join(strategy) }",
            "score": 100,
            "matched": str(exact),
            "paths": _folder_images(exact),
            "strategy": "/".join(strategy),
            "root": str(root),
        }

    candidates = []
    if _folder_images(root):
        candidates.append(root)
    try:
        for folder in root.rglob("*"):
            if folder.is_dir() and _folder_images(folder):
                candidates.append(folder)
    except Exception as e:
        log(f"Could not scan media root {root}: {e}")

    scored = []
    for folder in candidates:
        score = _score_media_folder(folder, root, values)
        scored.append({"folder": folder, "score": score, "paths": _folder_images(folder)})
    scored.sort(key=lambda x: (-x["score"], str(x["folder"]).lower()))

    if not scored:
        log(f"No image folders found under media root: {root}")
        return None

    best = scored[0]
    second = scored[1] if len(scored) > 1 else None
    log(
        f"Media score result: strategy={'/'.join(strategy)} target={values} "
        f"best={best['folder']} score={best['score']} min={min_score}"
    )
    if second:
        log(f"Second media candidate: {second['folder']} score={second['score']}")

    if best["score"] < min_score:
        log("Media match rejected because the score is below the minimum threshold.")
        return None
    if second and best["score"] - second["score"] <= 3 and best["folder"] != second["folder"]:
        log("Media match rejected as ambiguous: top two candidates are within 3 points.")
        return None

    return {
        "source": "scored-folder",
        "score": best["score"],
        "matched": str(best["folder"]),
        "paths": best["paths"],
        "strategy": "/".join(strategy),
        "root": str(root),
    }

def delete_existing_media_if_requested(page, data: ProductData):
    rep = ""
    if data and data.media:
        rep = (data.media.get("Replace Existing Images") or data.media.get("Replace Images") or "").strip().lower()
    if rep not in {"yes", "true", "1", "y"}:
        return
    log("Replace Existing Images = Yes. Trying to remove old media...")
    # Best-effort only because UI buttons may differ.
    for _ in range(25):
        clicked = page.evaluate(
            r"""() => {
                function visible(el) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                }
                const buttons = [...document.querySelectorAll('button')].filter(visible);
                const btn = buttons.find(b => {
                    const t = (b.innerText||b.textContent||'').toLowerCase();
                    const a = (b.getAttribute('aria-label')||'').toLowerCase();
                    const h = (b.innerHTML||'').toLowerCase();
                    return t.includes('delete') || t.includes('remove') || a.includes('delete') || a.includes('remove') || h.includes('trash');
                });
                if (btn) { btn.click(); return true; }
                return false;
            }"""
        )
        if not clicked:
            break
        page.wait_for_timeout(350)
        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(250)
        except Exception:
            pass


def _product_media_region(page):
    """Locate the top Product Media section and its Add Image control region.

    The Media tab also contains Product Review Images, Brochures, Add URL and Add
    Link controls. This function uses the visible Product Media heading and the
    next section heading as vertical boundaries so only the top Add Image tile is
    considered.
    """
    try:
        return page.evaluate(
            r"""() => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
                           s.visibility !== 'hidden' && s.opacity !== '0';
                }
                function clean(s) {
                    return (s || '').replace(/\s+/g, ' ').trim();
                }
                const nodes = [...document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p,strong,label')]
                    .filter(visible)
                    .map(el => ({el, text:clean(el.innerText || el.textContent || ''), r:el.getBoundingClientRect()}))
                    .filter(o => o.text && o.text.length <= 120);

                const heading = nodes
                    .filter(o => /^product media$/i.test(o.text))
                    .sort((a,b) => (a.r.width*a.r.height) - (b.r.width*b.r.height))[0];
                if (!heading) return {ok:false, reason:'product-media-heading-not-found'};

                const nextHeadings = nodes
                    .filter(o => o.r.top > heading.r.bottom + 20)
                    .filter(o => /^(product review images|brochures\s*&\s*files|brochures and files)$/i.test(o.text))
                    .sort((a,b) => a.r.top - b.r.top);
                const bottom = nextHeadings.length ? nextHeadings[0].r.top - 8 : heading.r.bottom + 700;

                const addNodes = nodes
                    .filter(o => o.r.top >= heading.r.bottom - 10 && o.r.top < bottom)
                    .filter(o => /^add image$/i.test(o.text))
                    .sort((a,b) => (a.r.width*a.r.height) - (b.r.width*b.r.height));

                let add = addNodes[0] || null;
                let target = add ? add.el : null;
                if (target) {
                    const direct = target.closest('button,label,[role="button"]');
                    if (direct && visible(direct)) {
                        target = direct;
                    } else {
                        let node = target;
                        for (let depth = 0; node && depth < 7; depth++, node = node.parentElement) {
                            const r = node.getBoundingClientRect();
                            const txt = clean(node.innerText || node.textContent || '');
                            const style = window.getComputedStyle(node);
                            const hasFile = !!node.querySelector('input[type="file"]');
                            const clickable = hasFile || style.cursor === 'pointer' || !!node.onclick ||
                                node.getAttribute('role') === 'button';
                            const tileSized = r.width >= 100 && r.width <= 420 && r.height >= 80 && r.height <= 420;
                            if (/add image/i.test(txt) && (clickable || tileSized)) {
                                target = node;
                                if (clickable) break;
                            }
                        }
                    }
                }

                const allInputs = [...document.querySelectorAll('input[type="file"]')];
                const fileInputs = allInputs.map((el, index) => {
                    let node = el.parentElement;
                    let context = '';
                    let rr = el.getBoundingClientRect();
                    for (let depth = 0; node && depth < 9; depth++, node = node.parentElement) {
                        const r = node.getBoundingClientRect();
                        const txt = clean(node.innerText || node.textContent || '');
                        if (r.top >= heading.r.top - 80 && r.top < bottom &&
                            (/add image/i.test(txt) || /product media/i.test(txt))) {
                            context = txt.slice(0, 400);
                            rr = r;
                            break;
                        }
                    }
                    const accept = (el.getAttribute('accept') || '').toLowerCase();
                    let score = 0;
                    if (rr.top >= heading.r.top - 80 && rr.top < bottom) score += 500;
                    if (/add image/i.test(context)) score += 400;
                    if (/product media/i.test(context)) score += 250;
                    if (accept.includes('image')) score += 200;
                    if (el.multiple) score += 100;
                    if (/review images/i.test(context)) score -= 1000;
                    if (/brochures|pdf|document/i.test(context)) score -= 1000;
                    return {index, score, multiple:!!el.multiple, accept, context,
                            top:Math.round(rr.top), left:Math.round(rr.left)};
                }).sort((a,b) => b.score - a.score);

                const tr = target ? target.getBoundingClientRect() : null;
                return {
                    ok: !!target || (fileInputs[0] && fileInputs[0].score > 0),
                    headingTop: Math.round(heading.r.top),
                    bottom: Math.round(bottom),
                    addTarget: target ? {
                        text: clean(target.innerText || target.textContent || ''),
                        tag: target.tagName,
                        role: target.getAttribute('role') || '',
                        x: Math.round(tr.left + tr.width / 2),
                        y: Math.round(tr.top + tr.height / 2),
                        left: Math.round(tr.left), top: Math.round(tr.top),
                        width: Math.round(tr.width), height: Math.round(tr.height)
                    } : null,
                    bestFileInput: fileInputs[0] || null,
                    fileInputs
                };
            }"""
        )
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _product_media_image_count(page) -> int:
    """Count visible image cards only inside the top Product Media section."""
    try:
        return int(page.evaluate(
            r"""() => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                function clean(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
                const nodes = [...document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p,strong')]
                    .filter(visible)
                    .map(el => ({el, text:clean(el.innerText || el.textContent || ''), r:el.getBoundingClientRect()}));
                const heading = nodes.filter(o => /^product media$/i.test(o.text))
                    .sort((a,b) => (a.r.width*a.r.height) - (b.r.width*b.r.height))[0];
                if (!heading) return 0;
                const next = nodes.filter(o => o.r.top > heading.r.bottom + 20)
                    .filter(o => /^(product review images|brochures\s*&\s*files|brochures and files)$/i.test(o.text))
                    .sort((a,b) => a.r.top - b.r.top)[0];
                const bottom = next ? next.r.top - 8 : heading.r.bottom + 700;
                return [...document.querySelectorAll('img')]
                    .filter(visible)
                    .filter(img => {
                        const r = img.getBoundingClientRect();
                        return r.top >= heading.r.bottom - 20 && r.top < bottom && r.width >= 70 && r.height >= 70;
                    }).length;
            }"""
        ))
    except Exception:
        return 0


def _click_product_media_add_image(page, paths) -> bool:
    """Click Product Media -> Add Image and submit all selected images together.

    This mirrors the manual working process: click the top Add Image tile, select
    every image in the model folder, then upload. It never touches Product Review
    Images, Upload, Add URL, Add Link or brochure controls.
    """
    region = _product_media_region(page)
    log(f"Product Media region inspection: {region}")
    target = (region or {}).get("addTarget")

    if target:
        try:
            with page.expect_file_chooser(timeout=7000) as chooser_info:
                page.mouse.click(float(target["x"]), float(target["y"]))
            chooser = chooser_info.value
            chooser.set_files(paths)
            log(f"Selected all {len(paths)} image(s) through Product Media -> Add Image.")
            return True
        except Exception as e:
            log(f"Product Media Add Image chooser click failed: {e}")

    # Exact fallback: use only the file input scored inside Product Media.
    best_input = (region or {}).get("bestFileInput")
    if best_input and int(best_input.get("score", 0)) > 0:
        try:
            idx = int(best_input["index"])
            page.locator("input[type='file']").nth(idx).set_input_files(paths)
            log(f"Selected all {len(paths)} image(s) on Product Media file input #{idx}.")
            return True
        except Exception as e:
            log(f"Product Media file-input fallback failed: {e}")

    return False


def upload_images_to_media(page, image_paths):
    paths = [str(Path(p).resolve()) for p in image_paths if Path(p).exists()]
    if not paths:
        log("No existing local image files to upload.")
        return False

    before_count = _product_media_image_count(page)
    log(f"Product Media images before upload: {before_count}")

    submitted = _click_product_media_add_image(page, paths)
    if not submitted:
        log("Could not submit images through Product Media -> Add Image.")
        return False

    # Let the site create/upload the Product Media cards before leaving Media.
    page.wait_for_timeout(5000)
    after_count = _product_media_image_count(page)
    log(f"Product Media images after upload wait: {after_count}")
    log(f"Product Media submission completed for {len(paths)} image(s).")
    return True


def fill_media(page, data: ProductData):
    product_name = get_product_name_for_matching(page, data)
    media = data.media or {}
    media_root = media.get("__Media Root") or str(DEFAULT_IMAGE_ROOT)
    media_sort = media.get("__Media Sort") or "modelnumber"
    min_score = media.get("__Media Min Score") or "85"

    log(f"Media root: {media_root}")
    log(f"MediaSort: {media_sort} | minimum score: {min_score}")

    click_tab(page, "Media")
    page.wait_for_timeout(2000)

    found = find_best_images(product_name, data)
    if not found or not found.get("paths"):
        log("No safe media match found. Product Media was not changed.")
        return False

    selected = found["paths"][:MAX_MEDIA_UPLOADS]
    log(
        f"Image match source: {found['source']} | score: {found['score']} | "
        f"matched: {found['matched']}"
    )
    log(f"Product Media images selected together: {len(selected)}")
    for p in selected:
        log(f"  {p}")

    delete_existing_media_if_requested(page, data)
    uploaded = upload_images_to_media(page, selected)
    if uploaded:
        log("Media fill done through Product Media -> Add Image only.")
    else:
        log("Media fill ended without a confirmed Product Media submission.")
    return uploaded

def update_media_alt_texts(page, data: ProductData):
    """Update visible media image Alt text boxes.

    Locate method:
    - Open Media tab.
    - Find visible image cards in the Product Media area.
    - Hover each card so the overlay/Alt text input appears.
    - Find the Alt text textbox inside that same card.
    - Clear existing text and set: '<Product Name> Prockured Image N'.
    """
    product_name = get_product_name_for_matching(page, data)
    if not product_name:
        log("Could not determine product name for image alt text.")
        return

    click_tab(page, "Media")
    page.wait_for_timeout(500)

    # Latest requested format: product name first, Prockured as suffix, plus image number for uniqueness.
    def alt_text_for(i: int) -> str:
        return f"{product_name} Prockured Image {i}"

    cards = page.evaluate(
        r"""() => {
            function visible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            }
            const imgs = [...document.querySelectorAll('img')]
                .filter(visible)
                .map(img => {
                    const r = img.getBoundingClientRect();
                    let card = img;
                    for (let d = 0; d < 8 && card.parentElement; d++) {
                        const pr = card.parentElement.getBoundingClientRect();
                        if (pr.width >= r.width && pr.height >= r.height && pr.width < 520 && pr.height < 520) {
                            card = card.parentElement;
                        } else {
                            break;
                        }
                    }
                    const cr = card.getBoundingClientRect();
                    return {
                        x: Math.round(cr.left + cr.width / 2),
                        y: Math.round(cr.top + cr.height / 2),
                        top: Math.round(cr.top),
                        left: Math.round(cr.left),
                        width: Math.round(cr.width),
                        height: Math.round(cr.height)
                    };
                })
                .filter(c => c.width >= 120 && c.height >= 120)
                .sort((a,b) => a.top - b.top || a.left - b.left);

            // De-dupe cards by approximate position.
            const out = [];
            const seen = new Set();
            for (const c of imgs) {
                const key = `${Math.round(c.left/20)}-${Math.round(c.top/20)}`;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push(c);
            }
            return out;
        }"""
    )

    if not cards:
        log("No visible media image cards found for alt text update.")
        return

    log(f"Updating alt text for {len(cards)} visible media image(s)...")

    updated = 0
    for idx, card in enumerate(cards, start=1):
        if stop_requested:
            log("Stopped while updating image alt text.")
            break

        text = alt_text_for(idx)
        try:
            page.mouse.move(card["x"], card["y"])
            page.wait_for_timeout(300)
        except Exception:
            pass

        res = page.evaluate(
            r"""({card, text}) => {
                function visible(el) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                function setValue(el, val) {
                    el.scrollIntoView({block:'center', inline:'nearest'});
                    el.focus();
                    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, '');
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    setter.call(el, val || '');
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    el.blur();
                }

                const inputs = [...document.querySelectorAll('input:not([type=hidden]), textarea')]
                    .filter(visible)
                    .map(el => ({el, r: el.getBoundingClientRect(), ph: el.placeholder || '', aria: el.getAttribute('aria-label') || '', val: el.value || ''}))
                    .filter(o => {
                        const cx = o.r.left + o.r.width/2;
                        const cy = o.r.top + o.r.height/2;
                        const inCard = cx >= card.left - 35 && cx <= card.left + card.width + 35 && cy >= card.top - 35 && cy <= card.top + card.height + 80;
                        const looksAlt = /alt\s*text/i.test(o.ph) || /alt\s*text/i.test(o.aria) || /alt/i.test(o.ph) || /alt/i.test(o.aria);
                        return inCard && looksAlt;
                    })
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);

                let target = inputs[0];
                if (!target) {
                    // Fallback: any visible text input under this card bottom, because Prockured shows an Alt text box there.
                    const fallback = [...document.querySelectorAll('input:not([type=hidden]), textarea')]
                        .filter(visible)
                        .map(el => ({el, r: el.getBoundingClientRect(), ph: el.placeholder || '', val: el.value || ''}))
                        .filter(o => {
                            const cx = o.r.left + o.r.width/2;
                            const cy = o.r.top + o.r.height/2;
                            return cx >= card.left - 35 && cx <= card.left + card.width + 35 && cy >= card.top && cy <= card.top + card.height + 110;
                        })
                        .sort((a,b) => b.r.top - a.r.top);
                    target = fallback[0];
                }

                if (!target) return {ok:false, reason:'alt-input-not-found'};
                const old = target.val;
                setValue(target.el, text);
                return {ok:true, old, newValue:text, placeholder:target.ph, y:Math.round(target.r.top)};
            }""",
            {"card": card, "text": text},
        )

        if res and res.get("ok"):
            updated += 1
            log(f"  Image {idx}: {res.get('newValue')}")
        else:
            log(f"  Image {idx}: could not update alt text ({res})")

    log(f"Alt text update done. Updated: {updated}/{len(cards)}")



def is_pricing_tab_visible(page):
    """Confirm the actual Pricing page is visible before pasting prices."""
    try:
        return bool(page.evaluate(
            r"""() => {
                function visible(el) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                const texts = [...document.querySelectorAll('h1,h2,h3,div,span,label,strong')]
                    .filter(visible)
                    .map(e => (e.innerText || e.textContent || '').replace(/\s+/g,' ').trim().toLowerCase());
                const hasHeading = texts.some(t => t.includes('pricing & availability'));
                const hasBase = texts.some(t => t.includes('base price'));
                const hasDiscount = texts.some(t => t.includes('discount price'));
                return hasHeading && hasBase && hasDiscount;
            }"""
        ))
    except Exception:
        return False


def open_pricing_tab(page):
    """Open Pricing tab reliably from any other tab.

    v21 fix:
    - Do not rely only on text locator because it can silently miss the tab in Brave.
    - Scroll to the top so the tab bar is visible.
    - Prefer role=tab exact name.
    - Fallback to direct mouse click on the center of the visible top-tab element.
    - Only returns True after Pricing & Availability + Base/Discount labels are visible.
    """
    log("Opening Pricing tab safely...")

    if is_pricing_tab_visible(page):
        return True

    # Make the sticky/top tab bar easy to locate.
    try:
        page.evaluate("() => { window.scrollTo({top: 0, left: 0, behavior: 'instant'}); }")
        page.wait_for_timeout(250)
    except Exception:
        pass

    # 1) Best method for Radix/shadcn style tabs: real accessible tab.
    try:
        tab = page.get_by_role("tab", name=re.compile(r"^\s*Pricing\s*$", re.I)).first
        if tab.count() > 0:
            tab.scroll_into_view_if_needed()
            tab.click(force=True, timeout=1500)
            page.wait_for_timeout(1200)
            if is_pricing_tab_visible(page):
                return True
    except Exception:
        pass

    # 2) Try visible button with exact text Pricing.
    try:
        btn = page.locator("button").filter(has_text=re.compile(r"^\s*Pricing\s*$", re.I)).first
        if btn.count() > 0:
            btn.scroll_into_view_if_needed()
            btn.click(force=True, timeout=1500)
            page.wait_for_timeout(1200)
            if is_pricing_tab_visible(page):
                return True
    except Exception:
        pass

    # 3) Direct coordinate click on the visible top navigation item. This is the most reliable fallback
    # when Playwright's element click hits a nested/span element but the UI does not switch.
    try:
        info = page.evaluate(
            r"""() => {
                function visible(el) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }
                const allowed = 'button,a,[role="tab"],[data-state],div,span';
                const candidates = [...document.querySelectorAll(allowed)]
                    .filter(visible)
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        const text = (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim();
                        const role = el.getAttribute('role') || '';
                        const dataState = el.getAttribute('data-state') || '';
                        return {el, text, role, dataState, x:r.left, y:r.top, w:r.width, h:r.height};
                    })
                    .filter(o => /^pricing$/i.test(o.text))
                    // top product-tab bar only, not random field/helper text lower on page
                    .filter(o => o.y >= 60 && o.y <= 260 && o.w >= 35 && o.w <= 220 && o.h >= 20 && o.h <= 80)
                    .sort((a,b) => {
                        // prefer real tab/buttons, then top-left order
                        const aw = ((a.role === 'tab') ? 0 : 10) + (a.el.tagName === 'BUTTON' ? 0 : 3) + a.y/1000 + a.x/10000;
                        const bw = ((b.role === 'tab') ? 0 : 10) + (b.el.tagName === 'BUTTON' ? 0 : 3) + b.y/1000 + b.x/10000;
                        return aw - bw;
                    });
                if (!candidates.length) return {ok:false, candidates:[]};
                const c = candidates[0];
                return {ok:true, x:Math.round(c.x + c.w/2), y:Math.round(c.y + c.h/2), chosen:{text:c.text, role:c.role, dataState:c.dataState, x:Math.round(c.x), y:Math.round(c.y), w:Math.round(c.w), h:Math.round(c.h)}, candidates:candidates.slice(0,5).map(c => ({text:c.text, role:c.role, dataState:c.dataState, x:Math.round(c.x), y:Math.round(c.y), w:Math.round(c.w), h:Math.round(c.h)}))};
            }"""
        )
        if info.get("ok"):
            log(f"Pricing tab coordinate click: {info.get('chosen')}")
            page.mouse.click(info["x"], info["y"])
            page.wait_for_timeout(1300)
            if is_pricing_tab_visible(page):
                return True
        else:
            log(f"Pricing tab coordinate candidate not found: {info}")
    except Exception as e:
        log(f"Pricing coordinate click failed: {e}")

    # 4) Last resort: JS click exact top tab element, using event dispatch.
    try:
        jsres = page.evaluate(
            r"""() => {
                function visible(el) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                const els = [...document.querySelectorAll('button,a,[role="tab"],div,span')]
                    .filter(visible)
                    .map(el => ({el, text:(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim(), r:el.getBoundingClientRect(), role:el.getAttribute('role')||''}))
                    .filter(o => /^pricing$/i.test(o.text) && o.r.top >= 60 && o.r.top <= 260)
                    .sort((a,b) => (a.role === 'tab' ? 0 : 10) - (b.role === 'tab' ? 0 : 10) || a.r.left - b.r.left);
                if (!els.length) return false;
                const el = els[0].el;
                el.scrollIntoView({block:'center', inline:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(type => el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, view:window})));
                return true;
            }"""
        )
        if jsres:
            page.wait_for_timeout(1300)
            if is_pricing_tab_visible(page):
                return True
    except Exception:
        pass

    return False


def set_pricing_fields(page, base_price: int, sale_price: int):
    """Fill Base Price + Discount Price only after Pricing page is confirmed.

    It uses this final rule for the simple-product pricing screen:
    visible pricing field #1 = Base Price
    visible pricing field #2 = Discount Price

    This avoids the earlier bug where numbers got pasted into SEO fields.
    """
    if not is_pricing_tab_visible(page):
        return {"baseFound": False, "discountFound": False, "error": "Not on Pricing tab - refused to paste"}

    return page.evaluate(
        r"""({basePrice, salePrice}) => {
            function isVisible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            }
            function setValue(el, val) {
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.focus();
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                setter.call(el, String(val));
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }

            const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea')]
                .filter(isVisible)
                .filter(el => !el.disabled && !el.readOnly)
                .map(el => ({el, r:el.getBoundingClientRect(), old:el.value || '', ph:el.placeholder || '', tag:el.tagName}))
                .filter(f => f.r.top > 280)  // skip top navigation/search fields
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);

            const base = fields[0] || null;
            const discount = fields[1] || null;

            if (base) setValue(base.el, basePrice);
            if (discount) setValue(discount.el, salePrice);

            return {
                baseFound: !!base,
                discountFound: !!discount,
                baseOld: base ? base.old : null,
                discountOld: discount ? discount.old : null,
                baseY: base ? Math.round(base.r.top) : null,
                discountY: discount ? Math.round(discount.r.top) : null,
                visiblePricingFields: fields.map((f, i) => ({index:i+1, tag:f.tag, placeholder:f.ph, old:f.old, y:Math.round(f.r.top)}))
            };
        }""",
        {"basePrice": base_price, "salePrice": sale_price},
    )


def fill_pricing(page, data: ProductData):
    product_name = get_product_name_for_matching(page, data)
    if not product_name:
        log("Could not determine product name for pricing matching.")
        return

    sale_raw = find_price_from_loaded_data(data)
    price_source = "[PRICING] section"
    best_csv = None
    if sale_raw is None:
        best_csv = find_best_price_in_csv(product_name, DEFAULT_OUTPUT_DIR)
        if best_csv:
            sale_raw = best_csv["price"]
            price_source = f"CSV match score {best_csv['score']} title '{best_csv['title']}'"
    if sale_raw is None:
        log("No price found. Add [PRICING] Sale Price : ... or check scraper CSV price columns.")
        return

    base_price, sale_price, markup = compute_base_and_discount_prices(product_name, sale_raw)

    if not open_pricing_tab(page):
        # Batch safety retry: old pricing logic is still used, but if the first open attempt
        # misses the tab, force the page to the top and try the exact Pricing tab one more time.
        try:
            page.evaluate("() => { window.scrollTo({top: 0, left: 0, behavior: 'instant'}); }")
            page.wait_for_timeout(500)
            try:
                page.get_by_role("tab", name=re.compile(r"^\s*Pricing\s*$", re.I)).first.click(timeout=1800, force=True)
            except Exception:
                try:
                    page.get_by_role("button", name=re.compile(r"^\s*Pricing\s*$", re.I)).first.click(timeout=1800, force=True)
                except Exception:
                    page.evaluate(r"""() => {
                        function visible(el){ const r=el.getBoundingClientRect(); const s=getComputedStyle(el); return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'; }
                        const els=[...document.querySelectorAll('button,a,[role=tab],div,span')]
                          .filter(visible)
                          .map(el=>({el,t:(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim(),r:el.getBoundingClientRect(),role:el.getAttribute('role')||''}))
                          .filter(o=>/^pricing$/i.test(o.t) && o.r.top>=50 && o.r.top<=300)
                          .sort((a,b)=>(a.role==='tab'?0:10)-(b.role==='tab'?0:10)||a.r.top-b.r.top||a.r.left-b.r.left);
                        if(els.length){ els[0].el.scrollIntoView({block:'center', inline:'nearest'}); els[0].el.click(); return true; }
                        return false;
                    }""")
            page.wait_for_timeout(1500)
        except Exception:
            pass

    if not is_pricing_tab_visible(page):
        log("Could not confirm Pricing tab. Pricing values were NOT pasted anywhere.")
        log("Open Pricing tab manually and press Alt + Shift + R again.")
        return

    log(f"Pricing for: {product_name}")
    log(f"Sale/discount price source: {price_source}")
    log(f"Sale/discount price rounded up: {sale_price}")
    log(f"Stable markup: {markup:.2f}%")
    log(f"Base price rounded up: {base_price}")

    res = set_pricing_fields(page, base_price, sale_price)
    log(f"Pricing field result: {res}")
    log("Pricing fill done.")

def debug_current_tab(page):
    log("\nVisible fields on current tab:")
    for f in visible_fields(page):
        val = f['value']
        if len(val) > 60:
            val = val[:60] + '...'
        log(f"#{f['index']} {f['tag']} type={f['type']} label='{f['label']}' placeholder='{f['placeholder']}' value='{val}' x={f['x']} y={f['y']} w={f['w']} h={f['h']}")
    log("")


def load_from_clipboard():
    global current_data
    text = pyperclip.paste()
    data = parse_clipboard_text(text)
    current_data = data
    log("\nLoaded product data from clipboard:")
    log(f"  Basics keys: {list(data.basics.keys())}")
    log(f"  Attributes: {len(data.attributes)}")
    log(f"  Filter Attributes: {len(data.filter_attributes)}")
    log(f"  Variant Attributes: {len(data.variant_attributes)}")
    log(f"  Variant + Filter Attributes: {len(data.variant_filter_attributes)}")
    log(f"  Variant Pricing rows: {len(data.variant_pricing)}")
    log(f"  SEO keys: {list(data.seo.keys())}")
    log("  Slug will be cleared/left empty. SKU, Brand and Category will not be touched.")
    if not data.basics and not data.attributes and not data.seo:
        log("  WARNING: No [BASICS], [ATTRIBUTES], [SEO] data found.")


def require_data():
    if current_data is None:
        load_from_clipboard()
    return current_data



# ----------------------------
# Batch Admin Filler (JSON -> Admin Products)
# ----------------------------

ATTRIBUTE_LABEL_MAP = {
    "quantity": "Quantity",
    "product_type": "Product Type",
    "food_type": "Food Type",
    "pack_type": "Pack Type",
    "packing_type": "Packing Type",
    "form": "Form",
    "storage_type": "Storage Type",
    "preparation_type": "Preparation Type",
    "usage": "Usage",
    "suitable_for": "Suitable For",
    "service_type": "Service Type",
    "material": "Material",
    "colour": "Colour",
    "color": "Colour",
    "size": "Size",
    "capacity": "Capacity",
    "dimensions": "Dimensions",
    "gsm": "GSM",
    "shape": "Shape",
    "model_name": "Model Name",
    "power": "Power",
    "voltage": "Voltage",
    "temperature_range": "Temperature Range",
    "application": "Application",
}


def display_label_from_key(key: str) -> str:
    k = norm_key(str(key)).replace(" ", "_")
    if k in ATTRIBUTE_LABEL_MAP:
        return ATTRIBUTE_LABEL_MAP[k]
    return " ".join(part.capitalize() for part in re.split(r"[_\s]+", str(key).strip()) if part)


class BatchLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")

    def write(self, msg=""):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} | {msg}"
        log(line)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def write_csv_report(path: Path, rows: list, fieldnames: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def load_batch_package(path: Path) -> tuple[list, dict]:
    """Load products plus optional top-level config from the batch JSON."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Batch JSON not found: {path}")
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        raise ValueError(f"Batch JSON file is empty: {path}")

    payload = json.loads(raw)
    config = {}
    data = payload
    if isinstance(payload, dict):
        config = payload.get("config") or {}
        for key in ("products", "batch_products", "items", "data"):
            if key in payload:
                data = payload[key]
                break
        else:
            if any(k in payload for k in ("admin", "basics", "attributes", "seo", "pricing")):
                data = [payload]

    if not isinstance(data, list):
        raise ValueError(
            "Batch JSON must be a list, a single product object, or an object with a products list."
        )

    cleaned = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Batch product #{i} must be an object, got {type(item).__name__}.")
        cleaned.append(item)
    return cleaned, config


def load_batch_json(path: Path) -> list:
    products, _config = load_batch_package(path)
    return products

def value_is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def item_get(item: dict, section: str, key: str, default=""):
    sec = item.get(section) or {}
    val = sec.get(key, default)
    if val is None:
        return default
    return val


MODEL_NAME_ALIASES = {
    "model_name",
    "model",
    "model_no",
    "model_number",
    "modelnumber",
    "model_code",
}


def normalize_model_name(value: str) -> str:
    """Normalize a model/SKU fragment for reliable comparison."""
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def batch_model_name(item: dict) -> str:
    """Read Model Name from OMS JSON without requiring SKU.

    Accepted locations:
    - top-level model_name/model/model_no/model_number/model_code
    - admin
    - basics
    - attributes (legacy OMS mapping)
    """
    if not isinstance(item, dict):
        return ""

    sections = [item, item.get("admin") or {}, item.get("basics") or {}, item.get("attributes") or {}]
    for section in sections:
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            normalized_key = norm_key(str(key)).replace(" ", "_")
            if normalized_key in MODEL_NAME_ALIASES and not value_is_blank(value):
                return str(value).strip()
    return ""


def batch_lookup_details(item: dict, lookup_mode: str = "sku") -> tuple[str, str]:
    """Use SKU by default. Model Number is used only when CLI requests it."""
    admin = item.get("admin") or {}
    mode = str(lookup_mode or "sku").strip().lower()
    if mode == "modelnumber":
        return "modelnumber", batch_model_name(item)
    return "sku", str(admin.get("sku") or "").strip()

def batch_item_missing_fields(item: dict, lookup_mode: str = "sku") -> list:
    missing = []
    lookup_type, lookup_value = batch_lookup_details(item, lookup_mode)
    if value_is_blank(lookup_value):
        missing.append("admin.sku" if lookup_type == "sku" else "model_number")

    if value_is_blank(item_get(item, "admin", "category_option", "")):
        missing.append("admin.category_option")

    for section, key in [
        ("basics", "description"),
        ("basics", "short_description"),
        ("seo", "seo_title"),
        ("seo", "seo_description"),
        ("seo", "seo_keywords"),
    ]:
        if value_is_blank(item_get(item, section, key, "")):
            missing.append(f"{section}.{key}")

    all_attr_sections = [
        item.get("attributes") or {},
        item.get("filter_attributes") or {},
        item.get("variant_attributes") or {},
        item.get("variant_filter_attributes") or {},
    ]
    has_attribute = False
    for attrs in all_attr_sections:
        for key, value in attrs.items():
            if norm_key(str(key)).replace(" ", "_") in MODEL_NAME_ALIASES:
                continue
            if not value_is_blank(value):
                has_attribute = True
                break
        if has_attribute:
            break
    if not has_attribute:
        missing.append("attributes")
    return missing

ALL_BATCH_SECTIONS = {
    "category", "brand", "basics", "attributes", "variations", "pricing", "seo", "media"
}


def fill_sections_for_item(item: dict, requested_sections: set | None = None) -> set:
    """Category is always included because the admin cannot save without it."""
    out = set(requested_sections or ALL_BATCH_SECTIONS)
    out.add("category")
    admin = item.get("admin") or {}
    if bool(admin.get("skip_media")):
        out.discard("media")
    if bool(admin.get("skip_pricing")):
        out.discard("pricing")
    return out

def dict_get_ci(mapping: dict, *keys, default=None):
    """Read JSON configuration keys case-insensitively and punctuation-insensitively."""
    if not isinstance(mapping, dict):
        return default
    normalized = {
        re.sub(r"[^a-z0-9]+", "", str(k).lower()): v
        for k, v in mapping.items()
    }
    for key in keys:
        nk = re.sub(r"[^a-z0-9]+", "", str(key).lower())
        if nk in normalized and normalized[nk] is not None:
            return normalized[nk]
    return default


def _resolve_path_from_run_dir(value, base: Path = RUN_DIR) -> Path:
    p = Path(str(value or "").strip())
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def product_data_from_batch_item(
    item: dict,
    update_product_name: bool = False,
    batch_config: dict | None = None,
    run_options: BatchRunOptions | None = None,
    force_variable: bool = False,
) -> ProductData:
    data = ProductData()
    basics = item.get("basics") or {}
    seo = item.get("seo") or {}
    pricing = item.get("pricing") or {}
    admin = item.get("admin") or {}

    product_name_value = str(basics.get("product_name") or "").strip()
    if product_name_value:
        data.basics["Product Name"] = product_name_value
        data.basics["__Batch Product Name"] = product_name_value

    product_type = str(basics.get("product_type") or "").strip()
    if force_variable:
        product_type = "Variable Product"
    if product_type:
        data.basics["Product Type"] = product_type

    if not value_is_blank(basics.get("product_tags")):
        data.basics["Product Tags"] = str(basics.get("product_tags")).strip()
    if not value_is_blank(basics.get("description")):
        data.basics["Description"] = str(basics.get("description")).strip()
    if not value_is_blank(basics.get("short_description")):
        data.basics["Short Description"] = str(basics.get("short_description")).strip()

    model_value = batch_model_name(item)
    if model_value:
        data.basics["Model Name"] = model_value
        data.basics["__Batch Model Name"] = model_value
    data.basics["__Batch SKU"] = str(admin.get("sku") or "").strip()
    data.basics["__Batch Brand"] = str(admin.get("brand_option") or basics.get("brand") or "").strip()

    def add_attribute_mapping(target, mapping, split_list=True):
        if not isinstance(mapping, dict):
            return
        for key, value in mapping.items():
            normalized_key = norm_key(str(key)).replace(" ", "_")
            if normalized_key in MODEL_NAME_ALIASES or value_is_blank(value):
                continue
            if isinstance(value, list):
                value = ", ".join(str(v).strip() for v in value if not value_is_blank(v))
            target.append((display_label_from_key(key), str(value).strip()))

    add_attribute_mapping(data.attributes, item.get("attributes") or {})
    add_attribute_mapping(data.filter_attributes, item.get("filter_attributes") or {})
    add_attribute_mapping(data.variant_attributes, item.get("variant_attributes") or {})
    add_attribute_mapping(data.variant_filter_attributes, item.get("variant_filter_attributes") or {})

    vpricing = item.get("variant_pricing") or []
    if isinstance(vpricing, list):
        for row in vpricing:
            if not isinstance(row, dict):
                continue
            attrs_row = row.get("attributes") or {
                k: v for k, v in row.items() if k not in {"price", "sale_price", "price_raw"}
            }
            price_val = row.get("price", row.get("sale_price", row.get("price_raw", "")))
            data.variant_pricing.append({
                "attributes": attrs_row,
                "price_raw": str(price_val),
                "price": parse_price_value(price_val),
            })

    if not value_is_blank(seo.get("seo_title")):
        data.seo["SEO Title"] = str(seo.get("seo_title")).strip()
    if not value_is_blank(seo.get("seo_description")):
        data.seo["SEO Description"] = str(seo.get("seo_description")).strip()
    if not value_is_blank(seo.get("seo_keywords")):
        data.seo["SEO Keywords"] = str(seo.get("seo_keywords")).strip()
    if not value_is_blank(pricing.get("sale_price")):
        data.pricing["Sale Price"] = str(pricing.get("sale_price")).strip()

    batch_config = batch_config or {}
    batch_media = dict_get_ci(batch_config, "media", default={}) or {}
    product_media = dict_get_ci(item, "media", default={}) or {}
    options = run_options or BatchRunOptions()

    # CLI overrides product JSON, which overrides batch JSON, which overrides RUN_DIR/images.
    root_value = (
        str(options.force_media_root) if options.force_media_root
        else dict_get_ci(product_media, "root", "images_root", "forceMedia")
        or dict_get_ci(batch_media, "root", "images_root", "forceMedia")
        or dict_get_ci(batch_config, "forceMedia", "media_root", "images_root")
        or str(DEFAULT_IMAGE_ROOT)
    )
    media_root = _resolve_path_from_run_dir(root_value)

    media_sort = (
        options.media_sort
        or dict_get_ci(product_media, "sort", "media_sort", "MediaSort")
        or dict_get_ci(batch_media, "sort", "media_sort", "MediaSort")
        or dict_get_ci(batch_config, "MediaSort", "media_sort")
        or "modelnumber"
    )
    min_score = (
        options.media_min_score
        if options.media_min_score is not None
        else dict_get_ci(
            product_media,
            "min_score", "media_min_score", "mediaMinScore",
            default=dict_get_ci(
                batch_media,
                "min_score", "media_min_score", "mediaMinScore",
                default=dict_get_ci(batch_config, "mediaMinScore", "media_min_score", default=85),
            ),
        )
    )

    direct_folder = dict_get_ci(product_media, "folder", "image_folder")
    if direct_folder:
        folder_path = Path(str(direct_folder))
        if not folder_path.is_absolute():
            folder_path = media_root / folder_path
        data.media["Image Folder"] = str(folder_path.resolve())

    data.media["__Media Root"] = str(media_root)
    data.media["__Media Sort"] = str(media_sort)
    data.media["__Media Min Score"] = str(min_score)
    data.media["__SKU"] = data.basics.get("__Batch SKU", "")
    data.media["__Model Number"] = model_value
    data.media["__Brand"] = data.basics.get("__Batch Brand", "")
    data.media["__Product Name"] = product_name_value

    replace_value = dict_get_ci(product_media, "replace_existing_images", "replace_images")
    if replace_value is not None:
        data.media["Replace Existing Images"] = str(replace_value)
    return data

def read_field_value_by_label(page, label_names) -> str:
    if isinstance(label_names, str):
        label_names = [label_names]
    try:
        res = page.evaluate(
            r"""({labels}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                function norm(s) { return (s || '').replace(/\*/g, '').replace(/\s+/g, ' ').trim().toUpperCase(); }
                const wanted = labels.map(norm);
                const allLabels = [...document.querySelectorAll('label, div, span, p, strong')]
                    .filter(visible)
                    .map(el => ({el, text:norm(el.innerText || el.textContent), r:el.getBoundingClientRect()}))
                    .filter(o => wanted.includes(o.text))
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (!allLabels.length) return {ok:false, reason:'label-not-found'};
                const lab = allLabels[0];
                const fields = [...document.querySelectorAll('input:not([type=hidden]), textarea, [contenteditable="true"]')]
                    .filter(visible)
                    .map(el => ({el, r:el.getBoundingClientRect(), value: el.value || el.innerText || el.textContent || ''}))
                    .filter(o => o.r.top >= lab.r.bottom - 20 && o.r.top < lab.r.bottom + 240)
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (fields.length) return {ok:true, value:fields[0].value};
                const nearText = [...document.querySelectorAll('div, span, p')]
                    .filter(visible)
                    .map(el => ({el, r:el.getBoundingClientRect(), text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim()}))
                    .filter(o => o.r.top >= lab.r.bottom - 20 && o.r.top < lab.r.bottom + 240 && o.text && o.text.length < 120)
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (nearText.length) return {ok:true, value:nearText[0].text};
                return {ok:false, reason:'field-not-found'};
            }""",
            {"labels": label_names},
        )
        if res and res.get("ok"):
            return str(res.get("value") or "").strip()
    except Exception:
        pass
    return ""


def read_current_model_name_from_page(page) -> str:
    """Read the dedicated Model Name input from the current edit page."""
    value = read_field_value_by_label(page, ["MODEL NAME", "Model Name"])
    if value:
        return value
    try:
        box = page.get_by_placeholder(re.compile(r"optional\s+model\s+identifier", re.I)).first
        if box.count() > 0:
            return str(box.input_value(timeout=1200) or "").strip()
    except Exception:
        pass
    return ""


def verify_model_name_in_sku_on_edit_page(page, expected_model_name: str) -> tuple[bool, str]:
    """Verify Model Number from the dedicated field or as a substring of SKU."""
    page.wait_for_timeout(800)
    actual_sku = read_field_value_by_label(page, ["SKU", "Sku"])
    actual_model = read_current_model_name_from_page(page)

    expected_norm = normalize_model_name(expected_model_name)
    sku_norm = normalize_model_name(actual_sku)
    dedicated_norm = normalize_model_name(actual_model)

    sku_match = bool(expected_norm and expected_norm in sku_norm)
    dedicated_match = bool(expected_norm and dedicated_norm == expected_norm)
    details = (
        f"SKU={actual_sku or '<blank>'}; "
        f"Model Name field={actual_model or '<blank>'}; "
        f"model_in_sku={sku_match}"
    )
    return dedicated_match or sku_match, details

def verify_batch_lookup_on_edit_page(page, lookup_type: str, expected_value: str) -> tuple[bool, str]:
    if lookup_type == "modelnumber":
        return verify_model_name_in_sku_on_edit_page(page, expected_value)
    return verify_sku_on_edit_page(page, expected_value)

def normalize_sku(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(s or "")).upper()

def open_products_list(page):
    page.goto(ADMIN_PRODUCTS_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)


def fill_admin_search(page, query: str) -> bool:
    query = str(query or "").strip()
    if not query:
        return False
    try:
        res = page.evaluate(
            r"""({query}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                function setValue(el, val) {
                    el.scrollIntoView({block:'center', inline:'nearest'});
                    el.focus();
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(el, '');
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }
                const inputs = [...document.querySelectorAll('input:not([type=hidden])')]
                    .filter(visible)
                    .map(el => ({el, ph:(el.placeholder || '').toLowerCase(), aria:(el.getAttribute('aria-label') || '').toLowerCase(), r:el.getBoundingClientRect()}))
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                let target = inputs.find(o => o.ph.includes('search') || o.aria.includes('search')) || inputs[0];
                if (!target) return {ok:false, reason:'search-input-not-found'};
                setValue(target.el, query);
                return {ok:true, placeholder:target.ph, y:Math.round(target.r.top)};
            }""",
            {"query": query},
        )
        if res and res.get("ok"):
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            page.wait_for_timeout(2000)
            return True
        log(f"Search input not found: {res}")
    except Exception as e:
        log(f"Search failed: {e}")
    return False


def click_product_result_by_model_in_sku(page, model_name: str) -> bool:
    """Open the filtered row whose SKU contains the requested Model Number."""
    wanted = normalize_model_name(model_name)
    if not wanted:
        return False

    log(f"Matching filtered product row where SKU contains Model Number: {model_name}")
    try:
        result = page.evaluate(
            r"""({modelName}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                function compact(s) { return (s || '').toUpperCase().replace(/[^A-Z0-9]+/g, ''); }
                function clean(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
                function clickEdit(container) {
                    const controls = [...container.querySelectorAll('a,button,[role="button"]')]
                        .filter(visible)
                        .filter(el => /^edit$/i.test(clean(
                            el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || ''
                        )));
                    if (!controls.length) return false;
                    controls[0].scrollIntoView({block:'center', inline:'nearest'});
                    controls[0].click();
                    return true;
                }

                const wanted = compact(modelName);
                const tables = [...document.querySelectorAll('table')].filter(visible);
                for (const table of tables) {
                    const headers = [...table.querySelectorAll('thead th, [role="columnheader"]')].filter(visible);
                    const skuIndex = headers.findIndex(h => /^sku$/i.test(clean(h.innerText || h.textContent || '')));
                    const rows = [...table.querySelectorAll('tbody tr, tr[role="row"]')].filter(visible);
                    for (const row of rows) {
                        const cells = [...row.querySelectorAll(':scope > td, :scope > [role="cell"]')];
                        let skuText = '';
                        if (skuIndex >= 0 && skuIndex < cells.length) {
                            skuText = clean(cells[skuIndex].innerText || cells[skuIndex].textContent || '');
                        } else {
                            const pieces = [...row.querySelectorAll('td,div,span,p')]
                                .filter(visible)
                                .map(el => clean(el.innerText || el.textContent || ''))
                                .filter(t => compact(t).includes(wanted));
                            skuText = pieces.sort((a,b) => a.length - b.length)[0] || '';
                        }
                        if (!compact(skuText).includes(wanted)) continue;
                        if (clickEdit(row)) return {ok:true, method:'table-sku-contains-model', skuText};
                    }
                }

                const edits = [...document.querySelectorAll('a,button,[role="button"]')]
                    .filter(visible)
                    .filter(el => /^edit$/i.test(clean(el.innerText || el.textContent || el.getAttribute('aria-label') || '')));
                for (const edit of edits) {
                    let node = edit;
                    for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
                        if (!visible(node)) continue;
                        const r = node.getBoundingClientRect();
                        const cardText = clean(node.innerText || node.textContent || '');
                        if (!compact(cardText).includes(wanted)) continue;
                        if (r.width < 250 || r.height < 40 || r.height > 650) continue;
                        edit.scrollIntoView({block:'center', inline:'nearest'});
                        edit.click();
                        return {ok:true, method:'card-sku-contains-model', cardText:cardText.slice(0,300)};
                    }
                }
                return {ok:false, reason:'no-row-sku-containing-model', wanted};
            }""",
            {"modelName": model_name},
        )
        log(f"Model-in-SKU row click result: {result}")
        if result and result.get("ok"):
            page.wait_for_timeout(3000)
            return True
    except Exception as e:
        log(f"Model-in-SKU row click failed: {e}")
    return False

def click_first_product_result(page, sku: str) -> bool:
    """After product list search, open first result using Edit/name/card fallback."""
    sku = str(sku or "").strip()
    # First try obvious Edit button/link.
    for locator_attempt in [
        lambda: page.get_by_role("button", name=re.compile(r"^\s*edit\s*$", re.I)).first,
        lambda: page.get_by_role("link", name=re.compile(r"^\s*edit\s*$", re.I)).first,
        lambda: page.locator("button").filter(has_text=re.compile(r"edit", re.I)).first,
        lambda: page.locator("a").filter(has_text=re.compile(r"edit", re.I)).first,
    ]:
        try:
            loc = locator_attempt()
            if loc.count() > 0:
                loc.click(timeout=2500, force=True)
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass

    # JS fallback: find row/card containing SKU, click Edit inside it, otherwise click a link/button in it.
    try:
        res = page.evaluate(
            r"""({sku}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                const skuText = (sku || '').toLowerCase();
                const containers = [...document.querySelectorAll('tr, [role="row"], li, article, section, div')]
                    .filter(visible)
                    .map(el => ({el, text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(), r:el.getBoundingClientRect()}))
                    .filter(o => o.text && o.text.length < 2000)
                    .filter(o => skuText ? o.text.toLowerCase().includes(skuText) : true)
                    .sort((a,b) => (a.r.width*a.r.height) - (b.r.width*b.r.height));
                const row = containers[0]?.el || null;
                if (row) {
                    const edit = [...row.querySelectorAll('button,a')].filter(visible).find(el => /edit/i.test(el.innerText || el.textContent || el.getAttribute('aria-label') || ''));
                    if (edit) { edit.click(); return {ok:true, method:'row-edit'}; }
                    const clickable = [...row.querySelectorAll('a,button')].filter(visible)[0];
                    if (clickable) { clickable.click(); return {ok:true, method:'row-clickable'}; }
                    row.click();
                    return {ok:true, method:'row-click'};
                }
                const anyEdit = [...document.querySelectorAll('button,a')].filter(visible).find(el => /edit/i.test(el.innerText || el.textContent || el.getAttribute('aria-label') || ''));
                if (anyEdit) { anyEdit.click(); return {ok:true, method:'any-edit'}; }
                const productLink = [...document.querySelectorAll('a,button')]
                    .filter(visible)
                    .map(el => ({el, text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(), r:el.getBoundingClientRect()}))
                    .filter(o => o.text && o.text.length > 3 && o.r.top > 120)
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left)[0]?.el || null;
                if (productLink) { productLink.click(); return {ok:true, method:'first-product-link'}; }
                return {ok:false, reason:'no-result-click-target'};
            }""",
            {"sku": sku},
        )
        if res and res.get("ok"):
            page.wait_for_timeout(2000)
            return True
        log(f"Could not open product result: {res}")
    except Exception as e:
        log(f"Product result click failed: {e}")
    return False


def verify_sku_on_edit_page(page, expected_sku: str) -> tuple[bool, str]:
    page.wait_for_timeout(1000)
    actual = read_field_value_by_label(page, ["SKU", "Sku"])
    if not actual:
        # Fallback: scan page text for expected SKU.
        try:
            found = page.evaluate("(sku) => document.body.innerText.includes(sku)", str(expected_sku))
            if found:
                actual = str(expected_sku)
        except Exception:
            pass
    return normalize_sku(actual) == normalize_sku(expected_sku), actual


def normalize_select_text(text: str) -> str:
    """Normalize dropdown text so Food & Beverage and Food and Beverage still match."""
    text = str(text or "").strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    # In your admin category names, the uploaded JSON may say
    # Food & Beverage Ingredients while the dropdown says Food and Beverage.
    # Removing repeated/generic category words makes the comparison tolerant
    # without allowing unrelated categories such as Acrylic Packaging.
    tokens = [t for t in text.split() if t]
    return " ".join(tokens)


def category_search_text(option_text: str) -> str:
    """Use the category name before brackets as the dropdown search query.

    Example:
    Bakery Ingredients (Food & Beverage Ingredients) -> Bakery Ingredients
    This avoids typing a full label that may differ slightly from the admin text.
    """
    s = str(option_text or "").strip()
    before = re.split(r"\s*[\(\[]", s, maxsplit=1)[0].strip()
    return before or s


def _click_visible_option_flexible(page, option_text: str, search_text: str = "", exact: bool = False):
    """Click visible dropdown result using tolerant matching.

    It prefers exact visible text, then accepts options that start with or contain
    the main search text. This fixes cases like:
    JSON:    Bakery Ingredients (Food & Beverage Ingredients)
    Admin:   Bakery Ingredients (Food and Beverage)
    """
    option_text = str(option_text or "").strip()
    search_text = str(search_text or "").strip()
    if not option_text and not search_text:
        return False

    try:
        clicked = page.evaluate(
            r"""({optionText, searchText, exact}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }
                function norm(s) {
                    return (s || '')
                        .replace(/&/g, ' and ')
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, ' ')
                        .replace(/\s+/g, ' ')
                        .trim();
                }
                function scoreCandidate(t) {
                    const nt = norm(t);
                    const no = norm(optionText);
                    const ns = norm(searchText || optionText);
                    if (!nt) return -999;
                    if (exact && nt === no) return 1000;
                    if (nt === no) return 950;
                    if (ns && nt === ns) return 940;
                    if (ns && nt.startsWith(ns)) return 900;
                    if (ns && nt.includes(ns)) return 850;
                    if (no && no.startsWith(nt) && nt.length >= 8) return 800;
                    if (no && nt.startsWith(no.split('(')[0]?.trim() || no)) return 780;
                    const ntoks = new Set(nt.split(' ').filter(Boolean));
                    const stoks = (ns || no).split(' ').filter(Boolean);
                    const required = stoks.filter(x => !['and','of','the','ingredients','ingredient'].includes(x));
                    const hits = required.filter(x => ntoks.has(x)).length;
                    if (required.length && hits === required.length) return 760 + hits;
                    return -999;
                }
                function clickEl(el) {
                    el.scrollIntoView({block:'center', inline:'nearest'});
                    const r = el.getBoundingClientRect();
                    const x = r.left + r.width / 2;
                    const y = r.top + r.height / 2;
                    try { el.click(); return true; } catch(e) {}
                    ['pointerdown','mousedown','mouseup','click'].forEach(type => {
                        el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y, view:window}));
                    });
                    return true;
                }
                const els = [...document.querySelectorAll('[role="option"], button, [role="button"], div, span, li')]
                    .filter(visible)
                    .map(el => ({
                        el,
                        text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(),
                        r:el.getBoundingClientRect()
                    }))
                    .filter(o => o.text && o.text.length <= 180)
                    .map(o => ({...o, score:scoreCandidate(o.text)}))
                    .filter(o => o.score > -999)
                    .sort((a,b) => b.score - a.score || (a.r.width*a.r.height) - (b.r.width*b.r.height));
                if (!els.length) return {ok:false, optionText, searchText, reason:'no-flexible-option-found'};
                clickEl(els[0].el);
                return {ok:true, clickedText:els[0].text, score:els[0].score, optionText, searchText};
            }""",
            {"optionText": option_text, "searchText": search_text, "exact": exact},
        )
        if clicked and clicked.get("ok"):
            page.wait_for_timeout(450)
            log(f"  Dropdown option clicked: {clicked}")
            return True
        log(f"  Dropdown flexible click failed: {clicked}")
    except Exception as e:
        log(f"  Dropdown flexible click error: {e}")
    return False


def fill_searchable_select_exact(page, label_text: str, option_text: str, required: bool = False, search_text: str = "") -> bool:
    option_text = str(option_text or "").strip()
    search_text = str(search_text or option_text or "").strip()
    if not option_text:
        return not required

    log(f"Selecting {label_text}: {option_text}")
    res = _click_field_below_label(page, label_text)
    log(f"  {label_text} open result: {res}")
    if not res or not res.get("ok"):
        return False

    page.wait_for_timeout(300)

    # Avoid Control+A unless a real input/textarea is focused. Earlier it selected
    # page text in Brave and made category selection unstable.
    try:
        active_is_textbox = page.evaluate(
            """() => {
                const el = document.activeElement;
                if (!el) return false;
                const tag = (el.tagName || '').toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                return tag === 'textarea' || (tag === 'input' && !['hidden','checkbox','radio','file'].includes(type));
            }"""
        )
        if active_is_textbox:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
    except Exception:
        pass

    try:
        page.keyboard.insert_text(search_text)
    except Exception:
        try:
            page.keyboard.type(search_text, delay=15)
        except Exception:
            return False

    page.wait_for_timeout(1100)

    # First try exact full option, then tolerant click using the shorter search text.
    clicked = _click_visible_option(page, option_text, exact=True)
    if not clicked:
        clicked = _click_visible_option_flexible(page, option_text, search_text=search_text, exact=False)

    # Do NOT press Enter as fallback for category/brand. Enter can keep the old
    # category or choose a wrong first option. If we did not click a visible match,
    # fail safely and report it.
    log(f"  {label_text} selected status: {clicked}")
    return bool(clicked)



def _set_input_value_js(page, placeholder_needles, value: str):
    """Click and set a visible input by placeholder using React-safe setters.

    This is used for the Category field because the label-based picker can hit
    the helper text/div instead of the actual searchable textbox on the current UI.
    """
    needles = placeholder_needles if isinstance(placeholder_needles, list) else [placeholder_needles]
    return page.evaluate(
        r"""({needles, value}) => {
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            }
            function setValue(el, val) {
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.click();
                el.focus();
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                setter.call(el, val || '');
                el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:val || ''}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            }
            const ns = needles.map(n => String(n || '').toLowerCase()).filter(Boolean);
            const inputs = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio])')]
                .filter(visible)
                .map(el => ({el, ph:(el.getAttribute('placeholder') || '').toLowerCase(), val:el.value || '', r:el.getBoundingClientRect()}))
                .filter(o => ns.some(n => o.ph.includes(n)))
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
            if (!inputs.length) return {ok:false, reason:'input-placeholder-not-found', needles};
            const target = inputs[0];
            setValue(target.el, value || '');
            return {
                ok:true,
                placeholder:target.ph,
                oldValue:target.val,
                newValue:value || '',
                x:Math.round(target.r.left),
                y:Math.round(target.r.top),
                w:Math.round(target.r.width),
                h:Math.round(target.r.height)
            };
        }""",
        {"needles": needles, "value": value or ""},
    )


def _read_input_value_js(page, placeholder_needles):
    needles = placeholder_needles if isinstance(placeholder_needles, list) else [placeholder_needles]
    try:
        return page.evaluate(
            r"""({needles}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }
                const ns = needles.map(n => String(n || '').toLowerCase()).filter(Boolean);
                const inputs = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio])')]
                    .filter(visible)
                    .map(el => ({el, ph:(el.getAttribute('placeholder') || '').toLowerCase(), val:el.value || '', r:el.getBoundingClientRect()}))
                    .filter(o => ns.some(n => o.ph.includes(n)))
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (!inputs.length) return '';
                return inputs[0].val || '';
            }""",
            {"needles": needles},
        ) or ""
    except Exception:
        return ""


def _find_category_input_box(page):
    """Return the real Category textbox under the exact CATEGORY label.

    This intentionally does NOT search placeholders globally, because the SKU helper
    text contains the word category and earlier versions typed category text into SKU.
    """
    return page.evaluate(
        r"""() => {
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            }
            function norm(s) {
                return (s || '').replace(/\*/g, '').replace(/\s+/g, ' ').trim().toUpperCase();
            }
            const labelNodes = [...document.querySelectorAll('label, div, span, p, strong')]
                .filter(visible)
                .map(el => ({el, text:norm(el.innerText || el.textContent), r:el.getBoundingClientRect()}))
                .filter(o => o.text === 'CATEGORY')
                .filter(o => (o.r.width * o.r.height) < 90000)
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
            if (!labelNodes.length) return {ok:false, reason:'category-label-not-found'};

            // Prefer the CATEGORY label that has an input directly below it.
            for (const lab of labelNodes) {
                const nextLabels = [...document.querySelectorAll('label, div, span, p, strong')]
                    .filter(visible)
                    .map(el => ({el, text:norm(el.innerText || el.textContent), r:el.getBoundingClientRect()}))
                    .filter(o => o.r.top > lab.r.top + 8 && ['BRAND','DESCRIPTION','PRODUCT TYPE','SHORT DESCRIPTION','MODEL NAME'].includes(o.text))
                    .sort((a,b) => a.r.top - b.r.top);
                const bottom = nextLabels.length ? nextLabels[0].r.top - 4 : lab.r.bottom + 230;

                const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio])')]
                    .filter(visible)
                    .filter(el => !el.disabled && !el.readOnly)
                    .map(el => ({
                        el,
                        placeholder: el.getAttribute('placeholder') || '',
                        value: el.value || '',
                        r: el.getBoundingClientRect()
                    }))
                    .filter(o => o.r.top >= lab.r.bottom - 12 && o.r.top < bottom)
                    .filter(o => o.r.width > 180 && o.r.height >= 25)
                    .sort((a,b) => {
                        const aph = (a.placeholder || '').toLowerCase().includes('category') ? 0 : 50;
                        const bph = (b.placeholder || '').toLowerCase().includes('category') ? 0 : 50;
                        return aph - bph || a.r.top - b.r.top || a.r.left - b.r.left;
                    });
                if (fields.length) {
                    const f = fields[0];
                    return {
                        ok:true,
                        labelTop: Math.round(lab.r.top),
                        placeholder:f.placeholder,
                        value:f.value,
                        x: Math.round(f.r.left + f.r.width / 2),
                        y: Math.round(f.r.top + f.r.height / 2),
                        left: f.r.left,
                        top: f.r.top,
                        right: f.r.right,
                        bottom: f.r.bottom,
                        width: f.r.width,
                        height: f.r.height
                    };
                }
            }
            return {ok:false, reason:'category-input-not-found-under-label'};
        }"""
    )



def _active_focus_is_brand_field(page):
    """Verify that focus has moved to the Brand searchable textbox after category Enter+Tab+Tab."""
    try:
        return page.evaluate(
            r"""() => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }
                function norm(s) { return (s || '').replace(/\*/g, '').replace(/\s+/g, ' ').trim().toUpperCase(); }
                const el = document.activeElement;
                if (!el || !visible(el)) return {ok:false, reason:'no-visible-active-element'};
                const tag = (el.tagName || '').toLowerCase();
                const placeholder = el.getAttribute('placeholder') || '';
                const value = el.value || '';
                const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                const role = el.getAttribute('role') || '';
                const r = el.getBoundingClientRect();

                // Strongest signal: actual brand input placeholder.
                if (/search\s+brand/i.test(placeholder)) {
                    return {ok:true, method:'active-placeholder-brand', tag, placeholder, value, text, role, x:Math.round(r.left), y:Math.round(r.top)};
                }

                // Fallback: active element is in the field area under BRAND label.
                const labels = [...document.querySelectorAll('label, div, span, p, strong')]
                    .filter(visible)
                    .map(node => ({node, text:norm(node.innerText || node.textContent), r:node.getBoundingClientRect()}))
                    .filter(o => o.text === 'BRAND')
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                for (const lab of labels) {
                    const inBrandZone = r.top >= lab.r.bottom - 20 && r.top <= lab.r.bottom + 180 && r.left >= lab.r.left - 30 && r.left <= lab.r.right + 700;
                    if (inBrandZone) {
                        return {ok:true, method:'active-under-brand-label', tag, placeholder, value, text, role, x:Math.round(r.left), y:Math.round(r.top)};
                    }
                }
                return {ok:false, reason:'focus-not-on-brand', tag, placeholder, value, text, role, x:Math.round(r.left), y:Math.round(r.top)};
            }"""
        )
    except Exception as e:
        return {"ok": False, "reason": f"brand-focus-check-error: {e}"}


def _type_category_and_press_enter(page, search_text: str):
    """Click CATEGORY textbox, type search text, press Enter once, then Tab twice.

    This mirrors the manual working sequence:
    CATEGORY textbox → type Bakery Ingredients → Enter once → Tab → Tab → focus should reach Brand.
    """
    search_text = str(search_text or '').strip()
    if not search_text:
        return {"ok": False, "reason": "empty-search-text"}

    box = _find_category_input_box(page)
    if not box or not box.get('ok'):
        return box or {"ok": False, "reason": "category-box-not-found"}

    # Put real cursor into the actual CATEGORY textbox only.
    page.mouse.click(float(box['x']), float(box['y']))
    page.wait_for_timeout(180)
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
    except Exception:
        pass
    page.wait_for_timeout(120)
    page.keyboard.insert_text(search_text)
    page.wait_for_timeout(900)

    # User-confirmed working behavior: one Enter, then Tab twice. No second Enter.
    page.keyboard.press("Enter")
    page.wait_for_timeout(350)
    page.keyboard.press("Tab")
    page.wait_for_timeout(250)
    page.keyboard.press("Tab")
    page.wait_for_timeout(500)

    after = _read_selected_category_text(page)
    focus_check = _active_focus_is_brand_field(page)
    return {
        "ok": bool(focus_check and focus_check.get("ok")),
        "method": "click-type-enter-tab-tab",
        "search_text": search_text,
        "input_before": box,
        "selected_after": after,
        "brand_focus_check": focus_check,
    }

def _read_selected_category_text(page) -> str:
    """Read current Category field value/text from the textbox under CATEGORY label."""
    try:
        res = page.evaluate(
            r"""() => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }
                function norm(s) { return (s || '').replace(/\*/g, '').replace(/\s+/g, ' ').trim().toUpperCase(); }
                const labels = [...document.querySelectorAll('label, div, span, p, strong')]
                    .filter(visible)
                    .map(el => ({el, text:norm(el.innerText || el.textContent), r:el.getBoundingClientRect()}))
                    .filter(o => o.text === 'CATEGORY')
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                for (const lab of labels) {
                    const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), button, [role="combobox"], [role="button"]')]
                        .filter(visible)
                        .map(el => ({el, value:el.value || '', text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(), ph:el.getAttribute('placeholder') || '', r:el.getBoundingClientRect()}))
                        .filter(o => o.r.top >= lab.r.bottom - 15 && o.r.top < lab.r.bottom + 190)
                        .filter(o => o.r.width > 180 && o.r.height >= 25)
                        .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                    if (fields.length) {
                        const f = fields[0];
                        return f.value || f.text || f.ph || '';
                    }
                }
                return '';
            }"""
        )
        return str(res or '').strip()
    except Exception:
        return ''


def _click_category_dropdown_result(page, option_text: str, search_text: str = "") -> bool:
    """Fallback click under the exact Category input area only."""
    search_text = str(search_text or option_text or '').strip()
    return _click_dropdown_option_under_input(page, ["search category or sub-category"], option_text, search_text=search_text)


def _click_dropdown_option_under_input(page, placeholder_needles, option_text: str, search_text: str = "") -> bool:
    """Click a dropdown option located under the specific searchable input.

    Restricting candidates to the rectangle below the Category input prevents the
    script from clicking old selected category text, helper text, or random page text.
    """
    needles = placeholder_needles if isinstance(placeholder_needles, list) else [placeholder_needles]
    try:
        clicked = page.evaluate(
            r"""({needles, optionText, searchText}) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }
                function norm(s) {
                    return (s || '')
                        .replace(/&/g, ' and ')
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, ' ')
                        .replace(/\s+/g, ' ')
                        .trim();
                }
                function clickEl(el) {
                    el.scrollIntoView({block:'center', inline:'nearest'});
                    const r = el.getBoundingClientRect();
                    const x = r.left + r.width / 2;
                    const y = r.top + r.height / 2;
                    try { el.click(); return true; } catch(e) {}
                    ['pointerdown','mousedown','mouseup','click'].forEach(type => {
                        el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y, view:window}));
                    });
                    return true;
                }
                function scoreText(t) {
                    const nt = norm(t);
                    const no = norm(optionText);
                    const ns = norm(searchText || optionText);
                    if (!nt) return -9999;
                    if (nt === no) return 1000;
                    if (ns && nt === ns) return 960;
                    if (ns && nt.startsWith(ns)) return 940;
                    if (ns && nt.includes(ns)) return 900;
                    const stoks = (ns || no).split(' ').filter(Boolean).filter(x => !['and','of','the','ingredients','ingredient'].includes(x));
                    const ntoks = new Set(nt.split(' ').filter(Boolean));
                    const hits = stoks.filter(x => ntoks.has(x)).length;
                    if (stoks.length && hits === stoks.length) return 840 + hits;
                    return -9999;
                }

                const ns = needles.map(n => String(n || '').toLowerCase()).filter(Boolean);
                const inputs = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio])')]
                    .filter(visible)
                    .map(el => ({el, ph:(el.getAttribute('placeholder') || '').toLowerCase(), r:el.getBoundingClientRect()}))
                    .filter(o => ns.some(n => o.ph.includes(n)))
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (!inputs.length) return {ok:false, reason:'input-not-found'};
                const ir = inputs[0].r;

                const candidates = [...document.querySelectorAll('[role="option"], button, [role="button"], li, div, span')]
                    .filter(visible)
                    .map(el => ({el, text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(), r:el.getBoundingClientRect(), tag:el.tagName, role:el.getAttribute('role') || ''}))
                    .filter(o => o.text && o.text.length <= 180)
                    // Only dropdown area below/overlapping the active category input.
                    .filter(o => o.r.top >= ir.bottom - 8 && o.r.top <= ir.bottom + 430)
                    .filter(o => o.r.right >= ir.left - 80 && o.r.left <= ir.right + 80)
                    .map(o => ({...o, score:scoreText(o.text)}))
                    .filter(o => o.score > -9999)
                    .sort((a,b) => b.score - a.score || (a.r.width*a.r.height) - (b.r.width*b.r.height));
                if (!candidates.length) return {ok:false, reason:'no-option-under-input', optionText, searchText, input:{top:Math.round(ir.top), bottom:Math.round(ir.bottom)}};
                clickEl(candidates[0].el);
                return {ok:true, clickedText:candidates[0].text, score:candidates[0].score, tag:candidates[0].tag, role:candidates[0].role};
            }""",
            {"needles": needles, "optionText": option_text or "", "searchText": search_text or ""},
        )
        if clicked and clicked.get("ok"):
            page.wait_for_timeout(450)
            log(f"  Dropdown option under input clicked: {clicked}")
            return True
        log(f"  Dropdown option under input failed: {clicked}")
    except Exception as e:
        log(f"  Dropdown option under input error: {e}")
    return False


def select_category_for_batch(page, category_option: str) -> bool:
    """Safely select Category by exact CATEGORY label: type, Enter once, Tab twice, verify Brand focus."""
    category_option = str(category_option or "").strip()
    if not category_option:
        return False
    search = category_search_text(category_option)
    log(f"Selecting CATEGORY with Enter+Tab+Tab: option='{category_option}' search='{search}'")

    click_tab(page, "Basics")
    page.wait_for_timeout(400)

    res = _type_category_and_press_enter(page, search)
    log(f"  Category type+enter+tab+tab result: {res}")
    if not res or not res.get("ok"):
        return False

    selected = _read_selected_category_text(page)
    log(f"  Category selected value after Enter+Tab: {selected}")

    # Strict success when the UI exposes the selected value.
    if selected and normalize_select_text(search) in normalize_select_text(selected):
        return True

    # If UI does not expose the selected value but the field action succeeded, continue.
    # The Save step will fail/report if category truly remained blank.
    log("  Category readable verification was unclear, but Enter+Tab+Tab reached the Brand field. Continuing.")
    return True

def select_brand_for_batch(page, brand_option: str) -> bool:
    """Select Brand only when an actual matching dropdown option exists.

    If the brand is blank or not found, the function clears the brand search box and
    continues safely without leaving invalid typed text in the Brand field.
    """
    if value_is_blank(brand_option):
        log("Brand blank in JSON. Leaving brand empty.")
        return True
    brand = str(brand_option).strip()
    log(f"Selecting BRAND safely: {brand}")
    click_tab(page, "Basics")
    page.wait_for_timeout(250)
    try:
        box = page.get_by_placeholder(re.compile(r"search\s+brand", re.I)).first
        if box.count() <= 0:
            log("  Brand search input not found.")
            return False
        box.scroll_into_view_if_needed(timeout=2500)
        box.click(force=True, timeout=2500)
        page.wait_for_timeout(150)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(brand)
        page.wait_for_timeout(1000)

        no_match = False
        try:
            no_match = bool(page.get_by_text(re.compile(r"no\s+matching\s+brand\s+found", re.I)).count() > 0)
        except Exception:
            no_match = False
        if no_match:
            log(f"  Brand not found. Clearing brand search and continuing without brand: {brand}")
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.press("Escape")
            return False

        clicked = _click_visible_option(page, brand, exact=True)
        if not clicked:
            clicked = _click_visible_option_flexible(page, brand, search_text=brand, exact=False)
        if not clicked:
            log(f"  Brand option not clicked. Clearing brand search and continuing without brand: {brand}")
            try:
                box.click(force=True, timeout=1200)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        page.wait_for_timeout(500)
        log(f"  Brand selected: {brand}")
        return True
    except Exception as e:
        log(f"  Brand selection error: {e}")
        return False


def prepare_basic_tab_for_update(page, wait_ms: int = 5000) -> bool:
    """Return to Basic, wait, scroll to the Publish/Update area, then wait again.

    This is intentionally used after the Media phase in batch mode. The Media page
    can keep upload state or overlays active for a few seconds; saving from Basic
    after a full wait avoids the update issue seen when saving directly from Media.
    """
    log("Preparing Basic tab for final Update Product save...")

    if not click_tab(page, "Basic"):
        if not click_tab(page, "Basics"):
            log("Could not return to Basic tab before Update Product.")
            return False

    page.wait_for_timeout(wait_ms)

    try:
        page.evaluate(
            """() => {
                window.scrollTo({top: document.documentElement.scrollHeight, left: 0, behavior: 'instant'});
                return {
                    scrollTop: window.scrollY,
                    documentHeight: document.documentElement.scrollHeight
                };
            }"""
        )
    except Exception as e:
        log(f"Could not scroll Basic tab to the bottom before saving: {e}")
        try:
            page.keyboard.press("End")
        except Exception:
            pass

    page.wait_for_timeout(wait_ms)
    log("Basic tab is ready at the bottom of the page for Update Product.")
    return True

def click_update_product(page) -> bool:
    log("Clicking Update Product...")
    patterns = [r"^\s*Update Product\s*$", r"Update\s+Product", r"^\s*Save\s*$", r"Save\s+Product"]
    for pat in patterns:
        try:
            btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
            if btn.count() > 0:
                btn.scroll_into_view_if_needed()
                btn.click(timeout=3500, force=True)
                # Do not add another five-second pause here. The required waits
                # already happen before Update Product is clicked.
                page.wait_for_timeout(1000)
                return True
        except Exception:
            pass
    try:
        clicked = page.evaluate(
            r"""() => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                const buttons = [...document.querySelectorAll('button, [role="button"]')]
                    .filter(visible)
                    .map(el => ({el, text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(), r:el.getBoundingClientRect()}))
                    .filter(o => /update product|save product|^save$/i.test(o.text))
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (!buttons.length) return false;
                buttons[0].el.scrollIntoView({block:'center', inline:'nearest'});
                buttons[0].el.click();
                return true;
            }"""
        )
        if clicked:
            # Give the click one second to dispatch, then continue to the next product.
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False



def wait_for_products_list_after_update(page, timeout_ms: int = 60000) -> bool:
    """Wait for a successful save redirect, otherwise force-open the products list.

    A successful Update Product action redirects the browser to:
    https://store.prockured.com/admin/products

    The function waits for up to one minute. If the redirect does not happen, it
    navigates to the products list itself so batch processing can continue safely.
    The return value is True only when the automatic redirect was confirmed.
    """
    target = ADMIN_PRODUCTS_URL.rstrip("/")
    target_pattern = re.compile(
        r"^https://store\.prockured\.com/admin/products/?(?:[?#].*)?$",
        re.I,
    )

    log(
        "Waiting up to 60 seconds for the saved product to redirect to "
        f"{target} ..."
    )

    try:
        # If the redirect already completed before this function starts,
        # wait_for_url returns immediately.
        page.wait_for_url(target_pattern, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        current = str(page.url or "").rstrip("/")
        if current == target or target_pattern.match(str(page.url or "")):
            log(f"Save redirect confirmed: {page.url}")
            return True
    except PlaywrightTimeoutError:
        log(
            "Save redirect was not confirmed within 60 seconds. "
            "Opening the Products list automatically."
        )
    except Exception as e:
        log(
            f"Error while waiting for the save redirect: {e}. "
            "Opening the Products list automatically."
        )

    # Timeout/error fallback requested for batch continuity.
    try:
        page.goto(ADMIN_PRODUCTS_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        log(f"Products list opened after save timeout: {page.url}")
    except Exception as e:
        log(f"Could not open Products list after save timeout: {e}")

    return False



def load_resume_report(path: Path | None) -> dict:
    """Read a previous success report and build skip indexes for resume mode."""
    index = {"row_id": set(), "sku": set(), "modelnumber": set(), "lookup_value": set(), "source": ""}
    if not path:
        return index

    path = Path(path)
    if path.is_dir():
        candidate = path / "success_report.csv"
        if candidate.exists():
            path = candidate
        else:
            csvs = sorted(path.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not csvs:
                raise FileNotFoundError(f"No CSV report found in resume folder: {path}")
            path = csvs[0]
    if not path.exists():
        raise FileNotFoundError(f"Resume report not found: {path}")

    success_filename = "success" in path.name.lower()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            status_text = " ".join(str(row.get(k) or "") for k in ("status", "message", "save_status")).lower()
            successful = success_filename or any(word in status_text for word in ("updated", "success", "confirmed", "saved"))
            if not successful:
                continue
            row_id = str(row.get("row_id") or "").strip()
            sku = normalize_sku(row.get("sku") or "")
            model = normalize_model_name(row.get("model_number") or row.get("model_name") or "")
            lookup_value = normalize_model_name(row.get("lookup_value") or "")
            if row_id:
                index["row_id"].add(row_id)
            if sku:
                index["sku"].add(sku)
            if model:
                index["modelnumber"].add(model)
            if lookup_value:
                index["lookup_value"].add(lookup_value)
    index["source"] = str(path)
    return index


def should_skip_from_resume(item: dict, lookup_type: str, lookup_value: str, resume_index: dict) -> bool:
    if not resume_index or not resume_index.get("source"):
        return False
    admin = item.get("admin") or {}
    row_id = str(admin.get("row_id") or "").strip()
    sku = normalize_sku(admin.get("sku") or "")
    model = normalize_model_name(batch_model_name(item))
    lookup_norm = normalize_model_name(lookup_value)
    return bool(
        (row_id and row_id in resume_index.get("row_id", set()))
        or (sku and sku in resume_index.get("sku", set()))
        or (model and model in resume_index.get("modelnumber", set()))
        or (lookup_norm and lookup_norm in resume_index.get("lookup_value", set()))
    )


def run_batch_json(
    page,
    batch_json_path: Path = DEFAULT_BATCH_JSON,
    run_options: BatchRunOptions | None = None,
):
    run_options = run_options or BatchRunOptions(sections=set(ALL_BATCH_SECTIONS))
    batch_json_path = Path(batch_json_path)
    products, batch_config = load_batch_package(batch_json_path)
    resume_index = load_resume_report(run_options.resume_report)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    report_dir = DEFAULT_BATCH_REPORT_DIR / ts
    report_dir.mkdir(parents=True, exist_ok=True)
    batch_logger = BatchLogger(report_dir / "batch_log.txt")

    success_rows, failed_rows, missing_rows, manual_rows, resume_rows = [], [], [], [], []

    batch_logger.write("========================================")
    batch_logger.write("Prockured Listing CLI V1 batch started")
    batch_logger.write(f"Batch JSON: {batch_json_path}")
    batch_logger.write(f"Products loaded: {len(products)}")
    batch_logger.write(f"Lookup mode: {run_options.lookup}")
    batch_logger.write(f"Selected sections: {', '.join(sorted(run_options.sections))}")
    batch_logger.write("Category is mandatory and will run for every product.")
    batch_logger.write(f"Default image root: {DEFAULT_IMAGE_ROOT}")
    if run_options.force_media_root:
        batch_logger.write(f"Forced media root: {run_options.force_media_root}")
    if run_options.media_sort:
        batch_logger.write(f"CLI MediaSort: {run_options.media_sort}")
    if resume_index.get("source"):
        batch_logger.write(f"Resume report: {resume_index['source']}")
    batch_logger.write(f"Report folder: {report_dir}")
    batch_logger.write("========================================")

    for idx, item in enumerate(products, start=1):
        admin = item.get("admin") or {}
        row_id = str(admin.get("row_id") or idx)
        sku = str(admin.get("sku") or "").strip()
        model_number = batch_model_name(item)
        lookup_type, lookup_value = batch_lookup_details(item, run_options.lookup)
        category_option = str(admin.get("category_option") or "").strip()
        brand_option = str(admin.get("brand_option") or "").strip()
        sections = fill_sections_for_item(item, run_options.sections)
        missing = batch_item_missing_fields(item, run_options.lookup)

        if should_skip_from_resume(item, lookup_type, lookup_value, resume_index):
            resume_rows.append({
                "row_id": row_id, "sku": sku, "model_number": model_number,
                "lookup_type": lookup_type, "lookup_value": lookup_value,
                "status": "skipped_from_resume", "resume_report": resume_index.get("source", ""),
            })
            batch_logger.write(f"[{idx}/{len(products)}] SKIPPED by resume: Row {row_id} | {lookup_type}={lookup_value}")
            continue

        if missing:
            missing_rows.append({
                "row_id": row_id, "sku": sku, "model_number": model_number,
                "lookup_type": lookup_type, "lookup_value": lookup_value,
                "missing_fields": " | ".join(missing),
            })

        batch_logger.write("")
        batch_logger.write(
            f"[{idx}/{len(products)}] Row {row_id} | "
            f"Lookup: {lookup_type}={lookup_value} | SKU: {sku} | Model Number: {model_number}"
        )

        if not lookup_value:
            reason = "Missing SKU" if lookup_type == "sku" else "Missing Model Number"
            failed_rows.append({
                "row_id": row_id, "sku": sku, "model_number": model_number,
                "lookup_type": lookup_type, "lookup_value": lookup_value,
                "reason": reason, "last_page_url": getattr(page, "url", ""), "details": "",
            })
            batch_logger.write(f"FAILED: {reason}")
            continue

        # Category is unconditional because Update Product cannot save without it.
        if not category_option:
            reason = "Missing category_option"
            failed_rows.append({
                "row_id": row_id, "sku": sku, "model_number": model_number,
                "lookup_type": lookup_type, "lookup_value": lookup_value,
                "reason": reason, "last_page_url": getattr(page, "url", ""),
                "details": "Category is mandatory for every saved batch operation.",
            })
            batch_logger.write(f"FAILED: {reason}")
            continue

        has_variant_attributes = bool(
            (item.get("variant_attributes") or {})
            or (item.get("variant_filter_attributes") or {})
        )
        run_variations = "variations" in sections and has_variant_attributes
        if run_options.explicit_variations and not has_variant_attributes:
            reason = "--variation requested but no variant_attributes or variant_filter_attributes were supplied"
            failed_rows.append({
                "row_id": row_id, "sku": sku, "model_number": model_number,
                "lookup_type": lookup_type, "lookup_value": lookup_value,
                "reason": reason, "last_page_url": getattr(page, "url", ""), "details": "",
            })
            batch_logger.write(f"FAILED: {reason}")
            continue

        try:
            open_products_list(page)
            batch_logger.write("Products list opened.")
            if not fill_admin_search(page, lookup_value):
                raise RuntimeError(f"Product search failed for {lookup_type}: {lookup_value}")
            batch_logger.write(f"Searched products by {lookup_type}: {lookup_value}")

            if lookup_type == "modelnumber":
                product_opened = click_product_result_by_model_in_sku(page, lookup_value)
            else:
                product_opened = click_first_product_result(page, lookup_value)
            if not product_opened:
                raise RuntimeError(f"{lookup_type} not found or matching product row could not be opened")
            batch_logger.write(f"Product opened: {page.url}")

            lookup_ok, actual_lookup = verify_batch_lookup_on_edit_page(page, lookup_type, lookup_value)
            batch_logger.write(
                f"Lookup verification: type={lookup_type} | expected={lookup_value} | "
                f"actual={actual_lookup} | match={lookup_ok}"
            )
            if not lookup_ok:
                raise RuntimeError(f"{lookup_type} mismatch on edit page: {actual_lookup}")

            data = product_data_from_batch_item(
                item,
                update_product_name=("basics" in sections),
                batch_config=batch_config,
                run_options=run_options,
                force_variable=run_variations,
            )

            if "basics" in sections:
                fill_basics(page, data)
                batch_logger.write("Basics filled.")
            elif run_variations:
                click_tab(page, "Basics")
                fill_product_type_dropdown(page, "Variable Product")
                page.wait_for_timeout(700)
                batch_logger.write("Product Type set to Variable Product for --variation workflow.")

            # Mandatory category phase for every save.
            click_tab(page, "Basic")
            page.wait_for_timeout(500)
            if not select_category_for_batch(page, category_option):
                raise RuntimeError("Category option was not selected")
            batch_logger.write(f"Mandatory Category selected: {category_option}")

            if "brand" in sections:
                if brand_option:
                    if not select_brand_for_batch(page, brand_option):
                        manual_rows.append({
                            "row_id": row_id, "sku": sku, "model_number": model_number,
                            "lookup_type": lookup_type, "lookup_value": lookup_value,
                            "issue": "Brand option not found",
                            "suggested_action": f"Check/select brand manually: {brand_option}",
                        })
                        batch_logger.write(f"WARNING: Brand not selected: {brand_option}")
                    else:
                        batch_logger.write(f"Brand selected: {brand_option}")
                else:
                    batch_logger.write("Brand blank; continuing without brand.")

            attributes_ok = True
            if "attributes" in sections:
                attributes_ok = fill_attributes(page, data, one=False, clear=True)
                batch_logger.write(f"Attributes filled status: {attributes_ok}")
                if not attributes_ok:
                    raise RuntimeError("Attribute fill incomplete")

            if run_variations:
                if not attributes_ok:
                    raise RuntimeError("Variations cannot run because attributes did not complete")
                fill_variations(page, data)
                batch_logger.write("Variations generated/priced after variant checkbox setup.")
            elif "variations" in sections:
                batch_logger.write("Variations skipped: no variant attributes in this product.")

            if "seo" in sections:
                fill_seo(page, data)
                batch_logger.write("SEO filled.")

            if "pricing" in sections and not is_variable_product(data):
                fill_pricing(page, data)
                batch_logger.write("Simple-product Pricing filled.")

            if "media" in sections:
                media_ok = fill_media(page, data)
                batch_logger.write(f"Media submission status: {media_ok}")
                if not media_ok:
                    manual_rows.append({
                        "row_id": row_id, "sku": sku, "model_number": model_number,
                        "lookup_type": lookup_type, "lookup_value": lookup_value,
                        "issue": "Media not uploaded",
                        "suggested_action": "Review media root, MediaSort and score threshold.",
                    })

            # Exact save sequence retained from the working version.
            click_tab(page, "Basic")
            page.wait_for_timeout(5000)
            page.evaluate("() => { window.scrollTo({top: document.body.scrollHeight, left: 0, behavior: 'instant'}); }")
            page.wait_for_timeout(5000)

            if not click_update_product(page):
                raise RuntimeError("Update Product button not found or click failed")

            save_redirected = wait_for_products_list_after_update(page, timeout_ms=60000)
            batch_logger.write(f"Automatic save redirect confirmed: {save_redirected} | current_url={page.url}")
            if not save_redirected:
                reason = "Save redirect not confirmed within 60 seconds"
                failed_rows.append({
                    "row_id": row_id, "sku": sku, "model_number": model_number,
                    "lookup_type": lookup_type, "lookup_value": lookup_value,
                    "reason": reason, "last_page_url": page.url,
                    "details": "Products list was opened automatically so the batch could continue.",
                })
                manual_rows.append({
                    "row_id": row_id, "sku": sku, "model_number": model_number,
                    "lookup_type": lookup_type, "lookup_value": lookup_value,
                    "issue": "Product save could not be confirmed",
                    "suggested_action": "Check whether the changes were saved.",
                })
                batch_logger.write(f"FAILED: {reason}")
                continue

            success_rows.append({
                "row_id": row_id, "sku": sku, "model_number": model_number,
                "lookup_type": lookup_type, "lookup_value": lookup_value,
                "category_option": category_option, "brand_option": brand_option,
                "sections": ", ".join(sorted(sections)),
                "status": "updated",
                "message": "Product updated and redirect to Products list confirmed.",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            batch_logger.write("SUCCESS: Product updated and redirect confirmed.")

        except Exception as e:
            details = traceback.format_exc()
            failed_rows.append({
                "row_id": row_id, "sku": sku, "model_number": model_number,
                "lookup_type": lookup_type, "lookup_value": lookup_value,
                "reason": str(e), "last_page_url": getattr(page, "url", ""),
                "details": details[-1500:],
            })
            batch_logger.write(f"FAILED unexpected error: {e}")
            batch_logger.write(details)
            continue

    common = ["row_id", "sku", "model_number", "lookup_type", "lookup_value"]
    write_csv_report(report_dir / "success_report.csv", success_rows, common + [
        "category_option", "brand_option", "sections", "status", "message", "updated_at"
    ])
    write_csv_report(report_dir / "failed_report.csv", failed_rows, common + ["reason", "last_page_url", "details"])
    write_csv_report(report_dir / "missing_data_report.csv", missing_rows, common + ["missing_fields"])
    write_csv_report(report_dir / "manual_review_report.csv", manual_rows, common + ["issue", "suggested_action"])
    write_csv_report(report_dir / "resume_skipped_report.csv", resume_rows, common + ["status", "resume_report"])

    batch_logger.write("")
    batch_logger.write("========================================")
    batch_logger.write("Batch completed")
    batch_logger.write(f"Success: {len(success_rows)}")
    batch_logger.write(f"Failed: {len(failed_rows)}")
    batch_logger.write(f"Resume-skipped: {len(resume_rows)}")
    batch_logger.write(f"Missing-data rows: {len(missing_rows)}")
    batch_logger.write(f"Manual-review rows: {len(manual_rows)}")
    batch_logger.write(f"Reports: {report_dir}")
    batch_logger.write("========================================")
    return report_dir

def run_command(cmd, page):
    global stop_requested
    try:
        if cmd == "load":
            load_from_clipboard()
        elif cmd == "basics":
            stop_requested = False
            fill_basics(page, require_data())
        elif cmd == "attributes":
            stop_requested = False
            fill_attributes(page, require_data(), one=False, clear=True)
        elif cmd == "one":
            stop_requested = False
            fill_attributes(page, require_data(), one=True, clear=False)
        elif cmd == "seo":
            stop_requested = False
            fill_seo(page, require_data())
        elif cmd == "variants":
            stop_requested = False
            fill_variations(page, require_data())
        elif cmd == "media":
            stop_requested = False
            fill_media(page, require_data())
        elif cmd == "alttext":
            stop_requested = False
            update_media_alt_texts(page, require_data())
        elif cmd == "pricing":
            stop_requested = False
            fill_pricing(page, require_data())
        elif cmd == "batch":
            stop_requested = False
            run_batch_json(page, DEFAULT_BATCH_JSON, BatchRunOptions(sections=set(ALL_BATCH_SECTIONS), lookup="sku"))
        elif cmd == "full":
            stop_requested = False
            data = require_data()
            fill_basics(page, data)
            if stop_requested: return
            attributes_ok = fill_attributes(page, data, one=False, clear=True)
            if stop_requested: return
            if not attributes_ok:
                log("Full fill stopped because Attributes did not complete. Variations were NOT generated.")
                return
            if is_variable_product(data):
                fill_variations(page, data)
                if stop_requested: return
            fill_seo(page, data)
            if stop_requested: return
            fill_media(page, data)
            if stop_requested: return
            if not is_variable_product(data):
                fill_pricing(page, data)
            log("Full fill done. Please review manually before saving.")
        elif cmd == "debug":
            debug_current_tab(page)
        elif cmd == "stop":
            stop_requested = True
            log("Stop requested. Script remains running.")
        elif cmd == "quit":
            log("Closing script.")
            os._exit(0)
    except Exception as e:
        log("\nERROR while running command:")
        log(str(e))
        traceback.print_exc()


def on_hotkey(cmd):
    command_queue.put(cmd)


def start_hotkeys():
    if keyboard is None:
        raise RuntimeError("pynput is required for interactive hotkey mode. Install it with: pip install pynput")
    hotkeys = keyboard.GlobalHotKeys({
        "<alt>+<shift>+l": lambda: on_hotkey("load"),
        "<alt>+<shift>+b": lambda: on_hotkey("basics"),
        "<alt>+<shift>+a": lambda: on_hotkey("attributes"),
        "<alt>+<shift>+1": lambda: on_hotkey("one"),
        "<alt>+<shift>+s": lambda: on_hotkey("seo"),
        "<alt>+<shift>+v": lambda: on_hotkey("variants"),
        "<alt>+<shift>+m": lambda: on_hotkey("media"),
        "<alt>+<shift>+i": lambda: on_hotkey("alttext"),
        "<alt>+<shift>+r": lambda: on_hotkey("pricing"),
        "<alt>+<shift>+j": lambda: on_hotkey("batch"),
        "<alt>+<shift>+f": lambda: on_hotkey("full"),
        "<alt>+<shift>+d": lambda: on_hotkey("debug"),
        "<alt>+<shift>+x": lambda: on_hotkey("stop"),
        "<alt>+<shift>+q": lambda: on_hotkey("quit"),
    })
    hotkeys.start()
    return hotkeys



def resolve_cli_sections(args) -> tuple[set, bool]:
    explicit_flags = {
        "basics": bool(args.basics),
        "brand": bool(args.brand),
        "attributes": bool(args.attributes),
        "variations": bool(args.variations),
        "pricing": bool(args.pricing),
        "seo": bool(args.seo),
        "media": bool(args.media),
    }
    any_explicit = any(explicit_flags.values()) or bool(args.all)
    if not any_explicit:
        sections = set(ALL_BATCH_SECTIONS)
    elif args.all:
        sections = set(ALL_BATCH_SECTIONS)
    else:
        sections = {name for name, enabled in explicit_flags.items() if enabled}

    # Category is compulsory for all saved operations.
    sections.add("category")
    # Variation requires the variant attributes to be created first.
    if args.variations:
        sections.add("attributes")
        sections.add("variations")
    return sections, bool(args.variations)


def print_cli_and_hotkey_banner(args=None, run_options: BatchRunOptions | None = None):
    log("============================================================")
    log("Prockured Independent Listing Bot - CLI V1.0.1")
    log("Made By Krishna Maheshwari")
    log("============================================================")
    log("BATCH CLI")
    log("  No section flags = run all sections. Category always runs.")
    log("  Default lookup = exact SKU. Model lookup only with --lookup modelnumber.")
    log("  --variation first sets Variable Product, recreates attributes, checks")
    log("    Variants/Filters, then generates and prices the variations.")
    log("")
    log("Examples:")
    log("  python independent_listing_bot_cli_v1.py --batch products.json")
    log("  python independent_listing_bot_cli_v1.py --batch products.json --media")
    log("  python independent_listing_bot_cli_v1.py --batch products.json --attributes --seo")
    log("  python independent_listing_bot_cli_v1.py --batch products.json --variation")
    log("  python independent_listing_bot_cli_v1.py --batch products.json --lookup modelnumber")
    log("  python independent_listing_bot_cli_v1.py --batch products.json --media --forceMedia \"D:\\Product Images\" --MediaSort modelnumber")
    log("  python independent_listing_bot_cli_v1.py --batch products.json --resume batch_reports\\LAST_RUN\\success_report.csv")
    log("")
    log("MediaSort values: modelnumber | sku | name | brand/modelnumber | brand/sku | brand/name")
    log("Default media folder: <current run folder>\\images")
    log("JSON attribute sections: attributes | filter_attributes | variant_attributes | variant_filter_attributes")
    log("JSON media config examples:")
    log('  "config": {"media": {"root": "images", "sort": "modelnumber", "min_score": 85}}')
    log('  Product override: "media": {"folder": "special-folder", "sort": "sku"}')
    log("============================================================")
    log("INTERACTIVE HOTKEYS")
    log("Alt + Shift + L  = Load clipboard data")
    log("Alt + Shift + B  = Fill Basics")
    log("Alt + Shift + A  = Fill Attributes")
    log("Alt + Shift + 1  = Test one Attribute")
    log("Alt + Shift + V  = Generate/Fix Variations")
    log("Alt + Shift + S  = Fill SEO")
    log("Alt + Shift + M  = Upload Product Media Images")
    log("Alt + Shift + I  = Update Image Alt Text")
    log("Alt + Shift + R  = Fill Pricing")
    log("Alt + Shift + J  = Run default Batch JSON (full, SKU lookup)")
    log("Alt + Shift + F  = Full Clipboard Fill")
    log("Alt + Shift + D  = Debug Current Tab")
    log("Alt + Shift + X  = Stop Current Action")
    log("Alt + Shift + Q  = Quit")
    log("============================================================")
    log(f"Run folder: {RUN_DIR}")
    log(f"Default Batch JSON: {DEFAULT_BATCH_JSON}")
    log(f"Default Images folder: {DEFAULT_IMAGE_ROOT}")
    log(f"Reports folder: {DEFAULT_BATCH_REPORT_DIR}")
    if run_options:
        log(f"Current lookup: {run_options.lookup}")
        log(f"Current sections: {', '.join(sorted(run_options.sections))}")
        if run_options.force_media_root:
            log(f"Forced Media root: {run_options.force_media_root}")
        if run_options.media_sort:
            log(f"Current MediaSort: {run_options.media_sort}")
        if run_options.resume_report:
            log(f"Resume report: {run_options.resume_report}")
    log("============================================================")


def main():
    parser = argparse.ArgumentParser(
        description="Prockured Independent Listing Bot CLI V1",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Rules:\n"
            "  * Category always runs because products cannot be saved without it.\n"
            "  * No section flags means run all sections.\n"
            "  * SKU is the default lookup. Use --lookup modelnumber explicitly.\n"
            "  * --variation implies attribute creation and Variable Product selection.\n"
        ),
    )
    parser.add_argument("--batch", type=str, default="", help="Batch JSON file to process immediately.")
    parser.add_argument("--all", action="store_true", help="Run all sections (same as supplying no section flags).")
    parser.add_argument("--basics", action="store_true", help="Update Basics fields.")
    parser.add_argument("--brand", action="store_true", help="Select Brand. Category still runs automatically.")
    parser.add_argument("--attributes", action="store_true", help="Process all four attribute sections.")
    parser.add_argument("--variation", "--variations", dest="variations", action="store_true", help="Set Variable Product, create classified attributes, then generate/fill variations.")
    parser.add_argument("--pricing", action="store_true", help="Update simple-product pricing.")
    parser.add_argument("--seo", action="store_true", help="Update SEO fields.")
    parser.add_argument("--media", action="store_true", help="Match and upload Product Media images.")
    parser.add_argument("--lookup", choices=["sku", "modelnumber"], default="sku", help="Product lookup mode. Default: sku")
    parser.add_argument("--forceMedia", "--force-media", dest="force_media", default="", help="Override media root directory.")
    parser.add_argument("--MediaSort", "--media-sort", dest="media_sort", default="", help="Media matching property/hierarchy, e.g. modelnumber or brand/modelnumber.")
    parser.add_argument("--mediaMinScore", "--media-min-score", dest="media_min_score", type=int, default=None, help="Minimum safe scored media match. Default: 85")
    parser.add_argument("--resume", type=str, default="", help="Previous success_report.csv (or report folder) whose completed products should be skipped.")
    parser.add_argument("--version", action="version", version="Prockured Listing Bot CLI V1.0.1")
    args = parser.parse_args()

    sections, explicit_variations = resolve_cli_sections(args)
    force_media_root = _resolve_path_from_run_dir(args.force_media) if args.force_media else None
    run_options = BatchRunOptions(
        sections=sections,
        lookup=args.lookup,
        force_media_root=force_media_root,
        media_sort=args.media_sort or None,
        media_min_score=args.media_min_score,
        resume_report=Path(args.resume).resolve() if args.resume else None,
        explicit_variations=explicit_variations,
    )

    print_cli_and_hotkey_banner(args, run_options)

    batch_path = None
    if args.batch:
        batch_path = _resolve_path_from_run_dir(args.batch)
        log(f"CLI batch command accepted: {batch_path}")
        if not batch_path.exists():
            log(f"ERROR: Batch JSON does not exist: {batch_path}")
            log("Check the filename and run the command again.")
            raise SystemExit(2)
        if not batch_path.is_file():
            log(f"ERROR: Batch path is not a file: {batch_path}")
            raise SystemExit(2)
        try:
            # Validate the package before waiting for the browser connection.
            preview_products, _preview_config = load_batch_package(batch_path)
            log(f"Batch JSON validated. Products found: {len(preview_products)}")
        except Exception as e:
            log(f"ERROR: Batch JSON could not be loaded: {e}")
            raise SystemExit(2)

    log("Starting Playwright...")
    try:
        with sync_playwright() as pw:
            browser = None
            page = None
            browser, page = connect_page(pw, browser)
            log(f"Connected to: {page.url}")

            if batch_path is not None:
                log("Starting batch processing now...")
                report_dir = run_batch_json(page, batch_path, run_options)
                log(f"Batch run finished. Reports: {report_dir}")
                return

            # Hotkeys are only started in interactive mode, not during immediate CLI batches.
            hotkeys = start_hotkeys()
            log("Interactive script is running. Keep this terminal open.")
            while True:
                cmd = command_queue.get()
                try:
                    browser, page = connect_page(pw, browser)
                    log(f"Using page: {page.url}")
                except Exception as e:
                    log(f"Could not reconnect to Prockured page: {e}")
                    continue
                run_command(cmd, page)
    except KeyboardInterrupt:
        log("Stopped by user.")
    except Exception as e:
        log("FATAL ERROR: The CLI could not start the batch.")
        log(str(e))
        log("")
        log("Browser connection check for Windows CMD:")
        log("  curl http://127.0.0.1:9222/json/version")
        log("")
        log("If that command cannot connect, launch Brave with:")
        log('  "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe" --remote-debugging-port=9222 --user-data-dir="C:\\brave-automation-profile"')
        raise SystemExit(2)

if __name__ == "__main__":
    main()
