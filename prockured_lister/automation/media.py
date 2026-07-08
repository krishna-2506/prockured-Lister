from ..logger import logger
from ..models import ProductData
from ..browser import click_tab
import re
import time

def delete_existing_media_if_requested(page, data: ProductData):
    rep = ""
    if data and data.media:
        rep = (data.media.get("Replace Existing Images") or data.media.get("Replace Images") or "").strip().lower()
    if rep not in {"yes", "true", "1", "y"}:
        return
    logger.info("Replace Existing Images = Yes. Trying to remove old media...")
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
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
            pass

def upload_images_to_media(page, image_paths):
    paths = [str(Path(p).resolve()) for p in image_paths if Path(p).exists()]
    if not paths:
        logger.info("No existing local image files to upload.")
        return False

    # Try direct file input first, without opening the OS dialog.
    file_inputs = page.locator("input[type='file']")
    try:
        count = file_inputs.count()
    except Exception as e:
        logger.debug(f'Exception: {e}', exc_info=True)
        count = 0
    for i in range(count):
        inp = file_inputs.nth(i)
        try:
            inp.set_input_files(paths)
            page.wait_for_timeout(1500)
            logger.info(f"Uploaded {len(paths)} image(s) using existing file input.")
            return True
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
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
        logger.info(f"Uploaded {len(paths)} image(s) using file chooser.")
        return True
    except Exception as e:
        logger.info(f"Could not upload images automatically: {e}")
        logger.info("Tip: open Media tab and press Alt+Shift+D so we can inspect upload fields/buttons if this fails.")
        return False

def fill_media(page, data: ProductData):
    product_name = get_product_name_for_matching(page, data)
    if not product_name:
        logger.info("Could not determine product name for media matching.")
        return
    click_tab(page, "Media")
    logger.info(f"Finding images for: {product_name}")
    found = find_best_images(product_name, data)
    if not found or not found.get("paths"):
        logger.info(f"No image match found in {DEFAULT_IMAGE_ROOT}. Run scraper first or set [MEDIA] Image Folder.")
        return
    all_paths = found["paths"]
    selected = all_paths[:MAX_MEDIA_UPLOADS]
    logger.info(f"Image match source: {found['source']} | score: {found['score']} | matched: {found['matched']}")
    logger.info(f"Images selected: {len(selected)}")
    for p in selected:
        logger.info(f"  {p}")
    # Do not replace/delete existing media unless explicitly requested in [MEDIA].
    delete_existing_media_if_requested(page, data)
    upload_images_to_media(page, selected)
    update_media_alt_texts(page, data)
    logger.info("Media fill done. Review images manually before saving.")

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
        logger.info("Could not determine product name for image alt text.")
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
        logger.info("No visible media image cards found for alt text update.")
        return

    logger.info(f"Updating alt text for {len(cards)} visible media image(s)...")

    updated = 0
    for idx, card in enumerate(cards, start=1):
        if stop_requested:
            logger.info("Stopped while updating image alt text.")
            break

        text = alt_text_for(idx)
        try:
            page.mouse.move(card["x"], card["y"])
            page.wait_for_timeout(300)
        except Exception as e:
            logger.debug(f'Exception: {e}', exc_info=True)
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
            logger.info(f"  Image {idx}: {res.get('newValue')}")
        else:
            logger.info(f"  Image {idx}: could not update alt text ({res})")

    logger.info(f"Alt text update done. Updated: {updated}/{len(cards)}")

