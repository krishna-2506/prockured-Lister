import os
import argparse
import queue
import traceback
from pathlib import Path
from pynput import keyboard
from playwright.sync_api import sync_playwright

from .logger import logger
from .config import (
    RUN_DIR, DEFAULT_BATCH_JSON, DEFAULT_OUTPUT_DIR, DEFAULT_IMAGE_ROOT,
    DEFAULT_BATCH_REPORT_DIR, BotState
)
from .browser import connect_page
from .models import ProductData
from .parser import parse_clipboard_text, is_variable_product
from .batch import run_batch_json

from .automation.basics import fill_basics
from .automation.attributes import fill_attributes
from .automation.variations import fill_variations
from .automation.seo import fill_seo
from .automation.media import fill_media, update_media_alt_texts
from .automation.pricing import fill_pricing

command_queue = queue.Queue()

def load_from_clipboard():
    import pyperclip
    text = pyperclip.paste()
    if not text:
        logger.info("Clipboard is empty.")
        return
    BotState.current_data = parse_clipboard_text(text)
    logger.info("Loaded ProductData from clipboard.")

def require_data() -> ProductData:
    if not BotState.current_data:
        logger.info("No data loaded. Loading from clipboard now.")
        load_from_clipboard()
    return BotState.current_data or ProductData()

def debug_current_tab(page):
    logger.info("Debug info:")
    logger.info(f"URL: {page.url}")
    logger.info(f"Title: {page.title()}")

def run_command(cmd, page):
    try:
        if cmd == "load":
            load_from_clipboard()
        elif cmd == "basics":
            BotState.stop_requested = False
            fill_basics(page, require_data())
        elif cmd == "attributes":
            BotState.stop_requested = False
            fill_attributes(page, require_data(), one=False, clear=True)
        elif cmd == "one":
            BotState.stop_requested = False
            fill_attributes(page, require_data(), one=True, clear=False)
        elif cmd == "seo":
            BotState.stop_requested = False
            fill_seo(page, require_data())
        elif cmd == "variants":
            BotState.stop_requested = False
            fill_variations(page, require_data())
        elif cmd == "media":
            BotState.stop_requested = False
            fill_media(page, require_data())
        elif cmd == "alttext":
            BotState.stop_requested = False
            update_media_alt_texts(page, require_data())
        elif cmd == "pricing":
            BotState.stop_requested = False
            fill_pricing(page, require_data())
        elif cmd == "batch":
            BotState.stop_requested = False
            run_batch_json(page, DEFAULT_BATCH_JSON)
        elif cmd == "full":
            BotState.stop_requested = False
            data = require_data()
            fill_basics(page, data)
            if BotState.stop_requested: return
            attributes_ok = fill_attributes(page, data, one=False, clear=True)
            if BotState.stop_requested: return
            if not attributes_ok:
                logger.info("Full fill stopped because Attributes did not complete. Variations were NOT generated.")
                return
            if is_variable_product(data):
                fill_variations(page, data)
                if BotState.stop_requested: return
            fill_seo(page, data)
            if BotState.stop_requested: return
            fill_media(page, data)
            if BotState.stop_requested: return
            if not is_variable_product(data):
                fill_pricing(page, data)
            logger.info("Full fill done. Please review manually before saving.")
        elif cmd == "debug":
            debug_current_tab(page)
        elif cmd == "stop":
            BotState.stop_requested = True
            logger.info("Stop requested. Script remains running.")
        elif cmd == "quit":
            logger.info("Closing script.")
            os._exit(0)
    except Exception as e:
        logger.error("ERROR while running command:", exc_info=True)

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
        "<alt>+<shift>+j": lambda: on_hotkey("batch"),
        "<alt>+<shift>+f": lambda: on_hotkey("full"),
        "<alt>+<shift>+d": lambda: on_hotkey("debug"),
        "<alt>+<shift>+x": lambda: on_hotkey("stop"),
        "<alt>+<shift>+q": lambda: on_hotkey("quit"),
    })
    hotkeys.start()
    return hotkeys

def main():
    parser = argparse.ArgumentParser(description="Prockured product listing automation + batch JSON filler")
    parser.add_argument("--batch", type=str, default="", help="Run batch JSON filler immediately with this JSON path, then exit.")
    args = parser.parse_args()

    logger.info("========================================")
    logger.info("Made By Krishna Maheshwari (Refactored)")
    logger.info("========================================")
    logger.info("Alt + Shift + L  = Load clipboard data")
    logger.info("Alt + Shift + J  = Run Batch JSON Fill")
    logger.info("Alt + Shift + Q  = Quit")
    logger.info("========================================")

    hotkeys = start_hotkeys()
    with sync_playwright() as pw:
        browser = None
        browser, page = connect_page(pw, browser)
        logger.info(f"Connected to: {page.url}")

        if args.batch:
            report_dir = run_batch_json(page, Path(args.batch))
            logger.info(f"Batch run finished. Reports: {report_dir}")
            return

        logger.info("Script is running. Keep this terminal open.")
        while True:
            cmd = command_queue.get()
            try:
                browser, page = connect_page(pw, browser)
                logger.info(f"Using page: {page.url}")
            except Exception as e:
                logger.error(f"Could not reconnect to Prockured page: {e}")
                continue
            run_command(cmd, page)

if __name__ == "__main__":
    main()
