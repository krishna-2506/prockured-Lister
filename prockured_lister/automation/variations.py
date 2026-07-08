from ..logger import logger
from ..models import ProductData
from ..browser import click_tab
import re
import time

def click_generate_variants(page):
    """Open Variations tab and click Generate Variants if available."""
    if not click_tab(page, "Variations"):
        click_tab(page, "Catalog")
    page.wait_for_timeout(900)
    logger.info("Trying to click Generate Variants...")
    clicked = False
    try:
        page.get_by_role("button", name=re.compile(r"generate\s+variants", re.I)).click(timeout=3500)
        clicked = True
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            clicked = False
    if clicked:
        logger.info("Generate Variants clicked. Waiting for combinations...")
        page.wait_for_timeout(3500)
    else:
        logger.info("Generate Variants button not found/clicked. Continuing to existing variant rows.")
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
        logger.info("Not a variable product. Skipping variant prices.")
        return
    entries = build_variant_price_entries(data)
    if not entries:
        logger.info("No [VARIANT PRICING] rows found. Variants may be generated but prices will not be filled.")
        return
    if not click_tab(page, "Variations"):
        click_tab(page, "Catalog")
    page.wait_for_timeout(1000)
    logger.info(f"Filling variant prices. Pricing rows loaded: {len(entries)}")
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
                    const activeResult = setCardActive(card, true);
                    setValue(inputs[1].el, matched.base_price);
                    setValue(inputs[2].el, matched.sale_price);
                    usedEntries.add(matchedIndex);
                    results.push({card:i+1, status:'priced-active', active:activeResult, key:matched.key, base:matched.base_price, sale:matched.sale_price});
                } else if (matched && inputs.length < 3) {
                    const activeResult = setCardActive(card, true);
                    results.push({card:i+1, status:'matched-but-inputs-missing', active:activeResult, key:matched.key, inputs:inputs.length});
                } else {
                    const activeResult = setCardActive(card, false);
                    results.push({card:i+1, status: activeResult.ok ? 'inactive-no-price' : 'no-price-match-active-not-found', active:activeResult, preview:text.slice(0,120)});
                }
            }
            return {cards:cards.length, priced:results.filter(r => r.status==='priced-active').length, results};
        }""",
        {"entries": entries},
    )
    logger.info(f"Variant fill result: cards={res.get('cards')} priced={res.get('priced')}")
    for r in res.get("results", []):
        logger.info(f"  {r}")
    logger.info("Variant price fill done.")

def fill_variations(page, data: ProductData):
    if not is_variable_product(data):
        logger.info("Not a variable product. Skipping Variations.")
        return
    click_generate_variants(page)
    fill_variant_prices(page, data)

