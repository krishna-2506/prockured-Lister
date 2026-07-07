# Prockured Scraper & Listing Automation

An end-to-end automation toolkit built for Prockured's internal product operations team. This project handles two major workflows:

1. **Product Image Scraping** — Finds and downloads high-quality product images from multiple e-commerce platforms (Hyperpure, Amazon, Flipkart, BigBasket) based on a CSV input list.
2. **Listing Automation** — Auto-fills the Prockured admin panel with product details (basics, attributes, SEO, pricing, media) by reading structured text from the clipboard — no manual typing needed.

> Built as an internship project at **Prockured** to speed up the product catalog management workflow.

---

## Table of Contents

- [Project Structure](#project-structure)
- [How It Works](#how-it-works)
- [Setup & Installation](#setup--installation)
- [Usage](#usage)
  - [1. Image Scraper](#1-image-scraper)
  - [2. Listing Automation](#2-listing-automation)
  - [3. Multi-Source Amazon-Strict Scraper](#3-multi-source-amazon-strict-scraper)
  - [4. Automation Bot (AutoBot)](#4-automation-bot-autobot)
- [Input Format](#input-format)
- [Output Files](#output-files)
- [Environment Variables](#environment-variables)
- [Notes](#notes)

---

## Project Structure

```
prockured_scraper_package/
│
├── prockured_scraper.py              # Core image scraper (Hyperpure → Amazon → Flipkart → BigBasket → Google)
├── Listing Final.py                  # Full listing automation with keyboard hotkeys (main tool)
├── listin v2.py                      # Lighter version of the listing automation script
├── multi_source_image_scraper_AMAZON_STRICT.py   # Strict Amazon-first multi-source scraper
├── autobot.py                        # Enhanced automation bot for Prockured operations
├── autobot v2.py                     # Next-generation automation with advanced features
│
├── test_input.csv                    # Sample input file to test the scraper
├── veeba_input.csv                   # Real product input used during development
├── start.bat                         # Launches Brave in remote-debug mode (needed for listing automation)
│
├── bakery_ingredients_hyperpure_single_sale_price.json      # Sample data: Hyperpure pricing for bakery items
├── batch_products_purix_2_products.json                     # Sample data: Purix batch product listings
│
├── prockured_output/                 # Auto-generated: scraper output (images, CSVs, logs)
└── prockured_scraper_output/         # Auto-generated: alternate scraper output folder
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

### Listing Automation (`Listing Final.py`)

Connects to a live Brave browser window via Chrome DevTools Protocol (CDP), then:
- Listens for a keyboard hotkey (e.g. **Ctrl+Shift+L**)
- Reads structured product data from your clipboard
- Parses sections: `[BASICS]`, `[ATTRIBUTES]`, `[VARIANT PRICING]`, `[SEO]`, `[MEDIA]`, `[PRICING]`
- Automatically fills in each form field on the Prockured admin page
- Handles variable products with variant attributes and pricing rows

This means you can paste a product spec from ChatGPT or a spreadsheet, hit the hotkey, and the whole form fills itself out.

---

## Setup & Installation

### Prerequisites

- Python 3.10 or higher
- [Brave Browser](https://brave.com/) (for listing automation)
- Git

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/prockured-scraper.git
cd prockured-scraper
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

### 2. Listing Automation

> Make sure Brave is running in debug mode (see step 4 above) and the product edit page is open.

```bash
python "Listing Final.py"
```

Once running, use the keyboard hotkeys defined in the script to trigger form fills. The script reads from your clipboard automatically, so just copy the product spec and press the hotkey.

### 3. Multi-Source Amazon-Strict Scraper

```bash
# Basic run with an Excel/CSV input
python multi_source_image_scraper_AMAZON_STRICT.py "your_products.xlsx"

# Limit rows, skip download, choose sources
python multi_source_image_scraper_AMAZON_STRICT.py "your_products.xlsx" --limit 10 --no-download
python multi_source_image_scraper_AMAZON_STRICT.py "your_products.xlsx" --sources hyperpure,bigbasket,amazon
python multi_source_image_scraper_AMAZON_STRICT.py "your_products.xlsx" --headful
```

### 4. Automation Bot (AutoBot)

Enhanced automation tools for Prockured product operations:

```bash
# Run the main automation bot
python autobot.py

# Run the next-generation version with advanced features
python "autobot v2.py"
```

These tools provide automated workflows for:
- Batch product processing across multiple sources (Hyperpure, Purix, etc.)
- Automated data collection and consolidation
- Streamlined product catalog updates

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

For the multi-source scraper, outputs go to `image_scraper_output/`:

| File | Description |
|------|-------------|
| `image_links.xlsx` | All image links per product |
| `catalog_review.xlsx` | Match quality review sheet |
| `download_report.xlsx` | Download status per image |

### Sample Data Files

Reference JSON files for understanding data structures from various sources:

| File | Description |
|------|-------------|
| `bakery_ingredients_hyperpure_single_sale_price.json` | Sample Hyperpure product data with pricing for bakery ingredients |
| `batch_products_purix_2_products.json` | Sample Purix batch product listings |

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
- The `prockured_output/` and `prockured_scraper_output/` folders are gitignored since they contain downloaded images and internal data.
- This project was written and tested on **Windows 10/11**. Linux/macOS would need minor path adjustments in `start.bat` and `Listing Final.py`.

---

## Built With

- [Playwright](https://playwright.dev/python/) — browser automation
- [RapidFuzz](https://github.com/maxbachmann/RapidFuzz) — fuzzy string matching
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing
- [Requests](https://docs.python-requests.org/) — HTTP
- [Pandas](https://pandas.pydata.org/) — data handling (multi-source scraper)
- [pynput](https://pynput.readthedocs.io/) — keyboard hotkey listener (listing tool)
- [pyperclip](https://pypi.org/project/pyperclip/) — clipboard access (listing tool)
- [tqdm](https://tqdm.github.io/) — progress bars
- [Pillow](https://python-pillow.org/) + [imagehash](https://github.com/JohannesBuchner/imagehash) — image deduplication

---

*Internship project — Prockured, 2025*
