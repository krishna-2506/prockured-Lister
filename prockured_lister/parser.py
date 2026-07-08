import re
import json
from pathlib import Path
from .models import ProductData, BASICS_KEYS, SEO_KEYS, MEDIA_KEYS, PRICING_KEYS
from .logger import logger

def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text

def norm_key(key: str) -> str:
    return re.sub(r"\s+", " ", (key or "").strip()).lower()

def display_label_from_key(key: str) -> str:
    parts = key.replace("_", " ").split()
    return " ".join(p.capitalize() for p in parts)

def parse_price_value(raw) -> float | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def parse_key_value_section(lines, known_keys):
    result_lines = {}
    current_key = None
    key_pattern = re.compile(r"^(.+?)\s*[:：]\s*(.*)$")
    known_norm = {norm_key(k): k for k in known_keys}
    preserve_blank_keys = {"Description"}

    for raw in lines:
        raw_line = raw.rstrip()
        stripped = raw_line.strip()

        if not stripped:
            if current_key in preserve_blank_keys:
                result_lines.setdefault(current_key, []).append("")
            continue

        m = key_pattern.match(stripped)
        if m:
            candidate_key = norm_key(m.group(1))
            if candidate_key in known_norm:
                current_key = known_norm[candidate_key]
                first_value = m.group(2).strip()
                result_lines[current_key] = [first_value] if first_value else []
                continue

        if current_key:
            if current_key in preserve_blank_keys:
                result_lines.setdefault(current_key, []).append(stripped)
            else:
                if result_lines.get(current_key):
                    result_lines[current_key][-1] = (result_lines[current_key][-1].rstrip() + " " + stripped).strip()
                else:
                    result_lines[current_key] = [stripped]

    result = {}
    for key, value_lines in result_lines.items():
        if key in preserve_blank_keys:
            value = "\n".join(value_lines).strip()
            value = re.sub(r"\n{3,}", "\n\n", value)
        else:
            value = " ".join([v.strip() for v in value_lines if v.strip()]).strip()
        if value:
            result[key] = value

    return result

def parse_attributes(lines):
    attrs = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\u2022]\s*", "", line)
        if ":" not in line and "：" not in line:
            continue
        if "：" in line and ":" not in line:
            name, value = line.split("：", 1)
        else:
            name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name and value:
            attrs.append((name, value))
    return attrs

def split_variant_values(value: str) -> list:
    if value is None:
        return []
    parts = [p.strip() for p in re.split(r"\s*,\s*", str(value)) if p.strip()]
    return parts or [str(value).strip()]

def parse_variant_pricing(lines):
    rows = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\u2022]\s*", "", line)
        parts = [p.strip() for p in line.split("|") if p.strip()]
        attrs = {}
        price_raw = ""
        price = None
        for part in parts:
            if ":" not in part and "：" not in part:
                continue
            if "：" in part and ":" not in part:
                k, v = part.split("：", 1)
            else:
                k, v = part.split(":", 1)
            key = k.strip()
            val = v.strip()
            if not key or not val:
                continue
            if norm_key(key) in {"price", "sale price", "discount price", "actual price"}:
                price_raw = val
                price = parse_price_value(val)
            else:
                attrs[key] = val
        if attrs:
            rows.append({"attributes": attrs, "price_raw": price_raw, "price": price})
    return rows

def is_variable_product(data: ProductData) -> bool:
    pt = norm_key((data.basics or {}).get("Product Type", ""))
    return "variable" in pt or bool(data.variant_attributes) or bool(data.variant_pricing)

def parse_clipboard_text(text: str) -> ProductData:
    data = ProductData()
    sections = {"BASICS": [], "ATTRIBUTES": [], "VARIANT ATTRIBUTES": [], "VARIANT PRICING": [], "SEO": [], "MEDIA": [], "PRICING": []}
    current = None

    for raw in text.splitlines():
        s = raw.strip()
        upper = s.upper()
        if upper in ["[BASICS]", "BASICS"]:
            current = "BASICS"
            continue
        if upper in ["[ATTRIBUTES]", "ATTRIBUTES"]:
            current = "ATTRIBUTES"
            continue
        if upper in ["[VARIANT ATTRIBUTES]", "VARIANT ATTRIBUTES"]:
            current = "VARIANT ATTRIBUTES"
            continue
        if upper in ["[VARIANT PRICING]", "VARIANT PRICING"]:
            current = "VARIANT PRICING"
            continue
        if upper in ["[SEO]", "SEO"]:
            current = "SEO"
            continue
        if upper in ["[MEDIA]", "MEDIA"]:
            current = "MEDIA"
            continue
        if upper in ["[PRICING]", "PRICING"]:
            current = "PRICING"
            continue
        if current and s:
            sections[current].append(raw)
            
    data.basics = parse_key_value_section(sections["BASICS"], BASICS_KEYS)
    data.attributes = parse_attributes(sections["ATTRIBUTES"])
    data.variant_attributes = parse_attributes(sections["VARIANT ATTRIBUTES"])
    data.variant_pricing = parse_variant_pricing(sections["VARIANT PRICING"])
    data.seo = parse_key_value_section(sections["SEO"], SEO_KEYS)
    data.media = parse_key_value_section(sections["MEDIA"], MEDIA_KEYS)
    data.pricing = parse_key_value_section(sections["PRICING"], PRICING_KEYS)
    return data

# Batch JSON methods
def load_batch_json(path: Path) -> list:
    if not path.exists():
        raise FileNotFoundError(f"Batch JSON not found: {path}")

    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        raise ValueError(f"Batch JSON file is empty: {path}")

    data = json.loads(raw)

    if isinstance(data, dict):
        for key in ("products", "batch_products", "items", "data"):
            if key in data:
                data = data[key]
                break
        else:
            if any(k in data for k in ("admin", "basics", "attributes", "seo", "pricing")):
                data = [data]

    if not isinstance(data, list):
        raise ValueError(
            "Batch JSON must be a list of products, or an object with a products/batch_products/items/data list."
        )

    cleaned = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Batch product #{i} must be an object/dict, got {type(item).__name__}.")
        cleaned.append(item)

    return cleaned

def value_is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False

def item_get(item: dict, section: str, key: str, default=""):
    sec = item.get(section) or {}
    val = sec.get(key, default)
    if val is None:
        return default
    return val

def batch_item_missing_fields(item: dict) -> list:
    missing = []
    hard_fields = [
        ("admin", "sku"),
        ("admin", "category_option"),
    ]
    soft_fields = [
        ("basics", "description"),
        ("basics", "short_description"),
        ("seo", "seo_title"),
        ("seo", "seo_description"),
        ("seo", "seo_keywords"),
    ]
    for section, key in hard_fields + soft_fields:
        if value_is_blank(item_get(item, section, key, "")):
            missing.append(f"{section}.{key}")
    attrs = item.get("attributes") or {}
    if not any(not value_is_blank(v) for v in attrs.values()):
        missing.append("attributes")
    return missing

def fill_sections_for_item(item: dict) -> set:
    admin = item.get("admin") or {}
    pricing = item.get("pricing") or {}
    sections = admin.get("fill_sections")
    if isinstance(sections, str):
        out = {s.strip().lower() for s in re.split(r"[,|]", sections) if s.strip()}
    elif isinstance(sections, list):
        out = {str(s).strip().lower() for s in sections if str(s).strip()}
    else:
        out = {"category", "brand", "basics", "attributes", "seo"}

    if not bool(admin.get("skip_media")):
        out.add("media")
    if not value_is_blank(pricing.get("sale_price")) and not bool(admin.get("skip_pricing")):
        out.add("pricing")
    return out

def product_data_from_batch_item(item: dict, update_product_name: bool = False) -> ProductData:
    data = ProductData()
    basics = item.get("basics") or {}
    seo = item.get("seo") or {}
    pricing = item.get("pricing") or {}
    attrs = item.get("attributes") or {}

    if not value_is_blank(basics.get("product_name")):
        product_name_value = str(basics.get("product_name")).strip()
        data.basics["Product Name"] = product_name_value
        data.basics["__Batch Product Name"] = product_name_value
    if not value_is_blank(basics.get("product_type")):
        data.basics["Product Type"] = str(basics.get("product_type")).strip()
    if not value_is_blank(basics.get("product_tags")):
        data.basics["Product Tags"] = str(basics.get("product_tags")).strip()
    if not value_is_blank(basics.get("description")):
        data.basics["Description"] = str(basics.get("description")).strip()
    if not value_is_blank(basics.get("short_description")):
        data.basics["Short Description"] = str(basics.get("short_description")).strip()

    for key, value in attrs.items():
        if value_is_blank(value):
            continue
        data.attributes.append((display_label_from_key(key), str(value).strip()))

    vattrs = item.get("variant_attributes") or {}
    if isinstance(vattrs, dict):
        for key, value in vattrs.items():
            if value_is_blank(value):
                continue
            if isinstance(value, list):
                value = ", ".join(str(v).strip() for v in value if not value_is_blank(v))
            data.variant_attributes.append((display_label_from_key(key), str(value).strip()))

    vpricing = item.get("variant_pricing") or []
    if isinstance(vpricing, list):
        for row in vpricing:
            if not isinstance(row, dict):
                continue
            attrs_row = row.get("attributes") or {k: v for k, v in row.items() if k not in {"price", "sale_price", "price_raw"}}
            price_val = row.get("price", row.get("sale_price", row.get("price_raw", "")))
            data.variant_pricing.append({"attributes": attrs_row, "price_raw": str(price_val), "price": parse_price_value(price_val)})

    if not value_is_blank(seo.get("seo_title")):
        data.seo["SEO Title"] = str(seo.get("seo_title")).strip()
    if not value_is_blank(seo.get("seo_description")):
        data.seo["SEO Description"] = str(seo.get("seo_description")).strip()
    if not value_is_blank(seo.get("seo_keywords")):
        data.seo["SEO Keywords"] = str(seo.get("seo_keywords")).strip()

    if not value_is_blank(pricing.get("sale_price")):
        data.pricing["Sale Price"] = str(pricing.get("sale_price")).strip()

    return data
