import os
import re
import sys
import time
import queue
import traceback
import csv
import math
import hashlib
from pathlib import Path
from dataclasses import dataclass, field

import pyperclip
from pynput import keyboard
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

CDP_URL = "http://127.0.0.1:9222"

# Local output from your scraper. Keep the script in C:\Users\krish\dsa and it will find this automatically.
# Absolute scraper output path on your Windows machine. You can still override with environment variables if needed.
DEFAULT_OUTPUT_DIR = Path(os.environ.get("PROCKURED_OUTPUT_DIR", r"C:\Users\krish\dsa\prockured_output"))
DEFAULT_IMAGE_ROOT = Path(os.environ.get("PROCKURED_IMAGE_ROOT", r"C:\Users\krish\dsa\prockured_output\images"))
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_MEDIA_UPLOADS = int(os.environ.get("PROCKURED_MAX_MEDIA_UPLOADS", "8"))
BASE_MARKUP_MIN = 10
BASE_MARKUP_MAX = 30

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
    variant_attributes: list = field(default_factory=list)
    variant_pricing: list = field(default_factory=list)
    seo: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)


BASICS_KEYS = [
    "Product Type",
    "Brand",
    "Product Name",
    "Slug",
    "Product Tags",
    "Description",
    "Short Description",
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
    return "variable" in pt or bool(data.variant_attributes) or bool(data.variant_pricing)

def parse_clipboard_text(text: str) -> ProductData:
    data = ProductData()
    sections = {"BASICS": [], "ATTRIBUTES": [], "VARIANT ATTRIBUTES": [], "VARIANT PRICING": [], "SEO": [], "MEDIA": [], "PRICING": []}
    current = None

    for raw in text.splitlines():
        s = raw.strip()
        upper = s.upper()
        if upper in ["[BASICS]", "BASICS"]:
            current = "BASICS"
            continue
        if upper in ["[ATTRIBUTES]", "ATTRIBUTES"]:
            current = "ATTRIBUTES"
            continue
        if upper in ["[VARIANT ATTRIBUTES]", "VARIANT ATTRIBUTES"]:
            current = "VARIANT ATTRIBUTES"
            continue
        if upper in ["[VARIANT PRICING]", "VARIANT PRICING"]:
            current = "VARIANT PRICING"
            continue
        if upper in ["[SEO]", "SEO"]:
            current = "SEO"
            continue
        if upper in ["[MEDIA]", "MEDIA"]:
            current = "MEDIA"
            continue
        if upper in ["[PRICING]", "PRICING"]:
            current = "PRICING"
            continue
        if current:
            sections[current].append(raw)

    data.basics = parse_key_value_section(sections["BASICS"], BASICS_KEYS)
    data.attributes = parse_attributes(sections["ATTRIBUTES"])
    data.variant_attributes = parse_attributes(sections["VARIANT ATTRIBUTES"])
    data.variant_pricing = parse_variant_pricing(sections["VARIANT PRICING"])
    data.seo = parse_key_value_section(sections["SEO"], SEO_KEYS)
    data.media = parse_key_value_section(sections["MEDIA"], MEDIA_KEYS)
    data.pricing = parse_key_value_section(sections["PRICING"], PRICING_KEYS)

    # Slug must stay empty so Prockured can auto-generate it.

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
    """Return a fresh live page. Reconnects if the old CDP target became stale."""
    browsers_to_try = []
    if old_browser is not None:
        browsers_to_try.append(old_browser)
    for browser in browsers_to_try:
        try:
            page = find_product_page(browser)
            if page_is_alive(page):
                return browser, page
        except Exception:
            pass
    browser = pw.chromium.connect_over_cdp(CDP_URL)
    page = find_product_page(browser)
    return browser, page


def click_tab(page, tab_name):
    log(f"Opening {tab_name} tab...")
    # Try exact text first. Works with Prockured tab bar.
    selectors = [
        f"text=/{tab_name}/i",
        f"button:has-text('{tab_name}')",
        f"a:has-text('{tab_name}')",
        f"[role='tab']:has-text('{tab_name}')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(force=True)
                time.sleep(0.6)
                return True
        except Exception:
            continue
    # JS fallback: click the visible element whose text equals tab name.
    ok = page.evaluate(
        """(tabName) => {
            const wanted = tabName.trim().toLowerCase();
            function visible(el){ const r=el.getBoundingClientRect(); return r.width>0 && r.height>0; }
            const els = [...document.querySelectorAll('button,a,div,span')].filter(visible);
            const el = els.find(e => (e.innerText||e.textContent||'').trim().toLowerCase() === wanted);
            if (el) { el.click(); return true; }
            return false;
        }""",
        tab_name,
    )
    time.sleep(0.6)
    return ok


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
    """Return first visible empty attribute Name input and its bounding box."""
    selectors = [
        "input[placeholder*='Name']",
        "input[placeholder*='Material']",
        "input[aria-label*='Name']",
    ]

    candidates = []
    seen_boxes = set()

    for selector in selectors:
        loc = page.locator(selector)
        count = loc.count()
        for i in range(count):
            item = loc.nth(i)
            try:
                if not item.is_visible():
                    continue
                value = item.input_value(timeout=800).strip()
                if value:
                    continue
                box = item.bounding_box(timeout=800)
                if not box:
                    continue
                key = (round(box["x"]), round(box["y"]), round(box["width"]), round(box["height"]))
                if key in seen_boxes:
                    continue
                seen_boxes.add(key)
                candidates.append((box["y"], box["x"], item, box))
            except Exception:
                continue

    if not candidates:
        return None, None

    # Work from the first empty Name field currently visible.
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2], candidates[0][3]


def find_value_input_for_name(page, name_box):
    """Find the value input belonging to the same attribute block as the given name input.

    v7 fix:
    - Recomputed coordinates are used after scroll.
    - Search is wider because Prockured can shift blocks while dropdowns open/close.
    - Falls back to the nearest visible value field below the current Name field.
    """
    selectors = [
        "input[placeholder*='Add value']",
        "input[placeholder*='press Enter']",
        "input[aria-label*='value']",
    ]

    all_candidates = []
    strict_candidates = []
    seen_boxes = set()

    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = loc.count()
        except Exception:
            count = 0

        for i in range(count):
            item = loc.nth(i)
            try:
                if not item.is_visible():
                    continue
                box = item.bounding_box(timeout=1200)
                if not box:
                    continue

                key = (round(box["x"]), round(box["y"]), round(box["width"]), round(box["height"]))
                if key in seen_boxes:
                    continue
                seen_boxes.add(key)

                dy = box["y"] - name_box["y"]
                dx = abs(box["x"] - name_box["x"])

                # Keep all visible value fields for fallback debugging/search.
                all_candidates.append((abs(dy), dy, dx, item, box))

                # Normal case: value field is below the name field inside the same block.
                # Wider than v6 because scroll/dropdown can shift the layout.
                if 15 <= dy <= 420 and dx <= 650:
                    strict_candidates.append((dy, dx, item, box))
            except Exception:
                continue

    if strict_candidates:
        strict_candidates.sort(key=lambda x: (x[0], x[1]))
        return strict_candidates[0][2], strict_candidates[0][3]

    # Fallback: nearest visible value input below the Name field.
    below = [c for c in all_candidates if c[1] > 0]
    if below:
        below.sort(key=lambda x: (x[1], x[2]))
        return below[0][3], below[0][4]

    # Last fallback: nearest value input anywhere.
    if all_candidates:
        all_candidates.sort(key=lambda x: (x[0], x[2]))
        return all_candidates[0][3], all_candidates[0][4]

    return None, None

def click_first_attribute_delete_button(page) -> bool:
    """Delete one attribute block from the Attributes tab, if one exists."""
    js = r"""
    () => {
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const st = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
        };
        const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();

        const nameInputs = Array.from(document.querySelectorAll('input'))
            .filter(el => visible(el))
            .filter(el => {
                const ph = norm(el.getAttribute('placeholder'));
                return ph.includes('name') || ph.includes('material');
            });

        for (const input of nameInputs) {
            let node = input.parentElement;
            for (let depth = 0; node && depth < 9; depth++, node = node.parentElement) {
                const valueInputs = Array.from(node.querySelectorAll('input'))
                    .filter(el => visible(el))
                    .filter(el => {
                        const ph = norm(el.getAttribute('placeholder'));
                        return ph.includes('add value') || ph.includes('press enter');
                    });
                if (!valueInputs.length) continue;

                const buttons = Array.from(node.querySelectorAll('button')).filter(visible);
                const deleteButtons = buttons.filter(btn => {
                    const text = norm(btn.innerText || btn.textContent);
                    const aria = norm(btn.getAttribute('aria-label'));
                    const title = norm(btn.getAttribute('title'));
                    const r = btn.getBoundingClientRect();
                    const html = norm(btn.innerHTML || '');
                    const hasSvg = !!btn.querySelector('svg');
                    return aria.includes('delete') || title.includes('delete') || text.includes('delete') || html.includes('trash') || (hasSvg && r.width <= 90 && r.height <= 90);
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
    try:
        return bool(page.evaluate(js))
    except Exception:
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
        page.wait_for_timeout(250)
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



def set_variants_checkbox_for_attribute(page, name_box) -> bool:
    """Tick the Variants checkbox inside the current attribute block.

    v15 fix:
    Prockured's checkbox is a real checkbox with accessible name "Variants".
    The older code sometimes clicked the surrounding button/card instead of the
    checkbox. This version finds the current attribute block from the Name input
    coordinates, then clicks the actual Variants checkbox or its label in that block.
    """
    try:
        res = page.evaluate(
            r"""(nameBox) => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
                }
                function norm(s) { return (s || '').replace(/\s+/g,' ').trim().toLowerCase(); }
                function rect(el) { const r = el.getBoundingClientRect(); return {x:r.x, y:r.y, width:r.width, height:r.height, left:r.left, top:r.top, right:r.right, bottom:r.bottom}; }
                function near(a, b, tol=8) { return Math.abs(a-b) <= tol; }
                function clickElement(el) {
                    el.scrollIntoView({block:'center', inline:'nearest'});
                    try { el.click(); return true; } catch(e) {}
                    const r = el.getBoundingClientRect();
                    const x = r.left + r.width / 2;
                    const y = r.top + r.height / 2;
                    for (const type of ['pointerdown','mousedown','mouseup','click']) {
                        el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                    }
                    return true;
                }

                // Find the specific name input using the coordinates passed from Playwright.
                const inputs = [...document.querySelectorAll('input')].filter(visible);
                const nameInput = inputs
                    .map(el => ({el, r: rect(el), ph: norm(el.getAttribute('placeholder'))}))
                    .filter(o => (o.ph.includes('name') || o.ph.includes('material')))
                    .filter(o => near(o.r.top, nameBox.y, 12) || near(o.r.y, nameBox.y, 12))
                    .sort((a,b) => Math.abs(a.r.left - nameBox.x) - Math.abs(b.r.left - nameBox.x))[0]?.el ||
                    inputs.map(el => ({el, r: rect(el)}))
                        .filter(o => Math.abs(o.r.top - nameBox.y) < 30 && Math.abs(o.r.left - nameBox.x) < 80)[0]?.el;

                if (!nameInput) return {ok:false, reason:'name-input-not-found-near-box', nameBox};

                // Find the smallest ancestor block that contains value input + Variants text.
                let block = null;
                let node = nameInput.parentElement;
                for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
                    const r = node.getBoundingClientRect();
                    const txt = norm(node.innerText || node.textContent);
                    const hasValueInput = [...node.querySelectorAll('input')].some(inp => {
                        const ph = norm(inp.getAttribute('placeholder'));
                        return visible(inp) && (ph.includes('add value') || ph.includes('press enter'));
                    });
                    const hasVariantsText = txt.includes('variants');
                    if (hasValueInput && hasVariantsText && r.width > 250 && r.height > 100 && r.height < 900) {
                        block = node;
                        break;
                    }
                }
                if (!block) return {ok:false, reason:'attribute-block-not-found'};

                const blockRect = block.getBoundingClientRect();

                // Prefer the real checkbox whose nearby label says Variants.
                const checkboxes = [...block.querySelectorAll('input[type="checkbox"]')]
                    .filter(visible)
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        const nearbyTextEls = [...block.querySelectorAll('label, span, div, button')]
                            .filter(visible)
                            .map(t => ({el:t, text:norm(t.innerText || t.textContent), r:t.getBoundingClientRect()}))
                            .filter(t => t.text.includes('variants'))
                            .filter(t => Math.abs((t.r.top + t.r.height/2) - (r.top + r.height/2)) < 35 || (t.r.top >= r.top - 18 && t.r.top <= r.bottom + 18))
                            .filter(t => t.r.left >= r.left - 25 && t.r.left <= r.right + 260);
                        const parentText = norm(el.closest('label, button, div')?.innerText || el.parentElement?.innerText || '');
                        const score = (nearbyTextEls.length ? 0 : 1000) + Math.abs(r.top - nameBox.y) + Math.max(0, r.left - 220);
                        return {el, r, parentText, nearby: nearbyTextEls.map(x => x.text).join(' | '), score};
                    })
                    .filter(o => o.parentText.includes('variants') || o.nearby.includes('variants'))
                    .sort((a,b) => a.score - b.score);

                let target = checkboxes[0]?.el || null;
                let targetKind = target ? 'real-checkbox' : '';

                // Fallback: click a button/label that says Variants inside this same block.
                if (!target) {
                    const controls = [...block.querySelectorAll('button, label, div[role="checkbox"], div, span')]
                        .filter(visible)
                        .map(el => ({el, text:norm(el.innerText || el.textContent), r:el.getBoundingClientRect()}))
                        .filter(o => o.text === 'variants' || o.text.includes('variants'))
                        .filter(o => o.r.top >= blockRect.top && o.r.top <= blockRect.bottom)
                        .sort((a,b) => (a.r.width*a.r.height) - (b.r.width*b.r.height));
                    if (controls.length) {
                        target = controls[0].el;
                        targetKind = 'variants-label-button';
                    }
                }

                if (!target) return {ok:false, reason:'variants-control-not-found-in-block'};

                const beforeChecked = target.matches('input[type="checkbox"]') ? target.checked :
                    !!(target.querySelector('input[type="checkbox"]')?.checked) ||
                    (target.getAttribute('aria-checked') || '').toLowerCase() === 'true' ||
                    (target.getAttribute('aria-pressed') || '').toLowerCase() === 'true' ||
                    norm(target.className || '').includes('checked') || norm(target.className || '').includes('active');

                if (!beforeChecked) {
                    // If target is not the input but contains one, click the input directly.
                    const innerCheckbox = target.matches('input[type="checkbox"]') ? target : target.querySelector('input[type="checkbox"]');
                    clickElement(innerCheckbox || target);
                }

                const afterChecked = target.matches('input[type="checkbox"]') ? target.checked :
                    !!(target.querySelector('input[type="checkbox"]')?.checked) ||
                    (target.getAttribute('aria-checked') || '').toLowerCase() === 'true' ||
                    (target.getAttribute('aria-pressed') || '').toLowerCase() === 'true' ||
                    norm(target.className || '').includes('checked') || norm(target.className || '').includes('active');

                return {
                    ok:true,
                    kind:targetKind,
                    clicked:!beforeChecked,
                    beforeChecked,
                    afterChecked,
                    blockTop:Math.round(blockRect.top),
                    targetText:norm(target.innerText || target.textContent || target.getAttribute('aria-label') || '')
                };
            }""",
            name_box,
        )
        log(f"  Variants checkbox result: {res}")
        page.wait_for_timeout(300)
        return bool(res and res.get("ok"))
    except Exception as e:
        log(f"  Could not tick Variants: {e}")
        return False


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


def fill_one_attribute(page, name: str, value: str, press_enter: bool = PRESS_ENTER_FOR_ATTRIBUTE_VALUE, is_variant: bool = False, split_values: bool = False) -> bool:
    name_input, name_box = find_empty_name_input(page)

    if name_input is None:
        click_add_attribute(page)
        name_input, name_box = find_empty_name_input(page)

    if name_input is None:
        log("  Could not find empty Attribute Name field.")
        return False

    log(f"Attribute → {name} : {value}" + ("  [VARIANT]" if is_variant else ""))

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

    value_input, value_box = find_value_input_for_name(page, name_box)

    if value_input is None:
        log("  Could not find value field for this attribute.")
        log("  Tip: keep the Attributes tab visible and do not touch the mouse while it is running.")
        return False

    value_input.scroll_into_view_if_needed()
    value_input.click()
    page.wait_for_timeout(120)
    value_input.fill("")
    fill_value_tags(value_input, value, split_values=split_values, press_enter=press_enter)

    if is_variant:
        set_variants_checkbox_for_attribute(page, name_box)

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
    combined = []
    for name, value in data.attributes:
        combined.append({"name": name, "value": value, "is_variant": False, "split_values": False})
    for name, value in data.variant_attributes:
        combined.append({"name": name, "value": value, "is_variant": True, "split_values": True})

    if not combined:
        log("No [ATTRIBUTES] or [VARIANT ATTRIBUTES] data loaded. Skipping Attributes.")
        return

    click_tab(page, "Attributes")
    attrs = combined[:1] if one else combined

    if clear and not one:
        clear_existing_attributes(page)
        page.wait_for_timeout(400)

    for index, item in enumerate(attrs, start=1):
        if stop_requested:
            log("Stopped current automation during Attributes.")
            return

        ok = fill_one_attribute(
            page,
            item["name"],
            item["value"],
            press_enter=PRESS_ENTER_FOR_ATTRIBUTE_VALUE,
            is_variant=item["is_variant"],
            split_values=item["split_values"],
        )
        if not ok:
            log(f"Stopped at attribute {index}. Fix manually or test with one attribute.")
            return

        # Do NOT click Add Attribute here.
        # Earlier versions clicked Add after every filled attribute, and when Prockured
        # shifted/failed to reuse that fresh empty block, it left blank attribute cards
        # between filled attributes. The next loop now creates a new block only when
        # fill_one_attribute() cannot find an existing empty Name field.
        page.wait_for_timeout(250)

    # Clean up any accidentally leftover empty attribute cards at the end.
    remove_empty_attribute_blocks(page)
    log("Attributes fill done.")



# ----------------------------
# Variable product / Variations automation
# ----------------------------

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
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            }
            function norm(s) { return (s || '').toLowerCase().replace(/[^a-z0-9]+/g,' ').replace(/\s+/g,' ').trim(); }
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
            function deactivateCard(card) {
                const text = card.innerText || card.textContent || '';
                if (!/active/i.test(text)) return false;
                const checks = [...card.querySelectorAll('input[type="checkbox"]')].filter(visible);
                for (const cb of checks) {
                    const near = (cb.closest('label, button, div')?.innerText || '').toLowerCase();
                    if (near.includes('active')) {
                        if (cb.checked) cb.click();
                        return true;
                    }
                }
                const btns = [...card.querySelectorAll('button, label, div')].filter(visible);
                const activeBtn = btns.find(b => ((b.innerText||b.textContent||'').toLowerCase().includes('active')));
                if (activeBtn) { activeBtn.click(); return true; }
                return false;
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
                    // Prockured variant order: SKU, Regular, Sale.
                    setValue(inputs[1].el, matched.base_price);
                    setValue(inputs[2].el, matched.sale_price);
                    usedEntries.add(matchedIndex);
                    results.push({card:i+1, status:'priced-active', key:matched.key, base:matched.base_price, sale:matched.sale_price});
                } else if (matched && inputs.length < 3) {
                    results.push({card:i+1, status:'matched-but-inputs-missing', key:matched.key, inputs:inputs.length});
                } else {
                    const deactivated = deactivateCard(card);
                    results.push({card:i+1, status: deactivated ? 'inactive-no-price' : 'no-price-match', preview:text.slice(0,120)});
                }
            }
            return {cards:cards.length, priced:results.filter(r => r.status==='priced-active').length, results};
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
    name = (data.basics.get("Product Name") if data and data.basics else "") or ""
    name = name.strip()
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


def find_best_images(product_name: str, data: ProductData):
    # Manual folder override in [MEDIA] wins if provided.
    folder_raw = ""
    if data and data.media:
        folder_raw = data.media.get("Image Folder", "") or data.media.get("Folder", "") or ""
    if folder_raw:
        folder = Path(folder_raw)
        if folder.exists():
            paths = sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS], key=lambda x: x.name.lower())
            if paths:
                return {"source": "manual-folder", "score": 100, "matched": str(folder), "paths": paths}

    # Prefer all_images.csv if present because it is exact output from scraper.
    from_csv = collect_image_paths_from_csv(product_name, DEFAULT_OUTPUT_DIR)
    if from_csv and from_csv["paths"]:
        return {"source": "csv", "score": from_csv["score"], "matched": from_csv["matched_title"], "paths": from_csv["paths"]}

    from_folders = collect_image_paths_from_folders(product_name, DEFAULT_IMAGE_ROOT)
    if from_folders and from_folders.get("paths"):
        return {"source": "folder", "score": from_folders["score"], "matched": from_folders.get("matched_folder"), "paths": from_folders["paths"]}

    return None


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


def upload_images_to_media(page, image_paths):
    paths = [str(Path(p).resolve()) for p in image_paths if Path(p).exists()]
    if not paths:
        log("No existing local image files to upload.")
        return False

    # Try direct file input first, without opening the OS dialog.
    file_inputs = page.locator("input[type='file']")
    try:
        count = file_inputs.count()
    except Exception:
        count = 0
    for i in range(count):
        inp = file_inputs.nth(i)
        try:
            inp.set_input_files(paths)
            page.wait_for_timeout(1500)
            log(f"Uploaded {len(paths)} image(s) using existing file input.")
            return True
        except Exception:
            continue

    # If file input appears only after clicking Add Image, use expect_file_chooser.
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            clicked = page.evaluate(
                r"""() => {
                    function visible(el) {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                    }
                    const els = [...document.querySelectorAll('button, div, span')].filter(visible);
                    const el = els.find(e => /add\s+image/i.test((e.innerText||e.textContent||'').trim())) ||
                               els.find(e => (e.innerText||e.textContent||'').includes('+'));
                    if (el) { el.click(); return true; }
                    return false;
                }"""
            )
            if not clicked:
                raise RuntimeError("Could not click Add Image tile/button.")
        chooser = fc_info.value
        chooser.set_files(paths)
        page.wait_for_timeout(2000)
        log(f"Uploaded {len(paths)} image(s) using file chooser.")
        return True
    except Exception as e:
        log(f"Could not upload images automatically: {e}")
        log("Tip: open Media tab and press Alt+Shift+D so we can inspect upload fields/buttons if this fails.")
        return False


def fill_media(page, data: ProductData):
    product_name = get_product_name_for_matching(page, data)
    if not product_name:
        log("Could not determine product name for media matching.")
        return
    click_tab(page, "Media")
    log(f"Finding images for: {product_name}")
    found = find_best_images(product_name, data)
    if not found or not found.get("paths"):
        log(f"No image match found in {DEFAULT_IMAGE_ROOT}. Run scraper first or set [MEDIA] Image Folder.")
        return
    all_paths = found["paths"]
    selected = all_paths[:MAX_MEDIA_UPLOADS]
    log(f"Image match source: {found['source']} | score: {found['score']} | matched: {found['matched']}")
    log(f"Images selected: {len(selected)}")
    for p in selected:
        log(f"  {p}")
    # Do not replace/delete existing media unless explicitly requested in [MEDIA].
    delete_existing_media_if_requested(page, data)
    upload_images_to_media(page, selected)
    update_media_alt_texts(page, data)
    log("Media fill done. Review images manually before saving.")


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
    log(f"  Variant Attributes: {len(data.variant_attributes)}")
    log(f"  Variant Pricing rows: {len(data.variant_pricing)}")
    log(f"  SEO keys: {list(data.seo.keys())}")
    log("  Slug will be cleared/left empty. SKU, Brand and Category will not be touched.")
    if not data.basics and not data.attributes and not data.seo:
        log("  WARNING: No [BASICS], [ATTRIBUTES], [SEO] data found.")


def require_data():
    if current_data is None:
        load_from_clipboard()
    return current_data


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
        elif cmd == "full":
            stop_requested = False
            data = require_data()
            fill_basics(page, data)
            if stop_requested: return
            fill_attributes(page, data, one=False, clear=True)
            if stop_requested: return
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
        "<alt>+<shift>+f": lambda: on_hotkey("full"),
        "<alt>+<shift>+d": lambda: on_hotkey("debug"),
        "<alt>+<shift>+x": lambda: on_hotkey("stop"),
        "<alt>+<shift>+q": lambda: on_hotkey("quit"),
    })
    hotkeys.start()
    return hotkeys


def main():
    log("========================================")
    log("Prockured Full Listing Hotkey v14 Variable Products - No Brand/Category Touch")
    log("========================================")
    log("Alt + Shift + L = Load clipboard data")
    log("Alt + Shift + B = Fill Basics only")
    log("Alt + Shift + A = Fill Attributes only")
    log("Alt + Shift + 1 = Test one attribute")
    log("Alt + Shift + S = Fill SEO only")
    log("Alt + Shift + V = Generate/fill Variants only")
    log("Alt + Shift + M = Fill Media only")
    log("Alt + Shift + I = Update Media image alt text only")
    log("Alt + Shift + R = Fill Pricing only")
    log("Alt + Shift + F = Fill Full product: Basics + Attributes + Variants/Pricing + SEO + Media")
    log("Alt + Shift + D = Debug visible fields")
    log("Alt + Shift + X = Stop current automation")
    log("Alt + Shift + Q = Quit script")
    log("========================================")
    log("Important: Brand and Category are NOT touched. SKU is NOT touched. Slug is cleared/left empty. Product Tags are filled only inside Product Tags. Supports simple + variable products. Variant checkbox click fixed. Variant price logic: sale price = given price rounded up, regular/base = stable 10-30% markup rounded up. Missing variant prices are marked inactive where possible.")

    hotkeys = start_hotkeys()
    with sync_playwright() as pw:
        browser = None
        page = None
        browser, page = connect_page(pw, browser)
        log(f"Connected to: {page.url}")
        log("Script is running. Keep this terminal open.")
        while True:
            cmd = command_queue.get()
            # Reconnect before every hotkey. This fixes stale/closed Playwright page targets.
            try:
                browser, page = connect_page(pw, browser)
                log(f"Using page: {page.url}")
            except Exception as e:
                log(f"Could not reconnect to Prockured page: {e}")
                continue
            run_command(cmd, page)


if __name__ == "__main__":
    main()
