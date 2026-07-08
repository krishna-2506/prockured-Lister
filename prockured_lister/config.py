import os
from pathlib import Path

CDP_URL = "http://127.0.0.1:9222"

RUN_DIR = Path(os.environ.get("PROCKURED_RUN_DIR", Path.cwd())).resolve()
SCRIPT_DIR = Path(__file__).resolve().parent.parent

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
DEFAULT_IMAGE_ROOT = Path(os.environ.get(
    "PROCKURED_IMAGE_ROOT",
    str(_first_existing_path(RUN_DIR / "images", RUN_DIR / "prockured_output" / "images", DEFAULT_OUTPUT_DIR, SCRIPT_DIR / "images", SCRIPT_DIR / "prockured_output" / "images"))
))
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_MEDIA_UPLOADS = int(os.environ.get("PROCKURED_MAX_MEDIA_UPLOADS", "8"))
BASE_MARKUP_MIN = 10
BASE_MARKUP_MAX = 30

ADMIN_PRODUCTS_URL = os.environ.get("PROCKURED_ADMIN_PRODUCTS_URL", "https://store.prockured.com/admin/products")
DEFAULT_BATCH_JSON = Path(os.environ.get(
    "PROCKURED_BATCH_JSON",
    str(_first_existing_path(RUN_DIR / "batch_products.json", SCRIPT_DIR / "batch_products.json"))
))
DEFAULT_BATCH_REPORT_DIR = Path(os.environ.get("PROCKURED_BATCH_REPORT_DIR", str(RUN_DIR / "batch_reports")))

# Globals for state (could be moved to a state manager, but kept for hotkeys)
class BotState:
    stop_requested = False
    current_data = None
