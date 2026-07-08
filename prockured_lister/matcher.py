from pathlib import Path
import csv
from rapidfuzz import fuzz
from .config import DEFAULT_OUTPUT_DIR, DEFAULT_IMAGE_ROOT, IMAGE_EXTENSIONS
from .logger import logger
from .models import ProductData

def normalize_match_text(text: str) -> str:
    if not text:
        return ""
    text = str(text).lower()
    text = text.replace("ml", " ml").replace("gm", " gm").replace("kg", " kg")
    import re
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def simple_match_score(a: str, b: str) -> int:
    a_norm = normalize_match_text(a)
    b_norm = normalize_match_text(b)
    if not a_norm or not b_norm:
        return 0
    return round(fuzz.token_set_ratio(a_norm, b_norm))

def csv_candidate_title(row: dict) -> str:
    for k in ["matched_title", "Matched Title", "product_title", "Product Title", "Title", "title", "Product Name"]:
        if row.get(k):
            return str(row[k])
    return ""

def csv_candidate_price(row: dict) -> float | None:
    for k in ["price", "Price", "sale_price", "Sale Price"]:
        val = row.get(k)
        if val:
            from .parser import parse_price_value
            p = parse_price_value(val)
            if p is not None:
                return p
    return None

def scan_csv_rows(output_dir: Path):
    if not output_dir.exists():
        return []
    csvs = [p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() == ".csv"]
    rows = []
    for path in csvs:
        try:
            with path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows.extend(list(reader))
        except Exception as e:
            logger.debug(f"Could not read CSV {path}: {e}")
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
        match_text = f"{folder.parent.name} {folder.name}"
        score = simple_match_score(product_name, match_text)
        images = []
        try:
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                    images.append(p)
        except Exception as e:
            logger.debug(f"Failed to scan folder {folder}: {e}")
            continue
        if not images:
            continue
        if best is None or score > best["score"]:
            images = sorted(images, key=lambda x: x.name.lower())
            best = {"score": score, "matched_folder": str(folder), "paths": images}
    if best and best["score"] >= 45:
        return best
    return best

def get_product_name_for_matching(page, data: ProductData) -> str:
    # First priority: Batch JSON explicit name
    if data and data.basics and data.basics.get("__Batch Product Name"):
        return data.basics["__Batch Product Name"]
    # Fallback to general Product Name
    if data and data.basics and data.basics.get("Product Name"):
        return data.basics["Product Name"]
    # Read from page as last resort
    try:
        from .browser import click_tab
        click_tab(page, "Basic")
        page.wait_for_timeout(250)
        from .automation.basics import read_field_value_by_label
        val = read_field_value_by_label(page, ["Product Name"])
        if val: return val
    except Exception as e:
        logger.debug(f"Could not read product name from page: {e}")
    return ""

def find_best_images(product_name: str, data: ProductData):
    folder_raw = ""
    if data and data.media:
        folder_raw = data.media.get("Image Folder", "") or data.media.get("Folder", "") or ""
    if folder_raw:
        folder = Path(folder_raw)
        if folder.exists():
            paths = sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS], key=lambda x: x.name.lower())
            if paths:
                return {"source": "manual-folder", "score": 100, "matched": str(folder), "paths": paths}

    from_csv = collect_image_paths_from_csv(product_name, DEFAULT_OUTPUT_DIR)
    if from_csv and from_csv["paths"]:
        return {"source": "csv", "score": from_csv["score"], "matched": from_csv["matched_title"], "paths": from_csv["paths"]}

    from_folders = collect_image_paths_from_folders(product_name, DEFAULT_IMAGE_ROOT)
    if from_folders and from_folders.get("paths"):
        return {"source": "folder", "score": from_folders["score"], "matched": from_folders.get("matched_folder"), "paths": from_folders["paths"]}

    return None
