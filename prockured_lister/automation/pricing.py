from ..logger import logger
from ..models import ProductData
from ..browser import click_tab
import re
import time

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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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
    logger.info("Opening Pricing tab safely...")

    if is_pricing_tab_visible(page):
        return True

    # Make the sticky/top tab bar easy to locate.
    try:
        page.evaluate("() => { window.scrollTo({top: 0, left: 0, behavior: 'instant'}); }")
        page.wait_for_timeout(250)
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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
            logger.info(f"Pricing tab coordinate click: {info.get('chosen')}")
            page.mouse.click(info["x"], info["y"])
            page.wait_for_timeout(1300)
            if is_pricing_tab_visible(page):
                return True
        else:
            logger.info(f"Pricing tab coordinate candidate not found: {info}")
    except Exception as e:
        logger.info(f"Pricing coordinate click failed: {e}")

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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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
        logger.info("Could not determine product name for pricing matching.")
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
        logger.info("No price found. Add [PRICING] Sale Price : ... or check scraper CSV price columns.")
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
            except Exception as e:
                logger.debug(f'Exception: {e}', exc_info=True)
                try:
                    page.get_by_role("button", name=re.compile(r"^\s*Pricing\s*$", re.I)).first.click(timeout=1800, force=True)
                except Exception as e:
                    logger.debug(f'Exception: {e}', exc_info=True)
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
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            pass

    if not is_pricing_tab_visible(page):
        logger.info("Could not confirm Pricing tab. Pricing values were NOT pasted anywhere.")
        logger.info("Open Pricing tab manually and press Alt + Shift + R again.")
        return

    logger.info(f"Pricing for: {product_name}")
    logger.info(f"Sale/discount price source: {price_source}")
    logger.info(f"Sale/discount price rounded up: {sale_price}")
    logger.info(f"Stable markup: {markup:.2f}%")
    logger.info(f"Base price rounded up: {base_price}")

    res = set_pricing_fields(page, base_price, sale_price)
    logger.info(f"Pricing field result: {res}")
    logger.info("Pricing fill done.")

