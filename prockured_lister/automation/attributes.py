from ..logger import logger
from ..models import ProductData
from ..browser import click_tab
import re
import time

def click_add_attribute(page):
    """Click the Add Attribute button reliably."""
    try:
        page.get_by_role("button", name=re.compile(r"add\s+attribute", re.I)).click()
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        try:
            page.locator("button").filter(has_text=re.compile(r"Add\s+Attribute", re.I)).last.click()
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
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
            logger.info(f"  Reusing blank/stale Attribute Name field currently showing '{info.get('value')}'.")
        return loc, box
    except Exception as e:
        logger.info(f"  find_empty_name_input failed: {e}")
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
        logger.info(f"  find_value_input_for_name failed: {e}")
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
        logger.info(f"  Attribute delete click failed: {e}")
        return False

def clear_existing_attributes(page, max_clicks: int = 80):
    logger.info("Clearing old Attribute blocks...")
    deleted = 0
    for _ in range(max_clicks):
        if stop_requested:
            logger.info("Stopped while clearing attributes.")
            break
        clicked = click_first_attribute_delete_button(page)
        if not clicked:
            break
        deleted += 1
        page.wait_for_timeout(350)
    logger.info(f"Deleted/cleared attribute blocks: {deleted}")

def dismiss_name_dropdown(page):
    """Close the suggestion dropdown after typing attribute name."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        pass
    try:
        page.evaluate("""() => { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); }""")
        page.wait_for_timeout(100)
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        pass
    # click a safe empty text area if present; this was part of the earlier stable method
    try:
        safe_text = page.get_by_text("Reuse existing attribute names and values", exact=False)
        if safe_text.count() > 0:
            safe_text.first.click(timeout=700, force=True)
            page.wait_for_timeout(120)
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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
        logger.info(f"  {toggle_name} checkbox for '{attribute_name}' result: {res}")
        page.wait_for_timeout(350)
        return bool(res and res.get("ok"))
    except Exception as e:
        logger.info(f"  Could not set {toggle_name} for '{attribute_name}': {e}")
        return False

def set_variants_checkbox_for_attribute(page, name_box) -> bool:
    """Legacy coordinate method disabled.

    The old coordinate-based function could click the right Variants control and then
    misread/click again on some Prockured layouts. New code sets Variants by exact
    attribute name through set_attribute_toggle_by_name().
    """
    logger.info("  Skipping legacy coordinate-based Variants click; using attribute-name verification instead.")
    return True

def ensure_variant_attributes_checked(page, data: ProductData):
    """Final safety pass: every [VARIANT ATTRIBUTES] name must have Variants checked."""
    if not data or not data.variant_attributes:
        return
    logger.info("Verifying Variants checkbox for all [VARIANT ATTRIBUTES]...")
    for name, _value in data.variant_attributes:
        if stop_requested:
            logger.info("Stopped while verifying variant attributes.")
            return
        set_attribute_toggle_by_name(page, name, "Variants", True)
    logger.info("Variant attribute checkbox verification done.")

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
        logger.info("  Could not find empty Attribute Name field.")
        return False

    logger.info(f"Attribute → {name} : {value}" + ("  [VARIANT]" if is_variant else ""))

    name_input.scroll_into_view_if_needed()
    page.wait_for_timeout(120)

    try:
        fresh_box = name_input.bounding_box(timeout=1200)
        if fresh_box:
            name_box = fresh_box
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
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
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        pass

    value_input, value_box = find_value_input_for_name(page, name_box)

    if value_input is None:
        logger.info("  Could not find value field for this attribute.")
        logger.info("  Tip: keep the Attributes tab visible and do not touch the mouse while it is running.")
        return False

    value_input.scroll_into_view_if_needed()
    value_input.click()
    page.wait_for_timeout(120)
    value_input.fill("")
    fill_value_tags(value_input, value, split_values=split_values, press_enter=press_enter)

    if is_variant:
        # Set by exact attribute name, not old screen coordinates. This prevents
        # the checkbox from being clicked again and accidentally deselected.
        set_attribute_toggle_by_name(page, name, "Variants", True)

    return True

def remove_empty_attribute_blocks(page, max_remove: int = 20):
    """Remove empty attribute cards left by old runs or UI shifts.

    Only deletes a card when the attribute Name input is empty AND no value chips/tags
    are present. It avoids touching filled attributes.
    """
    logger.info("Cleaning leftover empty Attribute blocks...")
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
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            clicked = False
        if not clicked:
            break
        removed += 1
        page.wait_for_timeout(200)
    logger.info(f"Removed leftover empty Attribute blocks: {removed}")

def fill_attributes(page, data: ProductData, one=False, clear=True):
    """Fill all normal attributes first, then all variant attributes.

    Returns True only when the Attributes step completed successfully. Full mode
    uses this return value so it will not move to Variations/Generate Variants if
    attributes failed or only partially filled.
    """
    regular = []
    variant = []
    for name, value in data.attributes:
        regular.append({"name": name, "value": value, "is_variant": False, "split_values": False})
    for name, value in data.variant_attributes:
        variant.append({"name": name, "value": value, "is_variant": True, "split_values": True})

    combined = regular + variant

    if not combined:
        logger.info("No [ATTRIBUTES] or [VARIANT ATTRIBUTES] data loaded. Skipping Attributes.")
        return True

    click_tab(page, "Attributes")
    attrs = combined[:1] if one else combined

    logger.info(f"Attributes to fill: regular={len(regular)} variant={len(variant)} total={len(attrs)}")

    if clear and not one:
        clear_existing_attributes(page)
        page.wait_for_timeout(500)

    for index, item in enumerate(attrs, start=1):
        if stop_requested:
            logger.info("Stopped current automation during Attributes.")
            return False

        ok = fill_one_attribute(
            page,
            item["name"],
            item["value"],
            press_enter=PRESS_ENTER_FOR_ATTRIBUTE_VALUE,
            is_variant=item["is_variant"],
            split_values=item["split_values"],
        )
        if not ok:
            logger.info(f"Attribute fill failed at {index}/{len(attrs)}: {item['name']}. Full flow will NOT move to Variations.")
            return False

        page.wait_for_timeout(300)

    # Clean up any accidentally leftover empty attribute cards at the end.
    remove_empty_attribute_blocks(page)

    # Final safety check for variable products: only turn Variants ON for the
    # attributes listed under [VARIANT ATTRIBUTES]. Never toggle them off.
    ensure_variant_attributes_checked(page, data)

    logger.info("Attributes fill done. All attributes completed before Variations.")
    return True

