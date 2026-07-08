# Prockured Scraper & Listing Automation

An end-to-end automation toolkit built for Prockured's internal product operations team. This project handles two major workflows:

1. **Product Image Scraping** — Finds and downloads high-quality product images from multiple e-commerce platforms (Hyperpure, Amazon, Flipkart, BigBasket) based on a CSV input list.
2. **Listing Automation** — Auto-fills the Prockured admin panel with product details (basics, attributes, SEO, pricing, media) by reading structured text from the clipboard — no manual typing needed. Supports both hotkey-triggered clipboard filling and automated batch JSON filling.

> Built as an internship project at **Prockured** to automate the product catalog management workflow.

---

## Table of Contents

- [Project Structure](#project-structure)
- [How It Works](#how-it-works)
- [Setup & Installation](#setup--installation)
- [Usage](#usage)
  - [1. Image Scraper](#1-image-scraper)
  - [2. Listing Automation (Independent Bot)](#2-listing-automation-independent-bot)
  - [3. Legacy Listing Script](#3-legacy-listing-script)
- [Batch Listing Automation Workflow](#batch-listing-automation-workflow)
- [Input Format](#input-format)
- [Output Files](#output-files)
- [Environment Variables](#environment-variables)
- [Notes](#notes)

---

## Project Structure

```
prockured_scraper_package/
│
├── prockured_scraper.py            # Core image scraper (Hyperpure → Amazon → Flipkart → BigBasket → Google)
├── independent_listing_bot.py      # Independent listing bot with hotkeys & batch JSON filling (Main Bot)
├── Listing Script.py               # Legacy version of listing automation with keyboard hotkeys
│
├── test_input.csv                  # Sample input file to test the scraper
├── veeba_input.csv                 # Real product input used during development
├── start.bat                       # Launches Brave in remote-debug mode (needed for listing automation)
│
├── prockured_output/               # Auto-generated: scraper output (images, CSVs, logs) [Git Ignored]
├── prockured_scraper_output/       # Auto-generated: alternate scraper output folder [Git Ignored]
└── batch_reports/                  # Auto-generated: batch run execution reports [Git Ignored]
```

---

## How It Works

### Image Scraper (`prockured_scraper.py`)

Takes a CSV of product names and brands, then searches each product across a priority pipeline:

```
Hyperpure → Amazon → Flipkart → BigBasket → Google Images
```

For each product it:
- Builds a clean search query from the brand + title
- Scrapes product pages using Playwright (headless browser)
- Scores each result using fuzzy matching (brand check + quantity/pack matching)
- Downloads images into a per-product folder
- Writes summary CSVs (`summary.csv`, `all_images.csv`, `hyperpure_prices.csv`)

### Listing Automation Bot (`independent_listing_bot.py`)

Connects to a live Brave browser window via Chrome DevTools Protocol (CDP), then:
- **Hotkey Mode**: Listens for keyboard hotkeys (e.g. **Alt+Shift+F** for Full Fill)
- **Clipboard Parsing**: Reads structured product data from your clipboard and parses sections: `[BASICS]`, `[ATTRIBUTES]`, `[VARIANT PRICING]`, `[SEO]`, `[MEDIA]`, `[PRICING]`
- **Batch JSON Mode**: Reads a batch list of products from a JSON file (e.g. `batch_products.json`), automatically searches them by SKU, fills out the form fields, and updates the products on the Prockured admin page.
- Handles variable products with variant attributes and pricing rows.

This allows you to copy a product spec from ChatGPT or a spreadsheet, hit a hotkey, and have the form filled out automatically.

---

## Setup & Installation

### Prerequisites

- Python 3.10 or higher
- [Brave Browser](https://brave.com/) (for listing automation)
- Git

### 1. Clone the repo

```bash
git clone https://github.com/krishna-2506/prockured-Lister.git
cd prockured-scraper-package
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Install Playwright browsers

```bash
python -m playwright install chromium
```

### 4. (For listing automation only) Launch Brave in debug mode

Double-click `start.bat` or run:

```bash
"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\prockured_brave_profile"
```

Then open the Prockured admin product edit page in that window.

---

## Usage

### 1. Image Scraper

```bash
# Basic run
python prockured_scraper.py test_input.csv

# Limit to first 10 products
python prockured_scraper.py your_products.csv --limit 10

# Don't download, just generate reports
python prockured_scraper.py your_products.csv --no-download

# Save output to a specific folder
python prockured_scraper.py your_products.csv --output my_output_folder
```

### 2. Listing Automation (Independent Bot)

> Make sure Brave is running in debug mode (see step 4 above) and the product edit page is open.

```bash
python independent_listing_bot.py
```

Once running, use the following hotkeys to trigger actions:

| Hotkey | Action |
|---|---|
| **Alt + Shift + L** | Load clipboard data |
| **Alt + Shift + B** | Fill Basics |
| **Alt + Shift + A** | Fill Attributes |
| **Alt + Shift + V** | Generate/Fix Variations |
| **Alt + Shift + S** | Fill SEO |
| **Alt + Shift + M** | Fill Media |
| **Alt + Shift + I** | Update Image Alt Text |
| **Alt + Shift + R** | Fill Pricing |
| **Alt + Shift + J** | Run Batch JSON Fill |
| **Alt + Shift + F** | Full Fill (fills all sections) |
| **Alt + Shift + D** | Debug Current Tab |
| **Alt + Shift + X** | Stop Current Action |
| **Alt + Shift + Q** | Quit |

#### Running in Batch Mode Directly:
You can also run batch filling directly from a JSON file:
```bash
python independent_listing_bot.py --batch path/to/batch_products.json
```

### 3. Legacy Listing Script

For running the legacy version:
```bash
python "Listing Script.py"
```

---

## Batch Listing Automation Workflow

The **Batch JSON Filler** mode in `independent_listing_bot.py` is the most powerful and comprehensive feature in this toolkit. It automates catalog updates for multiple products sequentially. Below is the step-by-step breakdown of how it works and handles all components:

### 1. Loading and Validating Input
- Reads the batch JSON file (supported root keys: `"products"`, `"batch_products"`, `"items"`, `"data"`, or a direct list of objects).
- Checks each item for missing required fields (like `sku` and `category_option`) or warning-level fields (like `description`, `short_description`, or `seo` fields) and flags them in a warning report.

### 2. Locating the Product in the Prockured Admin
- The bot navigates to the admin products list page (`https://store.prockured.com/admin/products`).
- Inserts the product's unique **SKU** into the search field and submits.
- Clicks on the first matching result.
- Once the edit page loads, the bot grabs the SKU on the page and verifies it against the expected SKU to prevent editing the wrong product.

### 3. Automating Form Sections
Based on the `"fill_sections"` array in the JSON item (or defaults if not specified), it executes:
- **Category Selection**: In the "Basic" tab, searches for the specified category, triggers the option, and executes a precise manual keyboard sequence (**Enter → Tab → Tab**) to safely transition focus back to the brand.
- **Brand Selection**: Selects the brand dropdown value.
- **Basics**: Populates Product Name, Product Type, Tags, Description (preserving exact paragraph spaces copied from spreadsheet/chat models), and Short Description.
- **Attributes**: Automatically clears any existing attributes first to prevent duplication, then populates new attributes.
- **Variations (Variable Products)**: If variant attributes are configured in the JSON, checks variation boxes, clicks generate, and populates the pricing grid for each variation.
- **SEO**: Fills SEO Title, SEO Description, and SEO Keywords.
- **Pricing**: Sets Base Price and Sale Price. If only `sale_price` is provided, it automatically computes a stable markup price (using a deterministic hash-based calculation from the product name to keep markups consistent).

### 4. Smart Media Integration (Scraper Image Pipeline)
The bot integrates directly with the output of the image scraper:
- Resolves the product name and scans for images in three prioritized locations:
  1. **Scraper CSV Report**: Looks up `all_images.csv` from the scraper output directory to find exact fuzzy matches of the product name with downloaded file paths.
  2. **Organized Image Directory**: Scans the `prockured_output/images/` root folder recursively for folder names matching the product name or brand.
  3. **JSON Manual Overrides**: Uses the path specified in `[MEDIA]` block's `Image Folder` if provided.
- Uploads the top images (up to `MAX_MEDIA_UPLOADS`, default 8).
- If `Replace Existing Images` is set to `Yes` / `True` in the JSON, the bot automatically clicks and removes all prior images in the admin panel before uploading new ones.
- **SEO Alt Text Optimization**: Once images are uploaded, the bot hovers over each image card in the admin interface, opens the alt text field, and sets it to `<Product Name> Prockured Image <Index>` for SEO naming conventions.

### 5. Saving and Report Generation
- Clicks the **Update Product** button to save the changes.
- Generates execution logs in `batch_reports/<timestamp>/`:
  - `success_report.csv`: List of successfully updated SKUs.
  - `failed_report.csv`: Details and stack traces of failed rows.
  - `missing_data_report.csv`: Lists missing keys per SKU.
  - `manual_review_report.csv`: Flags items that need manual human checks (e.g. brand not found).
  - `batch_log.txt`: Detailed raw text console output logs.

---

## Input Format

The input CSV should have at minimum a `Product Title` column. A `Brand` column is strongly recommended.

```csv
Brand,Product Title
Veeba,"Tasty Pixel (Veeba) - Chilli Flakes, 500 gm"
Veeba,"Tasty Pixel (Veeba) - Mixed Pickle (Blister Pack), 15 gm (Pack of 90)"
```

Other accepted column names: `Name`, `Title`, `Product Name`, `Brand Name`

---

## Output Files

After running the scraper, outputs appear in `prockured_output/` (or your custom `--output` folder):

| File | Description |
|------|-------------|
| `summary.csv` | Match status for every product |
| `all_images.csv` | All image URLs found, with source info |
| `hyperpure_prices.csv` | Prices pulled from Hyperpure matches |
| `scraper.log` | Full run log |
| `images/<Brand>/<Product>/` | Downloaded images, organized by brand and product |

---

## Environment Variables

You can override default paths without editing the code:

| Variable | Default | Description |
|----------|---------|-------------|
| `PROCKURED_OUTPUT_DIR` | `prockured_output` | Where scraper output is saved |
| `PROCKURED_IMAGE_ROOT` | `prockured_output/images` | Where downloaded images go |
| `PROCKURED_MAX_MEDIA_UPLOADS` | `8` | Max images to upload per product in listing tool |

Example:
```bash
set PROCKURED_OUTPUT_DIR=D:\work\output
python prockured_scraper.py products.csv
```

---

## Notes

- The listing automation script connects over **CDP (port 9222)**. If you see a connection error, make sure `start.bat` was run before the script.
- Matching is strict by design: brand, quantity (kg/g/ml), and pack count all have to line up before an image is accepted. This avoids wrong variants getting listed.
- Output folders (`prockured_output/`, `prockured_scraper_output/`, and `batch_reports/`) are in `.gitignore` to prevent committing internal data or run summaries.
- This project was written and tested on **Windows 10/11**.

---

## Built With

- [Playwright](https://playwright.dev/python/) — browser automation
- [RapidFuzz](https://github.com/maxbachmann/RapidFuzz) — fuzzy string matching
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing
- [Requests](https://docs.python-requests.org/) — HTTP
- [pynput](https://pynput.readthedocs.io/) — keyboard hotkey listener (listing tool)
- [pyperclip](https://pypi.org/project/pyperclip/) — clipboard access (listing tool)
- [tqdm](https://tqdm.github.io/) — progress bars
- [Pillow](https://python-pillow.org/) + [imagehash](https://github.com/JohannesBuchner/imagehash) — image deduplication

---

*Internship project — Prockured, 2025-2026*
