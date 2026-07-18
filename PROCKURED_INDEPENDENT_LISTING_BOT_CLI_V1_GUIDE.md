# Prockured Independent Listing Bot — CLI V1

**Script:** `independent_listing_bot_cli_v1.py`  
**Version:** CLI V1  
**Purpose:** Update existing products in the Prockured admin using batch JSON, command-line section controls, media matching, four attribute classifications, variation generation, save verification, reporting, resume support, and optional interactive hotkeys.

---

## Table of Contents

1. [Core Rules](#1-core-rules)
2. [What the Bot Can Update](#2-what-the-bot-can-update)
3. [System Requirements](#3-system-requirements)
4. [Recommended Folder Structure](#4-recommended-folder-structure)
5. [Preparing the Browser](#5-preparing-the-browser)
6. [Quick Start](#6-quick-start)
7. [CLI Command Reference](#7-cli-command-reference)
8. [How Section Selection Works](#8-how-section-selection-works)
9. [Product Lookup and Verification](#9-product-lookup-and-verification)
10. [Batch JSON Formats](#10-batch-json-formats)
11. [Complete JSON Example](#11-complete-json-example)
12. [JSON Field Reference](#12-json-field-reference)
13. [Four Attribute Sections](#13-four-attribute-sections)
14. [Variation Workflow](#14-variation-workflow)
15. [Pricing Workflow](#15-pricing-workflow)
16. [Media Matching Workflow](#16-media-matching-workflow)
17. [MediaSort Strategies](#17-mediasort-strategies)
18. [Media Scoring System](#18-media-scoring-system)
19. [Media Upload Behaviour](#19-media-upload-behaviour)
20. [Resume Workflow](#20-resume-workflow)
21. [Save and Redirect Verification](#21-save-and-redirect-verification)
22. [Reports](#22-reports)
23. [Interactive Clipboard Mode](#23-interactive-clipboard-mode)
24. [Hotkeys](#24-hotkeys)
25. [Environment Variables](#25-environment-variables)
26. [Terminal Startup Information](#26-terminal-startup-information)
27. [End-to-End Batch Workflow](#27-end-to-end-batch-workflow)
28. [Recommended Commands](#28-recommended-commands)
29. [Troubleshooting](#29-troubleshooting)
30. [Operational Recommendations](#30-operational-recommendations)
31. [Current V1 Limitations and Safety Notes](#31-current-v1-limitations-and-safety-notes)

---

# 1. Core Rules

The CLI V1 follows these rules in every batch run:

1. **Category is mandatory.**
   - The Prockured admin does not save a product unless a category is selected.
   - Category runs automatically even when only `--media`, `--seo`, `--pricing`, or another single section is requested.
   - Every batch product must include `admin.category_option`.

2. **SKU lookup is the default.**
   - When `--lookup` is not supplied, the bot searches and verifies using the full SKU.
   - Model Number lookup is used only when the command explicitly includes `--lookup modelnumber`.

3. **No section flags means run all sections.**
   - A normal `--batch` command performs the full listing workflow.
   - When at least one section flag is supplied, only those named sections run, plus the mandatory Category step.

4. **`--variation` has dependencies.**
   - It automatically includes attribute processing.
   - It sets Product Type to `Variable Product`.
   - It requires at least one entry in `variant_attributes` or `variant_filter_attributes`.

5. **All changes are made to existing products.**
   - The bot searches the Products page, opens an existing product, verifies it, edits it, and saves it.
   - It does not create new products.

6. **Saving is confirmed by redirect.**
   - After Update Product is clicked, the bot waits up to 60 seconds for the admin to redirect to the Products list.
   - If the redirect does not happen, save status is treated as unconfirmed.

---

# 2. What the Bot Can Update

The batch CLI can update:

- Basics
  - Product Type
  - Product Name
  - Product Tags
  - Description
  - Short Description
  - Model Name
- Mandatory Category
- Brand
- Four kinds of attributes
- Variant generation
- Variant pricing
- Simple-product pricing
- SEO fields
- Product Media images
- Product save and redirect confirmation

The script also provides:

- SKU lookup
- Model Number lookup
- Edit-page verification
- Scored image-folder matching
- Resume from an earlier report
- Success, failure, missing-data, manual-review, and resume reports
- Interactive clipboard mode with hotkeys

---

# 3. System Requirements

## 3.1 Python

Recommended:

```text
Python 3.10 or newer
```

The script uses modern type hints such as `Path | None`, so older Python versions are not recommended.

## 3.2 Python packages

Install the required packages:

```bash
pip install playwright pyperclip pynput
```

Install Playwright browser support:

```bash
playwright install chromium
```

`pynput` is required only for interactive hotkey mode. Immediate `--batch` mode does not start the hotkey listener.

## 3.3 Browser requirement

The script connects to an already-running Chromium-based browser through:

```text
http://127.0.0.1:9222
```

Brave, Chrome, or another Chromium browser must be started with remote debugging enabled.

## 3.4 Prockured admin session

Before starting the bot:

1. Open the remote-debugging browser.
2. Log in to the Prockured admin.
3. Open the Products page or an Edit Product page.
4. Keep the browser and terminal open throughout the run.

---

# 4. Recommended Folder Structure

The current working directory is important because it becomes `RUN_DIR`.

Recommended structure:

```text
listing_bot\
├── independent_listing_bot_cli_v1.py
├── products.json
├── images\
│   ├── MODEL-001\
│   │   ├── 01-main.jpg
│   │   └── 02-side.jpg
│   └── MODEL-002\
│       ├── 01-main.webp
│       └── 02-lifestyle.webp
└── batch_reports\
```

By default:

```text
Batch JSON:       <RUN_DIR>\batch_products.json
Images root:      <RUN_DIR>\images
Reports root:     <RUN_DIR>\batch_reports
Output/CSV scan:  <RUN_DIR> or the script directory
```

Run the command from the folder whose `images` and `batch_reports` directories you want the script to use.

Example:

```bat
cd C:\Users\krish\Downloads\listing_bot
python independent_listing_bot_cli_v1.py --batch products.json
```

---

# 5. Preparing the Browser

## 5.1 Brave example on Windows

Close any automation copy of Brave first, then start it with remote debugging:

```bat
"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="C:\prockured-brave-automation"
```

The executable path may differ on your computer.

## 5.2 Chrome example on Windows

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="C:\prockured-chrome-automation"
```

## 5.3 Why a separate browser profile is recommended

A separate `--user-data-dir`:

- Avoids profile-locking problems.
- Keeps the automation login separate from normal browsing.
- Reduces the chance of the bot connecting to the wrong browser window.

## 5.4 Required open page

Open:

```text
https://store.prockured.com/admin/products
```

The script scans connected pages and selects the best live Prockured admin page.

---

# 6. Quick Start

## 6.1 Full batch using SKU lookup

```bash
python independent_listing_bot_cli_v1.py --batch products.json
```

This runs all sections and uses SKU lookup.

## 6.2 Media-only update

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media
```

This runs:

```text
Product lookup → Product verification → Category → Media → Save
```

## 6.3 Attribute-only update

```bash
python independent_listing_bot_cli_v1.py --batch products.json --attributes
```

This processes all four JSON attribute sections, selects Category, and saves.

## 6.4 Model Number lookup

```bash
python independent_listing_bot_cli_v1.py --batch products.json --lookup modelnumber
```

This is required when SKU is blank and the product must be located using Model Number.

## 6.5 Variation workflow

```bash
python independent_listing_bot_cli_v1.py --batch products.json --variation
```

This automatically runs:

```text
Category
→ Product Type = Variable Product
→ Attributes
→ Filters/Variants checkboxes
→ Generate Variants
→ Variant pricing
→ Save
```

---

# 7. CLI Command Reference

## 7.1 Main batch argument

### `--batch FILE`

Runs the specified batch JSON immediately.

```bash
python independent_listing_bot_cli_v1.py --batch products.json
```

When `--batch` is present, hotkeys are not started.

---

## 7.2 Section flags

### `--all`

Runs all sections.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --all
```

This is equivalent to supplying no section flags.

### `--basics`

Updates Basics fields.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --basics
```

### `--brand`

Selects Brand.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --brand
```

Category still runs automatically.

### `--attributes`

Processes all four attribute sections:

```text
attributes
filter_attributes
variant_attributes
variant_filter_attributes
```

```bash
python independent_listing_bot_cli_v1.py --batch products.json --attributes
```

### `--variation` / `--variations`

Sets Product Type to Variable Product, processes attributes, generates combinations, and fills variant prices.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --variation
```

### `--pricing`

Updates simple-product Base Price and Discount Price.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --pricing
```

Pricing is skipped when the normalized product is variable.

### `--seo`

Updates SEO Title, SEO Description, and SEO Keywords.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --seo
```

### `--media`

Matches and uploads Product Media images.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media
```

---

## 7.3 Lookup arguments

### `--lookup sku`

Default lookup mode.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --lookup sku
```

The product must contain `admin.sku`.

### `--lookup modelnumber`

Uses Model Number as the search and verification identity.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --lookup modelnumber
```

The product must contain a Model Number in one of the supported locations described later.

---

## 7.4 Media arguments

### `--forceMedia PATH`

Overrides the default media root.

Aliases:

```text
--forceMedia
--force-media
```

Example:

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media --forceMedia "D:\Product Images"
```

A relative path is resolved from `RUN_DIR`.

### `--MediaSort STRATEGY`

Chooses the identity or hierarchy used to match image folders.

Aliases:

```text
--MediaSort
--media-sort
```

Example:

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media --MediaSort brand/modelnumber
```

### `--mediaMinScore NUMBER`

Sets the minimum accepted scored-folder result.

Aliases:

```text
--mediaMinScore
--media-min-score
```

Default:

```text
85
```

Example:

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media --mediaMinScore 90
```

---

## 7.5 Resume argument

### `--resume PATH`

Skips products already marked successful in a previous report.

```bash
python independent_listing_bot_cli_v1.py --batch products.json --resume "batch_reports\2026-07-17_120000\success_report.csv"
```

A report folder may also be supplied:

```bash
python independent_listing_bot_cli_v1.py --batch products.json --resume "batch_reports\2026-07-17_120000"
```

---

## 7.6 Version

### `--version`

```bash
python independent_listing_bot_cli_v1.py --version
```

Output:

```text
Prockured Listing Bot CLI V1
```

---

# 8. How Section Selection Works

## 8.1 No section flags

Command:

```bash
python independent_listing_bot_cli_v1.py --batch products.json
```

Resolved section set:

```text
category
brand
basics
attributes
variations
pricing
seo
media
```

Variation processing only runs for products that actually contain variant attributes.

## 8.2 One or more section flags

Command:

```bash
python independent_listing_bot_cli_v1.py --batch products.json --basics --seo
```

Resolved sections:

```text
category
basics
seo
```

No other content section is changed.

## 8.3 Category is always added

There is no separate `--category` flag in CLI V1 because Category is unconditional.

Examples:

```text
--media       → category + media
--seo         → category + seo
--pricing     → category + pricing
--brand       → category + brand
--attributes  → category + attributes
```

## 8.4 Product-level skip controls

Inside `admin`:

```json
{
  "skip_media": true,
  "skip_pricing": true
}
```

These remove Media or Pricing for that product even if the section was selected globally.

---

# 9. Product Lookup and Verification

# 9.1 SKU lookup

SKU is the default.

Required JSON:

```json
{
  "admin": {
    "sku": "198202DURALEX1029BB"
  }
}
```

Workflow:

1. Open Products list.
2. Search using the SKU.
3. Open the matching result.
4. Open the Edit Product page.
5. Read the SKU on the edit page.
6. Normalize spaces and formatting.
7. Require the expected and actual SKU to match.
8. Begin editing only after verification succeeds.

# 9.2 Model Number lookup

Model lookup is used only with:

```bash
--lookup modelnumber
```

Supported Model Number keys:

```text
model_name
model
model_no
model_number
modelnumber
model_code
```

Supported locations:

```text
Top level
admin
basics
attributes (legacy support)
```

Example:

```json
{
  "admin": {
    "sku": "",
    "model_number": "1029BB"
  }
}
```

Model lookup workflow:

1. Search Products using `1029BB`.
2. Inspect filtered results.
3. Find a row whose SKU contains the normalized Model Number.
4. Open that product’s Edit action.
5. Verify Model Name on the edit page when available.
6. Fall back to checking whether the Model Number is contained in the edit-page SKU.

Example:

```text
Expected Model Number: 1029BB
Actual SKU: 198202DURALEX1029BB
Verification: Match
```

No brand marker such as `DURALEX` is required by the general Model Number matching rule.

# 9.3 Missing lookup identity

The batch product fails immediately when:

- SKU mode is selected and `admin.sku` is blank.
- Model Number mode is selected and no supported Model Number value is present.

# 9.4 Why edit-page verification matters

A successful click is not enough. The script verifies the opened product before changing any fields, reducing the chance of updating the wrong product.

---

# 10. Batch JSON Formats

The loader accepts all of the following.

## 10.1 Direct product list

```json
[
  {
    "admin": {},
    "basics": {}
  }
]
```

## 10.2 Package with `products`

```json
{
  "config": {},
  "products": [
    {
      "admin": {},
      "basics": {}
    }
  ]
}
```

## 10.3 Alternative list keys

These are also accepted:

```text
batch_products
items
data
```

Example:

```json
{
  "items": []
}
```

## 10.4 Single product object

A single object is accepted when it contains product sections such as:

```text
admin
basics
attributes
seo
pricing
```

Example:

```json
{
  "admin": {
    "sku": "ABC123",
    "category_option": "Drinking Glasses (Tableware & Serveware)"
  },
  "basics": {
    "product_name": "Example Product"
  }
}
```

---

# 11. Complete JSON Example

```json
{
  "config": {
    "media": {
      "root": "images",
      "sort": "brand/modelnumber",
      "min_score": 85
    }
  },
  "products": [
    {
      "admin": {
        "row_id": "DUR1029BB",
        "sku": "198202DURALEX1029BB",
        "model_number": "1029BB",
        "category_option": "Drinking Glasses (Tableware & Serveware)",
        "brand_option": "Duralex",
        "skip_media": false,
        "skip_pricing": false
      },
      "basics": {
        "product_type": "Simple Product",
        "product_name": "Duralex Picardie Marine Highball Tumbler 360 ML",
        "product_tags": "duralex tumbler, picardie glass, highball glass",
        "description": "Duralex Picardie Marine Highball Tumbler 360 ML is made for commercial beverage service.\n\nIt is suitable for restaurants, cafés, hotels and catering operations.",
        "short_description": "Duralex Picardie Marine Highball Tumbler 360 ML is a commercial drinking glass for water, juice and beverage service."
      },
      "attributes": {
        "material": "Glass",
        "usage": "Hotels, Restaurants, Cafes and Catering"
      },
      "filter_attributes": {
        "colour": "Clear"
      },
      "variant_attributes": {},
      "variant_filter_attributes": {},
      "seo": {
        "seo_title": "Duralex Picardie Marine Highball Tumbler 360 ML",
        "seo_description": "Buy Duralex Picardie Marine Highball Tumbler 360 ML for professional beverage service in hotels, restaurants, cafes and catering operations.",
        "seo_keywords": "Duralex tumbler, highball glass, 360 ML drinking glass"
      },
      "pricing": {
        "sale_price": "250"
      },
      "media": {
        "sort": "brand/modelnumber",
        "min_score": 90,
        "replace_existing_images": false
      }
    }
  ]
}
```

---

# 12. JSON Field Reference

# 12.1 `config`

Batch-wide configuration.

```json
{
  "config": {
    "media": {
      "root": "images",
      "sort": "modelnumber",
      "min_score": 85
    }
  }
}
```

Media settings may also be placed at the top level in supported aliases, but the nested `config.media` format is the recommended structure.

# 12.2 `admin`

Recommended fields:

```json
{
  "admin": {
    "row_id": "ROW-001",
    "sku": "FULLSKU123",
    "model_number": "MODEL123",
    "category_option": "Sub-Category (Parent Category)",
    "brand_option": "Brand Name",
    "skip_media": false,
    "skip_pricing": false
  }
}
```

| Field | Purpose |
|---|---|
| `row_id` | Internal batch/report identity. Defaults to product position when blank. |
| `sku` | Required for default SKU lookup. |
| `model_number` | Used for model lookup and media matching. Aliases are supported. |
| `category_option` | Mandatory for every saved product. |
| `brand_option` | Brand dropdown option. |
| `skip_media` | Skips Media for this product. |
| `skip_pricing` | Skips Pricing for this product. |

# 12.3 `basics`

```json
{
  "basics": {
    "product_type": "Simple Product",
    "product_name": "Product Name",
    "product_tags": "tag one, tag two",
    "description": "Long description",
    "short_description": "Short description",
    "model_number": "MODEL123"
  }
}
```

`model_number` may be stored under `admin`, `basics`, top level, or legacy `attributes`. It is normalized into the dedicated Model Name field and excluded from normal attribute cards.

# 12.4 `attributes`

Normal attributes. Neither checkbox is enabled.

```json
{
  "attributes": {
    "material": "Glass",
    "usage": "Restaurants and Cafes"
  }
}
```

# 12.5 `filter_attributes`

Attributes whose **Filters** checkbox must be enabled.

```json
{
  "filter_attributes": {
    "colour": "Clear",
    "capacity": "360 ML"
  }
}
```

# 12.6 `variant_attributes`

Attributes whose **Variants** checkbox must be enabled.

```json
{
  "variant_attributes": {
    "packing_type": [
      "Single Piece",
      "Pack of 6",
      "Pack of 12"
    ]
  }
}
```

A comma-separated string is also supported:

```json
{
  "variant_attributes": {
    "packing_type": "Single Piece, Pack of 6, Pack of 12"
  }
}
```

# 12.7 `variant_filter_attributes`

Attributes whose **Filters** and **Variants** checkboxes must both be enabled.

```json
{
  "variant_filter_attributes": {
    "capacity": [
      "250 ML",
      "310 ML",
      "360 ML"
    ]
  }
}
```

# 12.8 `variant_pricing`

Each row maps an attribute combination to a price.

Recommended form:

```json
{
  "variant_pricing": [
    {
      "attributes": {
        "Capacity": "250 ML",
        "Packing Type": "Pack of 6"
      },
      "price": "1500"
    },
    {
      "attributes": {
        "Capacity": "360 ML",
        "Packing Type": "Pack of 6"
      },
      "price": "1800"
    }
  ]
}
```

Compact row form is also accepted:

```json
{
  "variant_pricing": [
    {
      "Capacity": "250 ML",
      "Packing Type": "Pack of 6",
      "sale_price": "1500"
    }
  ]
}
```

Accepted price keys:

```text
price
sale_price
price_raw
```

# 12.9 `seo`

```json
{
  "seo": {
    "seo_title": "SEO Title",
    "seo_description": "SEO Description",
    "seo_keywords": "keyword one, keyword two"
  }
}
```

# 12.10 `pricing`

```json
{
  "pricing": {
    "sale_price": "250"
  }
}
```

# 12.11 `media`

```json
{
  "media": {
    "root": "images",
    "folder": "MODEL123",
    "sort": "modelnumber",
    "min_score": 85,
    "replace_existing_images": false
  }
}
```

| Field | Purpose |
|---|---|
| `root` | Product-level media root override. |
| `folder` | Explicit product image folder. Highest-confidence product setting. |
| `sort` | MediaSort strategy. |
| `min_score` | Minimum scored-folder match. |
| `replace_existing_images` | Requests deletion of existing media before upload. Use carefully. |

---

# 13. Four Attribute Sections

The V1 attribute model supports four states.

| JSON section | Filters | Variants |
|---|---:|---:|
| `attributes` | Off | Off |
| `filter_attributes` | On | Off |
| `variant_attributes` | Off | On |
| `variant_filter_attributes` | On | On |

## 13.1 Normal attributes

Example:

```json
{
  "attributes": {
    "Material": "Glass"
  }
}
```

Result:

```text
Material: Glass
Filters: Off
Variants: Off
```

## 13.2 Filter attributes

Example:

```json
{
  "filter_attributes": {
    "Colour": "Clear"
  }
}
```

Result:

```text
Colour: Clear
Filters: On
Variants: Off
```

## 13.3 Variant attributes

Example:

```json
{
  "variant_attributes": {
    "Packing Type": ["Single Piece", "Pack of 6"]
  }
}
```

Result:

```text
Packing Type: Single Piece, Pack of 6
Filters: Off
Variants: On
```

## 13.4 Variant + Filter attributes

Example:

```json
{
  "variant_filter_attributes": {
    "Capacity": ["250 ML", "360 ML"]
  }
}
```

Result:

```text
Capacity: 250 ML, 360 ML
Filters: On
Variants: On
```

## 13.5 Duplicate merging

If the same attribute appears in more than one section, it is merged into one card.

Example input:

```json
{
  "filter_attributes": {
    "Capacity": "250 ML"
  },
  "variant_attributes": {
    "Capacity": ["250 ML", "360 ML"]
  }
}
```

Merged result:

```text
Capacity: 250 ML, 360 ML
Filters: On
Variants: On
```

Values are deduplicated using normalized text.

## 13.6 Attribute processing order

The script merges all four sections, then fills the final attribute cards. Existing attribute cards are cleared during a full attribute update.

Workflow:

```text
Open Attributes
→ Clear existing cards
→ Create merged cards
→ Enter values
→ Set Filters state
→ Set Variants state
→ Remove empty cards
→ Verify checkbox classifications
```

---

# 14. Variation Workflow

Variation mode is requested with:

```bash
--variation
```

## 14.1 Required JSON

At least one of these must contain data:

```text
variant_attributes
variant_filter_attributes
```

If both are empty, the product fails before editing with:

```text
--variation requested but no variant_attributes or variant_filter_attributes were supplied
```

## 14.2 Automatic dependencies

`--variation` automatically adds:

```text
attributes
variations
category
```

## 14.3 Product Type

The bot sets:

```text
Product Type = Variable Product
```

This happens even when `--basics` was not explicitly requested.

## 14.4 Variation sequence

```text
Open product
→ Verify product
→ Set Variable Product
→ Select mandatory Category
→ Recreate attributes
→ Enable Filters/Variants as classified
→ Open Variations/Catalog
→ Generate Variants
→ Match variant cards to variant_pricing
→ Fill Base Price and Sale Price
→ Preserve Active checkbox state
→ Save
```

## 14.5 Active checkbox rule

CLI V1 does **not** mark matched or unmatched variant cards Active or Inactive.

It preserves the existing Active checkbox state and only fills prices for matched combinations.

## 14.6 Unmatched combinations

Generated combinations without a matching `variant_pricing` row are left unpriced. Their Active state is not changed.

---

# 15. Pricing Workflow

# 15.1 Simple products

For simple products, the script uses `pricing.sale_price` as the Discount Price.

It calculates a higher Base Price using a stable markup derived from the product name.

Actual configured range:

```text
10% to 30%
```

The same product name receives the same markup on future runs because the markup is derived from an MD5 hash of the normalized name.

Example:

```text
Input Sale Price: 1000
Stable markup: approximately 10%–30%
Base Price: calculated above 1000
Discount Price: 1000
```

Both values are rounded upward to whole numbers.

# 15.2 CSV price fallback

When the loaded product has no pricing value, the script may search nearby CSV files for a fuzzy Product Name match.

It scans preferred files such as:

```text
hyperpure_prices.csv
summary.csv
all_images.csv
```

and then other CSV files under the configured output directory.

# 15.3 Variable products

Simple Pricing is skipped when the product is identified as variable.

Variant prices are handled in the Variations tab using `variant_pricing`.

# 15.4 Important price-format rule in V1

Use prices without thousands separators.

Correct:

```json
{
  "sale_price": "17100"
}
```

Avoid:

```json
{
  "sale_price": "17,100"
}
```

The current V1 parser removes the comma by replacing it with a space and then reads the first numeric segment. Therefore `17,100` can be interpreted incorrectly as `17`.

Also prefer:

```text
17100
17100.00
₹17100
```

over comma-formatted values.

---

# 16. Media Matching Workflow

The media system has no hardcoded Duralex directory.

Default media root:

```text
<RUN_DIR>\images
```

## 16.1 Media configuration precedence

For Media root:

```text
1. CLI --forceMedia
2. Product media.root
3. Batch config.media.root
4. Batch-level media_root / forceMedia alias
5. RUN_DIR\images
```

For MediaSort:

```text
1. CLI --MediaSort
2. Product media.sort
3. Batch config.media.sort
4. Batch-level MediaSort alias
5. modelnumber
```

For minimum score:

```text
1. CLI --mediaMinScore
2. Product media.min_score
3. Batch config.media.min_score
4. Batch-level mediaMinScore alias
5. 85
```

## 16.2 Explicit product folder

A product may specify:

```json
{
  "media": {
    "folder": "1029BB"
  }
}
```

When relative, it is resolved under the selected media root.

Example:

```text
Media root: D:\Product Images
Folder: 1029BB
Resolved: D:\Product Images\1029BB
```

An absolute folder remains absolute.

If it exists and contains supported images, it receives score 100.

## 16.3 Exact hierarchy check

For:

```text
MediaSort = brand/modelnumber
Brand = Duralex
Model Number = 1029BB
```

The bot first checks:

```text
<media root>\Duralex\1029BB
```

Folder comparison supports normalized, case-insensitive child matching.

## 16.4 Scored fallback

When the exact hierarchy is not found, the bot recursively scans folders containing supported images and assigns each one a score.

Only a safe, non-ambiguous result above the configured minimum is accepted.

---

# 17. MediaSort Strategies

Supported canonical properties:

```text
brand
modelnumber
sku
name
```

Supported aliases include:

```text
model
modelname
model_name
model_number
title
productname
product_name
```

Separators accepted in a hierarchy:

```text
/
|
,
>
```

The recommended separator is `/`.

## 17.1 `modelnumber`

```bash
--MediaSort modelnumber
```

Expected folder example:

```text
images\1029BB
```

## 17.2 `sku`

```bash
--MediaSort sku
```

Expected folder example:

```text
images\198202DURALEX1029BB
```

## 17.3 `name`

```bash
--MediaSort name
```

`name` means Brand + Product Name when the Product Name does not already start with the Brand.

Example identity:

```text
Duralex Picardie Marine Highball Tumbler 360 ML
```

## 17.4 `brand/modelnumber`

```bash
--MediaSort brand/modelnumber
```

Expected hierarchy:

```text
images\Duralex\1029BB
```

## 17.5 `brand/sku`

```bash
--MediaSort brand/sku
```

Expected hierarchy:

```text
images\Duralex\198202DURALEX1029BB
```

## 17.6 `brand/name`

```bash
--MediaSort brand/name
```

Expected hierarchy:

```text
images\Duralex\Duralex Picardie Marine Highball Tumbler 360 ML
```

## 17.7 Missing property behaviour

If a selected strategy requires data that is missing, Media is not changed.

Example:

```text
MediaSort: brand/modelnumber
Missing: brand
Result: No media upload
```

---

# 18. Media Scoring System

# 18.1 Exact scores

The following receive score 100:

- Explicit valid `media.folder`.
- Exact expected hierarchy.
- Exact normalized tail hierarchy.

If the final expected property appears exactly as a normalized folder component, the score is raised to at least 95.

# 18.2 Similarity calculation

The base text matcher combines:

- Sequence similarity: 55%
- Token overlap: 45%

The folder score combines:

```text
Hierarchy/component score: 75%
Full expected path vs actual path: 20%
Best filename similarity: 5%
```

For multi-level strategies such as `brand/modelnumber`, component weighting is:

```text
Final component: 70%
Earlier components combined: 30%
```

This makes the product-specific final segment more important than a broad Brand folder.

# 18.3 Minimum score

Default:

```text
85
```

A best candidate below the minimum is rejected.

# 18.4 Ambiguity protection

If the top two different folders are within 3 points, Media is rejected as ambiguous.

Example:

```text
Candidate A: 88
Candidate B: 86
Difference: 2
Result: No upload; manual review
```

# 18.5 Recommended thresholds

| Threshold | Suggested use |
|---:|---|
| 95 | Highly strict batches |
| 90 | Strong production safety |
| 85 | Default balanced setting |
| Below 85 | Use only after reviewing folder naming quality |

---

# 19. Media Upload Behaviour

# 19.1 Supported files

```text
.jpg
.jpeg
.png
.webp
.avif
```

Zero-byte files are ignored.

# 19.2 Maximum number of images

Default:

```text
25
```

This may be changed with the environment variable:

```text
PROCKURED_MAX_MEDIA_UPLOADS
```

# 19.3 Upload target

The bot uploads only through:

```text
Media → Product Media → Add Image
```

It avoids:

```text
Product Review Images
Upload button in review section
Add URL
Add Link
Brochures & Files
```

# 19.4 Upload sequence

```text
Open Media
→ Wait 2 seconds
→ Resolve safe folder
→ Collect and sort image files
→ Click top Product Media Add Image tile
→ Select all chosen images together
→ Wait 5 seconds
→ Log Product Media image count
→ Continue
```

# 19.5 File ordering

Images are sorted by lowercase filename.

Recommended naming:

```text
01-main.jpg
02-side.jpg
03-back.jpg
04-lifestyle.jpg
```

# 19.6 Existing images

By default, existing Product Media is preserved and new images are added.

When JSON requests:

```json
{
  "media": {
    "replace_existing_images": true
  }
}
```

the script attempts to remove existing media first.

**Use this option carefully.** The deletion logic is best-effort and should be tested on a draft product before a large run.

# 19.7 Upload confirmation in V1

The script logs Product Media image count before and after the 5-second upload wait. It considers file submission successful when the Product Media file chooser accepts the files.

The current V1 does not fail solely because the visible count did not increase. Final save confirmation is still based on the redirect after Update Product.

---

# 20. Resume Workflow

Resume allows a new batch run to skip products that were successful in an earlier run.

## 20.1 Resume from success report

```bash
python independent_listing_bot_cli_v1.py --batch products.json --resume "batch_reports\2026-07-17_120000\success_report.csv"
```

## 20.2 Resume from report folder

```bash
python independent_listing_bot_cli_v1.py --batch products.json --resume "batch_reports\2026-07-17_120000"
```

Folder behaviour:

1. Use `success_report.csv` when it exists.
2. Otherwise use the newest CSV in the folder.

For safest behaviour, provide the exact `success_report.csv` path.

## 20.3 Successful row detection

A row is accepted as successful when:

- The filename contains `success`; or
- Status/message text contains words such as:
  - updated
  - success
  - confirmed
  - saved

## 20.4 Resume matching keys

A current product is skipped when any of these match a successful prior row:

```text
row_id
SKU
Model Number
lookup_value
```

## 20.5 Resume output

Skipped products are written to:

```text
resume_skipped_report.csv
```

---

# 21. Save and Redirect Verification

The save sequence is intentionally delayed to allow Media and other React state to settle.

## 21.1 Save sequence

```text
Finish selected sections
→ Open Basic tab
→ Wait 5 seconds
→ Scroll to bottom
→ Wait 5 seconds
→ Click Update Product
→ Wait up to 60 seconds
```

## 21.2 Success condition

Save is confirmed only when the browser returns to the Products list:

```text
https://store.prockured.com/admin/products
```

The Products list URL may include a trailing slash or query string during checking, but the bot confirms that it is no longer on an individual product edit URL.

## 21.3 Timeout behaviour

When no automatic redirect happens within 60 seconds:

1. Save is marked unconfirmed.
2. The product is added to failed and manual-review reports.
3. The bot directly opens the Products list.
4. The next product continues.

This prevents one stuck product from stopping the complete batch.

---

# 22. Reports

Each batch creates a timestamped folder:

```text
batch_reports\YYYY-MM-DD_HHMMSS\
```

Files:

```text
batch_log.txt
success_report.csv
failed_report.csv
missing_data_report.csv
manual_review_report.csv
resume_skipped_report.csv
```

## 22.1 `batch_log.txt`

Detailed chronological terminal-style log containing:

- Batch file
- Product count
- Lookup mode
- Selected sections
- Image root
- MediaSort
- Product search
- Verification
- Section status
- Media score
- Save redirect
- Final totals

## 22.2 `success_report.csv`

Contains successfully updated products whose redirect was confirmed.

Important columns:

```text
row_id
sku
model_number
lookup_type
lookup_value
category_option
brand_option
sections
status
message
updated_at
```

## 22.3 `failed_report.csv`

Contains products that could not complete.

Important columns:

```text
reason
last_page_url
details
```

## 22.4 `missing_data_report.csv`

Records missing data such as:

```text
SKU or Model Number
category_option
description
short_description
SEO fields
attributes
```

Only missing lookup identity and missing Category are unconditional blockers. Other missing fields may be reported while a section-limited run continues.

## 22.5 `manual_review_report.csv`

Records non-fatal or uncertain conditions such as:

- Brand option not found
- Media not uploaded
- Save redirect not confirmed

## 22.6 `resume_skipped_report.csv`

Contains products skipped because they matched the prior resume report.

---

# 23. Interactive Clipboard Mode

When the script is launched without `--batch`, it starts interactive hotkeys.

```bash
python independent_listing_bot_cli_v1.py
```

The browser connection remains open and commands are triggered from the keyboard.

## 23.1 Clipboard block format

```text
[BASICS]
Product Type : Simple Product
Product Name : Example Product
Product Tags : example, product
Description : First paragraph.

Second paragraph.
Short Description : Short summary.
Model Name : MODEL123

[ATTRIBUTES]
Material : Stainless Steel
Usage : Hotels and Restaurants

[FILTER ATTRIBUTES]
Colour : Silver

[VARIANT ATTRIBUTES]
Packing Type : Single Piece, Pack of 6

[VARIANT FILTER ATTRIBUTES]
Capacity : 250 ML, 360 ML

[VARIANT PRICING]
Packing Type : Single Piece | Capacity : 250 ML | Price : 250
Packing Type : Pack of 6 | Capacity : 250 ML | Price : 1500

[SEO]
SEO Title : Example Product
SEO Description : Example SEO description.
SEO Keywords : example product, commercial product

[MEDIA]
Image Folder : C:\Product Images\MODEL123
Replace Existing Images : No

[PRICING]
Sale Price : 250
```

## 23.2 Description formatting

Long Description preserves blank lines between paragraphs.

Other fields are compacted to avoid accidental line breaks.

## 23.3 Interactive full fill

The Full Fill hotkey fills:

```text
Basics
→ Attributes
→ Variations when variable
→ SEO
→ Media
→ Pricing when simple
```

It does not automatically click Update Product. Review manually before saving.

---

# 24. Hotkeys

| Hotkey | Action |
|---|---|
| `Alt + Shift + L` | Load clipboard data |
| `Alt + Shift + B` | Fill Basics |
| `Alt + Shift + A` | Fill Attributes |
| `Alt + Shift + 1` | Test one Attribute |
| `Alt + Shift + V` | Generate/Fix Variations |
| `Alt + Shift + S` | Fill SEO |
| `Alt + Shift + M` | Upload Product Media Images |
| `Alt + Shift + I` | Update Image Alt Text |
| `Alt + Shift + R` | Fill Pricing |
| `Alt + Shift + J` | Run default Batch JSON using full sections and SKU lookup |
| `Alt + Shift + F` | Full Clipboard Fill |
| `Alt + Shift + D` | Debug Current Tab |
| `Alt + Shift + X` | Request stop for current action |
| `Alt + Shift + Q` | Quit immediately |

The default Batch JSON used by `Alt + Shift + J` is:

```text
<RUN_DIR>\batch_products.json
```

---

# 25. Environment Variables

The script supports environment overrides.

| Variable | Purpose | Default |
|---|---|---|
| `PROCKURED_RUN_DIR` | Main working directory | Current terminal directory |
| `PROCKURED_OUTPUT_DIR` | CSV/output scan location | First existing run/script directory |
| `PROCKURED_IMAGE_ROOT` | Default media root | `<RUN_DIR>\images` |
| `PROCKURED_MAX_MEDIA_UPLOADS` | Maximum Product Media files | `25` |
| `PROCKURED_ADMIN_PRODUCTS_URL` | Products list URL | `https://store.prockured.com/admin/products` |
| `PROCKURED_BATCH_JSON` | Default hotkey batch JSON | `<RUN_DIR>\batch_products.json` |
| `PROCKURED_BATCH_REPORT_DIR` | Report root | `<RUN_DIR>\batch_reports` |

Windows CMD example:

```bat
set PROCKURED_IMAGE_ROOT=D:\Product Images
set PROCKURED_MAX_MEDIA_UPLOADS=15
python independent_listing_bot_cli_v1.py --batch products.json --media
```

PowerShell example:

```powershell
$env:PROCKURED_IMAGE_ROOT = "D:\Product Images"
$env:PROCKURED_MAX_MEDIA_UPLOADS = "15"
python .\independent_listing_bot_cli_v1.py --batch .\products.json --media
```

CLI `--forceMedia` takes priority over the environment-based default root for that run.

---

# 26. Terminal Startup Information

At startup, the script prints:

- CLI V1 name and author
- Default behaviour
- Category rule
- Lookup rule
- Variation dependency rule
- Example commands
- MediaSort values
- Default images folder
- Four attribute section names
- JSON media configuration examples
- All interactive hotkeys
- Current Run folder
- Current Batch JSON
- Current Images folder
- Reports folder
- Selected lookup mode
- Selected sections
- Forced media root when present
- MediaSort when present
- Resume report when present

This is intended to make the terminal self-documenting during normal use.

Use standard argparse help for the compact command list:

```bash
python independent_listing_bot_cli_v1.py --help
```

---

# 27. End-to-End Batch Workflow

For each product, CLI V1 follows this sequence.

## 27.1 Pre-run preparation

```text
Parse CLI arguments
→ Resolve selected sections
→ Add mandatory Category
→ Load JSON package
→ Load batch media config
→ Load resume report
→ Create timestamped report folder
```

## 27.2 Per-product validation

```text
Read Row ID
→ Read SKU
→ Read Model Number
→ Resolve lookup identity
→ Read Category and Brand
→ Apply product skip_media/skip_pricing
→ Check resume index
→ Record missing data
→ Stop immediately when lookup identity or Category is missing
```

## 27.3 Product opening

```text
Open Products list
→ Search lookup value
→ Resolve matching product row
→ Open Edit Product
→ Verify SKU or Model Number
```

## 27.4 Section processing

Full-mode order:

```text
Basics
→ Mandatory Category
→ Brand
→ Attributes
→ Variations
→ SEO
→ Simple Pricing
→ Media
```

Media runs near the end so the script can return to Basic and perform the tested save sequence.

## 27.5 Save

```text
Open Basic
→ Wait 5 seconds
→ Scroll to bottom
→ Wait 5 seconds
→ Click Update Product
→ Wait for Products-list redirect up to 60 seconds
```

## 27.6 Reporting

```text
Redirect confirmed → success_report.csv
Redirect timeout → failed_report.csv + manual_review_report.csv
Resume match → resume_skipped_report.csv
Other error → failed_report.csv
```

---

# 28. Recommended Commands

## 28.1 Full listing batch

```bash
python independent_listing_bot_cli_v1.py --batch products.json
```

## 28.2 Basics only

```bash
python independent_listing_bot_cli_v1.py --batch products.json --basics
```

## 28.3 Brand only

```bash
python independent_listing_bot_cli_v1.py --batch products.json --brand
```

## 28.4 Attributes only

```bash
python independent_listing_bot_cli_v1.py --batch products.json --attributes
```

## 28.5 Variations only with required dependency workflow

```bash
python independent_listing_bot_cli_v1.py --batch products.json --variation
```

## 28.6 SEO only

```bash
python independent_listing_bot_cli_v1.py --batch products.json --seo
```

## 28.7 Pricing only

```bash
python independent_listing_bot_cli_v1.py --batch products.json --pricing
```

## 28.8 Media only using Model Number folder names

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media --MediaSort modelnumber
```

## 28.9 Media from another root

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media --forceMedia "D:\Product Images" --MediaSort modelnumber
```

## 28.10 Brand/Model hierarchy

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media --forceMedia "D:\Product Images" --MediaSort brand/modelnumber
```

Expected folder pattern:

```text
D:\Product Images\Brand Name\Model Number\images...
```

## 28.11 Model Number product lookup

```bash
python independent_listing_bot_cli_v1.py --batch products.json --lookup modelnumber
```

## 28.12 Model Number lookup plus Media-only update

```bash
python independent_listing_bot_cli_v1.py --batch products.json --lookup modelnumber --media --MediaSort modelnumber
```

## 28.13 Resume a full run

```bash
python independent_listing_bot_cli_v1.py --batch products.json --resume "batch_reports\LAST_RUN\success_report.csv"
```

## 28.14 Resume a Media-only run

```bash
python independent_listing_bot_cli_v1.py --batch products.json --media --resume "batch_reports\LAST_RUN\success_report.csv"
```

---

# 29. Troubleshooting

## 29.1 `No live Prockured admin page found`

Cause:

- Browser was not started with remote debugging.
- Port 9222 is not available.
- Prockured admin is not open.
- The connected browser tab was closed.

Fix:

1. Start Brave/Chrome with `--remote-debugging-port=9222`.
2. Open and log in to Prockured admin.
3. Open the Products page.
4. Run the command again.

---

## 29.2 `Missing SKU`

Cause:

Default lookup is SKU, but `admin.sku` is blank.

Fix:

- Add the full SKU; or
- Run with:

```bash
--lookup modelnumber
```

and provide a Model Number.

---

## 29.3 `Missing Model Number`

Cause:

`--lookup modelnumber` was selected but no recognized Model Number key was found.

Fix:

Add one of:

```json
{
  "admin": {
    "model_number": "MODEL123"
  }
}
```

or another supported alias.

---

## 29.4 `Missing category_option`

Cause:

Category is mandatory for every saved batch operation.

Fix:

```json
{
  "admin": {
    "category_option": "Sub-Category (Parent Category)"
  }
}
```

Use the visible admin dropdown wording as closely as possible.

---

## 29.5 Product search returns a result but the product is not opened

Check:

- The SKU or Model Number appears in the correct result row.
- The result row exposes an Edit action.
- The page has fully loaded before the next action.
- Search did not leave multiple ambiguous rows.

Use terminal logs to inspect:

```text
Searched products by...
Product opened...
Lookup verification...
```

---

## 29.6 SKU mismatch on edit page

The bot opened a result whose edit-page SKU does not match the requested SKU.

The product is intentionally not changed.

Check for:

- Duplicate/partial search results.
- Incorrect SKU in JSON.
- Search formatting differences.

---

## 29.7 Model Number mismatch

Check:

- Model Number is contained in the admin SKU.
- The dedicated Model Name field contains the expected value.
- JSON uses the correct model.
- Search result row belongs to the correct product.

---

## 29.8 Category is not selected

Possible causes:

- JSON category wording differs from the dropdown.
- The dropdown did not receive keyboard focus.
- The page moved while automation was running.

Fix:

- Use the exact visible Category/Sub-Category wording.
- Do not use the mouse during automation.
- Test one product before a large batch.

---

## 29.9 Attribute fill fails

Possible causes:

- Existing UI card structure changed.
- Attribute value input is not visible.
- A stale card could not be cleared.
- The user moved or clicked the page during automation.

Fix:

1. Keep Attributes visible and do not touch the mouse.
2. Test one attribute with interactive hotkey:

```text
Alt + Shift + 1
```

3. Use debug hotkey:

```text
Alt + Shift + D
```

---

## 29.10 Filters or Variants checkbox is wrong

Check that the attribute is placed in the correct JSON section:

```text
filter_attributes          → Filters only
variant_attributes         → Variants only
variant_filter_attributes  → both
```

The bot reads current checkbox state and attempts to click only when necessary.

---

## 29.11 `--variation requested but no variant_attributes...`

Fix by adding:

```json
{
  "variant_attributes": {
    "Packing Type": ["Single Piece", "Pack of 6"]
  }
}
```

or:

```json
{
  "variant_filter_attributes": {
    "Capacity": ["250 ML", "360 ML"]
  }
}
```

---

## 29.12 Variants are generated but prices are missing

Check:

- Attribute values exactly match generated card text.
- Every `variant_pricing.attributes` value corresponds to one generated combination.
- Prices do not use thousands separators.
- Variant attribute names align with the admin card labels.

---

## 29.13 Images are not matched

Check terminal messages:

```text
Media root does not exist
Missing values for MediaSort
No image folders found
Score below minimum
Ambiguous top candidates
```

Fix options:

- Correct `--forceMedia` path.
- Change `--MediaSort`.
- Rename image folders more consistently.
- Add an explicit `media.folder`.
- Lower `--mediaMinScore` only after manual review.

---

## 29.14 Images are matched but not uploaded

Check:

- The Media tab contains `Product Media` and `Add Image`.
- A file chooser opens when Add Image is clicked manually.
- Files use supported extensions.
- Files are not zero bytes.
- The browser has permission to access the paths.

The bot deliberately avoids Review Images, Add URL, Add Link, and Brochure controls.

---

## 29.15 Images appear but changes are not saved

The script already performs the tested save order:

```text
Media
→ Basic
→ Wait 5 seconds
→ Scroll bottom
→ Wait 5 seconds
→ Update Product
→ Wait for redirect
```

If redirect times out:

- Check the failed/manual-review report.
- Open the product manually and verify whether changes were saved.
- Check for validation errors elsewhere on the form.

---

## 29.16 Resume skipped the wrong product

Resume matches by any one of:

```text
row_id
SKU
Model Number
lookup_value
```

Use unique Row IDs, SKUs, and Model Numbers. Prefer the exact `success_report.csv` rather than a generic report folder.

---

# 30. Operational Recommendations

## 30.1 Always test one draft product first

Before a large run:

1. Create a JSON containing one product.
2. Run only the intended sections.
3. Verify every saved field.
4. Confirm media location and ordering.
5. Confirm the redirect.

## 30.2 Use exact identifiers

Preferred reliability order:

```text
Exact SKU lookup
Exact Model Number folder
Explicit media.folder
Exact hierarchy
High score fallback
```

## 30.3 Keep folder names clean

For Model Number sorting:

```text
images\1029BB
```

For Brand/Model sorting:

```text
images\Duralex\1029BB
```

Avoid multiple old/archive folders with nearly identical names under the same root because the ambiguity rule may reject them.

## 30.4 Use numbered image names

```text
01-main.jpg
02-side.jpg
03-detail.jpg
04-lifestyle.jpg
```

This gives predictable upload order.

## 30.5 Use prices without commas

```text
17100
```

not:

```text
17,100
```

## 30.6 Do not touch the browser during a batch

Mouse movement, manual scrolling, or typing can change focus and interfere with dropdown, attribute, or file-chooser logic.

## 30.7 Use Resume after interruption

After a partial batch:

```bash
python independent_listing_bot_cli_v1.py --batch products.json --resume "previous_run\success_report.csv"
```

## 30.8 Separate batches by purpose

For safer operations, consider section-specific runs:

```text
Run 1: Basics + SEO
Run 2: Attributes
Run 3: Media
Run 4: Variations
```

This reduces the amount of work that must be repeated after a failure.

## 30.9 Keep report folders

Reports are required for:

- Resume
- Auditing
- Manual review
- Debugging
- Identifying category or media failures

---

# 31. Current V1 Limitations and Safety Notes

1. **Price strings with commas are unsafe.**
   - Use `17100`, not `17,100`.

2. **Category runs in every batch operation.**
   - Even Media-only runs require a valid `category_option`.

3. **The CLI does not have `--category`.**
   - Category is automatic and mandatory.

4. **Model Number lookup is not automatic fallback.**
   - SKU remains the default.
   - Use `--lookup modelnumber` explicitly.

5. **Existing attributes are cleared during attribute updates.**
   - Ensure the JSON contains every attribute that should remain.

6. **Variation Active state is preserved.**
   - The bot does not activate or deactivate generated variants.

7. **Media scored fallback is intentionally conservative.**
   - Low-score and ambiguous folders are rejected.

8. **Replace Existing Images is best-effort.**
   - Test it before production use.

9. **Media upload success is based on file submission, not a strict final count increase.**
   - Save redirect remains the final product-save confirmation.

10. **Brand failure is non-fatal.**
    - The product can continue without Brand and is added to manual review.

11. **Missing descriptions, SEO, or attributes may appear in `missing_data_report.csv` even in a section-limited run.**
    - Missing lookup identity and Category are the hard blockers.

12. **Interactive Full Fill does not save automatically.**
    - Review and save manually in hotkey mode.

13. **Batch mode does save automatically.**
    - It clicks Update Product and verifies the redirect.

---

# Final Summary

CLI V1 is designed around a safe existing-product update workflow:

```text
Find the correct product
→ Verify it
→ Apply only the selected sections
→ Always set Category
→ Handle four attribute checkbox states
→ Build variations only after variant attributes exist
→ Match Media using configurable identities and scoring
→ Upload only to Product Media
→ Return to Basic
→ Save
→ Confirm redirect
→ Report every outcome
```

For normal full use:

```bash
python independent_listing_bot_cli_v1.py --batch products.json
```

For products with blank SKU and available Model Number:

```bash
python independent_listing_bot_cli_v1.py --batch products.json --lookup modelnumber
```

For a safe Model Number-based Media run:

```bash
python independent_listing_bot_cli_v1.py --batch products.json --lookup modelnumber --media --forceMedia "D:\Product Images" --MediaSort modelnumber --mediaMinScore 90
```
