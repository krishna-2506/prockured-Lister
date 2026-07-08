from pathlib import Path
from datetime import datetime
from .logger import logger
from .models import BatchLogger, ProductData
from .parser import load_batch_json, fill_sections_for_item, batch_item_missing_fields, product_data_from_batch_item
from .config import DEFAULT_BATCH_REPORT_DIR, DEFAULT_BATCH_JSON
from .automation.basics import fill_admin_search, open_products_list, click_first_product_result, verify_sku_on_edit_page, fill_basics, select_category_for_batch, select_brand_for_batch
from .automation.attributes import fill_attributes
from .automation.variations import fill_variations
from .automation.seo import fill_seo
from .automation.media import fill_media
from .automation.pricing import fill_pricing
from .browser import click_tab
from .parser import is_variable_product
import traceback
import re

def click_update_product(page) -> bool:
    logger.info("Clicking Update Product...")
    patterns = [r"^\s*Update Product\s*$", r"Update\s+Product", r"^\s*Save\s*$", r"Save\s+Product"]
    for pat in patterns:
        try:
            btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
            if btn.count() > 0:
                btn.scroll_into_view_if_needed()
                btn.click(timeout=3500, force=True)
                page.wait_for_timeout(2500)
                return True
        except Exception as e:
            logger.debug(f"Button pat {pat} error: {e}", exc_info=True)
            
    try:
        clicked = page.evaluate(
            r"""() => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                const buttons = [...document.querySelectorAll('button, [role="button"]')]
                    .filter(visible)
                    .map(el => ({el, text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(), r:el.getBoundingClientRect()}))
                    .filter(o => /update product|save product|^save$/i.test(o.text))
                    .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
                if (!buttons.length) return false;
                buttons[0].el.scrollIntoView({block:'center', inline:'nearest'});
                buttons[0].el.click();
                return true;
            }"""
        )
        if clicked:
            page.wait_for_timeout(2500)
            return True
    except Exception as e:
        logger.debug(f"Fallback click update error: {e}", exc_info=True)
    return False

def write_csv_report(path: Path, rows: list, fieldnames: list):
    import csv
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except Exception as e:
        logger.error(f"Error writing report {path}: {e}")

def run_batch_json(page, batch_json_path: Path = DEFAULT_BATCH_JSON):
    batch_json_path = Path(batch_json_path)
    products = load_batch_json(batch_json_path)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    report_dir = DEFAULT_BATCH_REPORT_DIR / ts
    report_dir.mkdir(parents=True, exist_ok=True)
    batch_logger = BatchLogger(report_dir / "batch_log.txt")

    success_rows = []
    failed_rows = []
    missing_rows = []
    manual_rows = []

    batch_logger.write("========================================")
    batch_logger.write("Prockured Batch Admin Filler started")
    batch_logger.write(f"Batch JSON: {batch_json_path}")
    batch_logger.write(f"Products loaded: {len(products)}")
    batch_logger.write(f"Report folder: {report_dir}")
    batch_logger.write("========================================")

    for idx, item in enumerate(products, start=1):
        admin = item.get("admin") or {}
        row_id = str(admin.get("row_id") or idx)
        sku = str(admin.get("sku") or "").strip()
        category_option = str(admin.get("category_option") or "").strip()
        brand_option = str(admin.get("brand_option") or "").strip()
        sections = fill_sections_for_item(item)
        missing = batch_item_missing_fields(item)
        
        if missing:
            missing_rows.append({"row_id": row_id, "sku": sku, "missing_fields": " | ".join(missing)})

        batch_logger.write("")
        batch_logger.write(f"[{idx}/{len(products)}] Row {row_id} | SKU: {sku}")

        if not sku:
            reason = "Missing SKU"
            batch_logger.write(f"FAILED: {reason}")
            failed_rows.append({"row_id": row_id, "sku": sku, "reason": reason, "last_page_url": getattr(page, 'url', ''), "details": ""})
            continue
            
        if not category_option and "category" in sections:
            reason = "Missing category_option"
            batch_logger.write(f"FAILED: {reason}")
            failed_rows.append({"row_id": row_id, "sku": sku, "reason": reason, "last_page_url": getattr(page, 'url', ''), "details": "Category is mandatory."})
            continue

        try:
            open_products_list(page)
            batch_logger.write("Products list opened.")

            if not fill_admin_search(page, sku):
                reason = "Product search input not found or search failed"
                batch_logger.write(f"FAILED: {reason}")
                failed_rows.append({"row_id": row_id, "sku": sku, "reason": reason, "last_page_url": page.url, "details": ""})
                continue
                
            batch_logger.write("SKU searched. Waiting completed.")

            if not click_first_product_result(page, sku):
                reason = "SKU not found or product result could not be opened"
                batch_logger.write(f"FAILED: {reason}")
                failed_rows.append({"row_id": row_id, "sku": sku, "reason": reason, "last_page_url": page.url, "details": ""})
                continue
                
            batch_logger.write(f"Product opened: {page.url}")

            sku_ok, actual_sku = verify_sku_on_edit_page(page, sku)
            batch_logger.write(f"SKU check: expected={sku} | actual={actual_sku} | match={sku_ok}")
            if not sku_ok:
                reason = "SKU mismatch on edit page"
                failed_rows.append({"row_id": row_id, "sku": sku, "reason": reason, "last_page_url": page.url, "details": f"actual_sku={actual_sku}"})
                batch_logger.write(f"FAILED: {reason}")
                continue

            data = product_data_from_batch_item(item, update_product_name=("product_name" in sections))

            if "basics" in sections:
                fill_basics(page, data)
                batch_logger.write("Basics filled.")

            if "category" in sections:
                click_tab(page, "Basic")
                page.wait_for_timeout(500)
                if not select_category_for_batch(page, category_option):
                    reason = "Category option not selected"
                    failed_rows.append({"row_id": row_id, "sku": sku, "reason": reason, "last_page_url": page.url, "details": category_option})
                    batch_logger.write(f"FAILED: {reason}")
                    continue
                batch_logger.write(f"Category selected: {category_option}")

            if "brand" in sections:
                if brand_option:
                    brand_ok = select_brand_for_batch(page, brand_option)
                    if not brand_ok:
                        manual_rows.append({"row_id": row_id, "sku": sku, "issue": "Brand option not found", "suggested_action": f"Check/select brand manually: {brand_option}"})
                        batch_logger.write(f"WARNING: Brand not selected: {brand_option}")
                    else:
                        batch_logger.write(f"Brand selected: {brand_option}")

            attributes_ok = True
            if "attributes" in sections:
                attributes_ok = fill_attributes(page, data, one=False, clear=True)
                batch_logger.write(f"Attributes filled status: {attributes_ok}")
                if not attributes_ok:
                    manual_rows.append({"row_id": row_id, "sku": sku, "issue": "Attribute fill incomplete", "suggested_action": "Review Attributes manually."})

            if "variants" in sections or "variations" in sections:
                if attributes_ok and is_variable_product(data):
                    fill_variations(page, data)
                    batch_logger.write("Variations filled.")

            if "seo" in sections:
                fill_seo(page, data)
                batch_logger.write("SEO filled.")

            if "media" in sections:
                fill_media(page, data)
                batch_logger.write("Media filled.")

            if "pricing" in sections and not is_variable_product(data):
                fill_pricing(page, data)
                batch_logger.write("Pricing filled.")

            if not click_update_product(page):
                reason = "Update Product button not found or click failed"
                failed_rows.append({"row_id": row_id, "sku": sku, "reason": reason, "last_page_url": page.url, "details": ""})
                batch_logger.write(f"FAILED: {reason}")
                continue

            success_rows.append({
                "row_id": row_id,
                "sku": sku,
                "category_option": category_option,
                "brand_option": brand_option,
                "status": "updated",
                "message": "Product updated.",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            batch_logger.write("SUCCESS: Product updated.")

        except Exception as e:
            details = traceback.format_exc()
            failed_rows.append({"row_id": row_id, "sku": sku, "reason": str(e), "last_page_url": getattr(page, "url", ""), "details": details[-1500:]})
            batch_logger.write(f"FAILED unexpected error: {e}")
            batch_logger.write(details)
            continue

    write_csv_report(report_dir / "success_report.csv", success_rows, ["row_id", "sku", "category_option", "brand_option", "status", "message", "updated_at"])
    write_csv_report(report_dir / "failed_report.csv", failed_rows, ["row_id", "sku", "reason", "last_page_url", "details"])
    write_csv_report(report_dir / "missing_data_report.csv", missing_rows, ["row_id", "sku", "missing_fields"])
    write_csv_report(report_dir / "manual_review_report.csv", manual_rows, ["row_id", "sku", "issue", "suggested_action"])

    batch_logger.write("========================================")
    batch_logger.write("Batch completed")
    batch_logger.write(f"Success: {len(success_rows)}")
    batch_logger.write(f"Failed: {len(failed_rows)}")
    batch_logger.write("========================================")
    return report_dir
