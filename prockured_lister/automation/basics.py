from ..logger import logger
from ..models import ProductData
from ..browser import click_tab
import re
import time

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
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        pass
    return False

def fill_brand_dropdown(page, brand: str):
    """Fill Brand combobox properly: click Search brand field, type brand, click matching option."""
    brand = (brand or '').strip()
    if not brand:
        return

    logger.info(f"Filling Basics → Brand dropdown: {brand}")

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
            except Exception as e:
                logger.debug(f'Exception: {e}', exc_info=True)
                page.keyboard.press("Control+A")
                page.keyboard.insert_text(brand)
            opened = True
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        opened = False

    if not opened:
        res = _click_field_below_label(page, "BRAND", preferred_placeholder="Search brand")
        logger.info(f"  Brand field open result: {res}")
        if not res or not res.get('ok'):
            return
        page.wait_for_timeout(250)
        try:
            page.keyboard.press("Control+A")
            page.keyboard.insert_text(brand)
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            pass

    page.wait_for_timeout(800)

    # Select exact dropdown result like 'Amul'.
    clicked = _click_visible_option(page, brand, exact=True)
    if not clicked:
        clicked = _click_visible_option(page, brand, exact=False)
    if not clicked:
        logger.info("  Brand option not clicked by text. Pressing Enter fallback.")
        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            pass
    else:
        logger.info(f"  Brand selected: {brand}")

def fill_product_type_dropdown(page, product_type: str):
    """Select Product Type dropdown value: Simple Product / Variable Product / Group-Bundle Product."""
    product_type = (product_type or '').strip()
    if not product_type:
        return

    logger.info(f"Filling Basics → Product Type dropdown: {product_type}")

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
            logger.info(f"  Product Type selected directly: {direct}")
            page.wait_for_timeout(700)
            return
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        pass

    # Custom select path: click Product Type field and choose visible option.
    res = _click_field_below_label(page, "PRODUCT TYPE")
    logger.info(f"  Product Type field open result: {res}")
    if not res or not res.get('ok'):
        return
    page.wait_for_timeout(500)

    clicked = _click_visible_option(page, product_type, exact=True)
    if not clicked:
        clicked = _click_visible_option(page, product_type, exact=False)
    if not clicked:
        logger.info("  Product Type option not clicked by text. Keyboard fallback.")
        try:
            page.keyboard.insert_text(product_type)
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")
            page.wait_for_timeout(700)
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            pass
    else:
        logger.info(f"  Product Type selected: {product_type}")

def fill_basics(page, data: ProductData):
    click_tab(page, "Basics")
    b = data.basics
    logger.info("Filling Basics. Brand/Category/SKU will NOT be touched. Slug will be cleared only. Product Tags will stay in Product Tags only.")

    if b.get("Product Type"):
        fill_product_type_dropdown(page, b.get("Product Type"))
        time.sleep(0.5)

    # Brand is intentionally NOT automated now.
    # You will select Brand manually, so the script does not risk touching Category/Brand dropdowns.
    if b.get("Brand"):
        logger.info("Skipping Brand automation. Please select Brand manually.")
        time.sleep(0.10)

    if b.get("Product Name"):
        logger.info("Filling Basics → Product Name")
        res = set_field_in_labeled_section(page, ["PRODUCT NAME", "Product Name"], b.get("Product Name", ""), allow_textarea=False, allow_input=True)
        logger.info(f"  result: {res}")
        time.sleep(0.25)

    logger.info("Clearing Slug so Prockured can auto-generate it.")
    res = set_field_in_labeled_section(page, ["SLUG", "Slug"], "", allow_textarea=False, allow_input=True, clear_only=True)
    logger.info(f"  Slug clear result: {res}")
    time.sleep(0.25)

    if b.get("Product Tags"):
        logger.info("Filling Basics → Product Tags")
        # Allow input and textarea, but only inside the Product Tags section and before the SKU label.
        res = set_field_in_labeled_section(page, ["PRODUCT TAGS", "Product Tags"], b.get("Product Tags", ""), allow_textarea=True, allow_input=True)
        logger.info(f"  result: {res}")
        time.sleep(0.25)

    if b.get("Description"):
        logger.info("Filling Basics → Description")
        res = set_field_in_labeled_section(page, ["DESCRIPTION", "Description"], b.get("Description", ""), allow_textarea=True, allow_input=False)
        logger.info(f"  result: {res}")
        time.sleep(0.25)

    if b.get("Short Description"):
        logger.info("Filling Basics → Short Description")
        res = set_field_in_labeled_section(page, ["SHORT DESCRIPTION", "Short Description"], b.get("Short Description", ""), allow_textarea=True, allow_input=False)
        logger.info(f"  result: {res}")
        time.sleep(0.25)

    logger.info("Basics fill done.")

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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        pass
    return ""

def normalize_sku(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "").strip())

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
            except Exception as e:
                logger.debug(f'Exception: {e}', exc_info=True)
                pass
            page.wait_for_timeout(2000)
            return True
        logger.info(f"Search input not found: {res}")
    except Exception as e:
        logger.info(f"Search failed: {e}")
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
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
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
        logger.info(f"Could not open product result: {res}")
    except Exception as e:
        logger.info(f"Product result click failed: {e}")
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
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            pass
    return normalize_sku(actual) == normalize_sku(expected_sku), actual

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
            logger.info(f"  Dropdown option clicked: {clicked}")
            return True
        logger.info(f"  Dropdown flexible click failed: {clicked}")
    except Exception as e:
        logger.info(f"  Dropdown flexible click error: {e}")
    return False

def fill_searchable_select_exact(page, label_text: str, option_text: str, required: bool = False, search_text: str = "") -> bool:
    option_text = str(option_text or "").strip()
    search_text = str(search_text or option_text or "").strip()
    if not option_text:
        return not required

    logger.info(f"Selecting {label_text}: {option_text}")
    res = _click_field_below_label(page, label_text)
    logger.info(f"  {label_text} open result: {res}")
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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        pass

    try:
        page.keyboard.insert_text(search_text)
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        try:
            page.keyboard.type(search_text, delay=15)
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            return False

    page.wait_for_timeout(1100)

    # First try exact full option, then tolerant click using the shorter search text.
    clicked = _click_visible_option(page, option_text, exact=True)
    if not clicked:
        clicked = _click_visible_option_flexible(page, option_text, search_text=search_text, exact=False)

    # Do NOT press Enter as fallback for category/brand. Enter can keep the old
    # category or choose a wrong first option. If we did not click a visible match,
    # fail safely and report it.
    logger.info(f"  {label_text} selected status: {clicked}")
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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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

def select_category_for_batch(page, category_option: str) -> bool:
    """Safely select Category by exact CATEGORY label: type, Enter once, Tab twice, verify Brand focus."""
    category_option = str(category_option or "").strip()
    if not category_option:
        return False
    search = category_search_text(category_option)
    logger.info(f"Selecting CATEGORY with Enter+Tab+Tab: option='{category_option}' search='{search}'")

    click_tab(page, "Basics")
    page.wait_for_timeout(400)

    res = _type_category_and_press_enter(page, search)
    logger.info(f"  Category type+enter+tab+tab result: {res}")
    if not res or not res.get("ok"):
        return False

    selected = _read_selected_category_text(page)
    logger.info(f"  Category selected value after Enter+Tab: {selected}")

    # Strict success when the UI exposes the selected value.
    if selected and normalize_select_text(search) in normalize_select_text(selected):
        return True

    # If UI does not expose the selected value but the field action succeeded, continue.
    # The Save step will fail/report if category truly remained blank.
    logger.info("  Category readable verification was unclear, but Enter+Tab+Tab reached the Brand field. Continuing.")
    return True

def select_brand_for_batch(page, brand_option: str) -> bool:
    """Select Brand only when an actual matching dropdown option exists.

    If the brand is blank or not found, the function clears the brand search box and
    continues safely without leaving invalid typed text in the Brand field.
    """
    if value_is_blank(brand_option):
        logger.info("Brand blank in JSON. Leaving brand empty.")
        return True
    brand = str(brand_option).strip()
    logger.info(f"Selecting BRAND safely: {brand}")
    click_tab(page, "Basics")
    page.wait_for_timeout(250)
    try:
        box = page.get_by_placeholder(re.compile(r"search\s+brand", re.I)).first
        if box.count() <= 0:
            logger.info("  Brand search input not found.")
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
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            no_match = False
        if no_match:
            logger.info(f"  Brand not found. Clearing brand search and continuing without brand: {brand}")
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.press("Escape")
            return False

        clicked = _click_visible_option(page, brand, exact=True)
        if not clicked:
            clicked = _click_visible_option_flexible(page, brand, search_text=brand, exact=False)
        if not clicked:
            logger.info(f"  Brand option not clicked. Clearing brand search and continuing without brand: {brand}")
            try:
                box.click(force=True, timeout=1200)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.press("Escape")
            except Exception as e:
                logger.debug(f'Exception: {e}', exc_info=True)
                pass
            return False
        page.wait_for_timeout(500)
        logger.info(f"  Brand selected: {brand}")
        return True
    except Exception as e:
        logger.info(f"  Brand selection error: {e}")
        return False

