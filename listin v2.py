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
DEFAULT_OUTPUT_DIR = Path(os.environ.get("PROCKURED_OUTPUT_DIR", "prockured_output"))
DEFAULT_IMAGE_ROOT = Path(os.environ.get("PROCKURED_IMAGE_ROOT", str(DEFAULT_OUTPUT_DIR / "images")))
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_MEDIA_UPLOADS = int(os.environ.get("PROCKURED_MAX_MEDIA_UPLOADS", "8"))
BASE_MARKUP_MIN = 10
BASE_MARKUP_MAX = 20

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
    seo: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)


BASICS_KEYS = [
    "Product Name",
    "Slug",
    "Product Tags",
    "Description",
    "Short Description",
]
SEO_KEYS = ["SEO Title", "SEO Description", "SEO Keywords"]
MEDIA_KEYS = ["Main Image", "Image 1", "Image 2", "Image 3", "Image URL", "Image URLs"]
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


def parse_clipboard_text(text: str) -> ProductData:
    data = ProductData()
    sections = {"BASICS": [], "ATTRIBUTES": [], "SEO": [], "MEDIA": [], "PRICING": []}
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


def fill_basics(page, data: ProductData):
    click_tab(page, "Basics")
    b = data.basics
    log("Filling Basics. Slug will be cleared only. Product Tags will stay in Product Tags only.")

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


def fill_one_attribute(page, name: str, value: str, press_enter: bool = PRESS_ENTER_FOR_ATTRIBUTE_VALUE) -> bool:
    name_input, name_box = find_empty_name_input(page)

    if name_input is None:
        click_add_attribute(page)
        name_input, name_box = find_empty_name_input(page)

    if name_input is None:
        log("  Could not find empty Attribute Name field.")
        return False

    log(f"Attribute → {name} : {value}")

    name_input.scroll_into_view_if_needed()
    page.wait_for_timeout(120)

    # Important v7 fix: scrolling can change the input's screen coordinates.
    # Recompute before trying to locate the matching value field.
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

    # Recompute again after dropdown closes because Prockured may shift the block.
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
    value_input.fill(value)
    page.wait_for_timeout(200)

    if press_enter:
        # Enter is sent directly to the value input, not the whole page.
        value_input.press("Enter")
        page.wait_for_timeout(350)

    return True


def fill_attributes(page, data: ProductData, one=False, clear=True):
    if not data.attributes:
        log("No [ATTRIBUTES] data loaded. Skipping Attributes.")
        return

    click_tab(page, "Attributes")
    attrs = data.attributes[:1] if one else data.attributes

    if clear and not one:
        clear_existing_attributes(page)
        page.wait_for_timeout(400)

    for index, (name, value) in enumerate(attrs, start=1):
        if stop_requested:
            log("Stopped current automation during Attributes.")
            return

        ok = fill_one_attribute(page, name, value, press_enter=PRESS_ENTER_FOR_ATTRIBUTE_VALUE)
        if not ok:
            log(f"Stopped at attribute {index}. Fix manually or test with one attribute.")
            return

        # Create the next empty block only after the current value/tag is completed.
        if index < len(attrs):
            click_add_attribute(page)
            page.wait_for_timeout(500)

    log("Attributes fill done.")


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
    delete_existing_media_if_requested(page, data)
    upload_images_to_media(page, selected)
    log("Media fill done. Review images manually before saving.")


def set_pricing_fields(page, base_price: int, sale_price: int):
    res = page.evaluate(
        r"""({basePrice, salePrice}) => {
            function isVisible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            }
            function normalize(s) { return (s || '').replace(/\*/g,'').replace(/\s+/g,' ').trim().toUpperCase(); }
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
            const labels = [...document.querySelectorAll('label, div, span, p, strong')]
                .filter(isVisible)
                .map(el => ({el, text: normalize(el.innerText || el.textContent), r: el.getBoundingClientRect()}))
                .filter(o => o.text && o.text.length < 80)
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
            const inputs = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio])')]
                .filter(isVisible)
                .filter(el => !el.disabled && !el.readOnly)
                .map(el => ({el, r: el.getBoundingClientRect(), old: el.value || '', ph: el.placeholder || ''}))
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
            function fieldAfterLabel(labelNeedles) {
                const lab = labels.find(o => labelNeedles.some(n => o.text.includes(n)));
                if (!lab) return null;
                const candidates = inputs
                    .filter(i => i.r.top >= lab.r.bottom - 15 && i.r.top < lab.r.bottom + 160)
                    .sort((a,b) => (a.r.top - b.r.top) || (a.r.left - b.r.left));
                return candidates[0] || null;
            }
            const base = fieldAfterLabel(['BASE PRICE']);
            const discount = fieldAfterLabel(['DISCOUNT PRICE', 'SALE PRICE']);
            if (base) setValue(base.el, basePrice);
            if (discount) setValue(discount.el, salePrice);
            return {
                baseFound: !!base,
                discountFound: !!discount,
                baseOld: base ? base.old : null,
                discountOld: discount ? discount.old : null
            };
        }""",
        {"basePrice": base_price, "salePrice": sale_price},
    )
    return res


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
    click_tab(page, "Pricing")
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
    log(f"  SEO keys: {list(data.seo.keys())}")
    log("  Slug will be cleared/left empty.")
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
        elif cmd == "media":
            stop_requested = False
            fill_media(page, require_data())
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
            fill_seo(page, data)
            if stop_requested: return
            fill_media(page, data)
            if stop_requested: return
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
        "<alt>+<shift>+m": lambda: on_hotkey("media"),
        "<alt>+<shift>+p": lambda: on_hotkey("pricing"),
        "<alt>+<shift>+f": lambda: on_hotkey("full"),
        "<alt>+<shift>+d": lambda: on_hotkey("debug"),
        "<alt>+<shift>+x": lambda: on_hotkey("stop"),
        "<alt>+<shift>+q": lambda: on_hotkey("quit"),
    })
    hotkeys.start()
    return hotkeys


def main():
    log("========================================")
    log("Prockured Full Listing Hotkey v9 Media + Pricing")
    log("========================================")
    log("Alt + Shift + L = Load clipboard data")
    log("Alt + Shift + B = Fill Basics only")
    log("Alt + Shift + A = Fill Attributes only")
    log("Alt + Shift + 1 = Test one attribute")
    log("Alt + Shift + S = Fill SEO only")
    log("Alt + Shift + M = Fill Media only")
    log("Alt + Shift + P = Fill Pricing only")
    log("Alt + Shift + F = Fill Full product + Media + Pricing")
    log("Alt + Shift + D = Debug visible fields")
    log("Alt + Shift + X = Stop current automation")
    log("Alt + Shift + Q = Quit script")
    log("========================================")
    log("Important: SKU and Slug are cleared/left empty. Product Tags are filled only inside Product Tags, not SKU. Browser page reconnects before every hotkey. Attribute engine v7 plus Description paragraph preservation. v9 also uploads matched images from prockured_output/images and fills Pricing with stable 10-20% base markup over rounded-up sale price.")

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
