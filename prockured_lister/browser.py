from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from .config import CDP_URL
from .logger import logger
import re

def score_product_page(page):
    try:
        url = page.url or ""
        title = page.title() or ""
        score = 0
        if "prockured" in url.lower(): score += 10
        if "admin/products" in url.lower(): score += 20
        if "admin | prockured" in title.lower(): score += 5
        if score == 0: return -999
        return score
    except Exception:
        return -999

def page_is_alive(page):
    try:
        page.title()
        return True
    except Exception:
        return False

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
    return candidates[0][1]

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
        except Exception as e:
            logger.debug(f"Stale browser discarded: {e}", exc_info=True)
    
    browser = pw.chromium.connect_over_cdp(CDP_URL)
    page = find_product_page(browser)
    return browser, page

def click_tab(page, tab_name):
    logger.info(f"Switching to tab: {tab_name}")
    try:
        if tab_name.lower() == "basics":
            tab_name = "Basic"
        btn = page.get_by_role("tab", name=re.compile(f"^\\s*{tab_name}\\s*$", re.I)).first
        if btn.count() > 0:
            btn.click(timeout=2500)
            page.wait_for_timeout(250)
            return True
    except PlaywrightTimeoutError:
        logger.debug(f"Tab '{tab_name}' not found by role.")
    except Exception as e:
        logger.debug(f"Exception clicking tab '{tab_name}': {e}")
    return False
