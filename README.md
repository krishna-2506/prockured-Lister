# Prockured Scraper & Listing Automation

An end-to-end automation toolkit built for Prockured's internal product operations team. This project handles multiple major workflows to streamline product catalog management:

1. **Product Image Scraping** — Finds and downloads high-quality product images from multiple e-commerce platforms (Hyperpure, Amazon, Flipkart, BigBasket) based on a CSV input list.
2. **Listing Automation** — Auto-fills the Prockured admin panel with product details (basics, attributes, SEO, pricing, media) by reading structured text from the clipboard — no manual typing needed.
3. **Batch Automation (AutoBot)** — Advanced automation for bulk product processing across multiple suppliers (Hyperpure, Purix, etc.) with multi-source data consolidation and real-time price updates.

> Built as an internship project at **Prockured** to speed up the product catalog management workflow.
> **v2 Update** — Extended with enhanced automation capabilities for scalable batch processing and multi-source data integration.

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
├── Image Scraping & Listing Tools
│   ├── prockured_scraper.py              # Core image scraper (Hyperpure → Amazon → Flipkart → BigBasket → Google)
│   ├── Listing Final.py                  # Full listing automation with keyboard hotkeys (main tool)
│   ├── listin v2.py                      # Lighter version of the listing automation script
│   └── multi_source_image_scraper_AMAZON_STRICT.py   # Strict Amazon-first multi-source scraper
│
├── Batch Automation (AutoBot)
│   ├── autobot.py                        # Core automation bot for batch product processing from multiple suppliers
│   └── autobot v2.py                     # Enhanced v2 with improved performance, error handling, and data consolidation
│
├── Sample Data & Reference Files
│   ├── bakery_ingredients_hyperpure_single_sale_price.json   # Hyperpure API response sample (bakery items with pricing)
│   ├── batch_products_purix_2_products.json                  # Purix batch product listing sample (2 product reference)
│   ├── test_input.csv                    # Standard test input for image scraper
│   └── veeba_input.csv                   # Real product data used during development (Veeba brand)
│
├── Utilities & Configuration
│   └── start.bat                         # Batch script: Launches Brave in remote-debugging mode (CDP port 9222)
│
├── Auto-Generated Output Folders
│   ├── prockured_output/                 # Primary scraper output (images, CSVs, logs)
│   │   ├── images/                       # Downloaded product images organized by brand/product
│   │   ├── summary.csv                   # Scraper execution summary
│   │   ├── all_images.csv                # Consolidated image URL references
│   │   ├── hyperpure_prices.csv          # Price data extracted from Hyperpure
│   │   └── scraper.log                   # Detailed execution log
│   │
│   └── prockured_scraper_output/         # Alternate output location (for compatibility)
│
├── Documentation
│   ├── README.md                         # This file
│   └── requirements.txt                  # Python dependencies
│
└── Legacy/Archive
    └── old/                              # Archived previous versions and test data (not used)
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

### Batch Automation Bot (`autobot.py` and `autobot v2.py`)

Automated bulk processing tool for importing and updating product data from multiple suppliers in real-time:

**Core Capabilities:**
- **Multi-source data ingestion** — Connects to supplier APIs (Hyperpure, Purix, etc.) and fetches live product catalogs
- **Batch processing** — Processes hundreds of products in a single run with automatic retry logic and error recovery
- **Data consolidation** — Merges pricing, availability, and metadata from multiple sources for comparison and analysis
- **Price tracking** — Monitors and logs historical price changes from each supplier
- **Smart matching** — Fuzzy-matches supplier products to existing Prockured catalog entries
- **Automated updates** — Directly updates product prices, availability, and variant information in Prockured

**Key Differences (v2 improvements):**
- **Enhanced performance** — Optimized API calls and parallelized batch processing
- **Better error handling** — Graceful recovery with detailed error logs and retry mechanisms
- **Extended supplier support** — Added support for additional suppliers beyond initial implementation
- **Improved logging** — Structured JSON logging for easier debugging and monitoring
- **Data validation** — Pre-processing validation to catch data issues before they reach the database

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

### 4. Batch Automation Bot (AutoBot)

The AutoBot provides automated bulk product processing from multiple suppliers. Use this for large-scale catalog imports, price updates, and multi-source data consolidation.

#### Basic Usage

```bash
# Run the stable version (autobot.py)
python autobot.py

# Run the enhanced v2 version (recommended for new implementations)
python "autobot v2.py"
```

#### Command-Line Options

```bash
# Process products from a specific supplier
python autobot.py --supplier hyperpure --limit 100

# Fetch from Hyperpure and compare with Purix pricing
python autobot.py --supplier hyperpure --compare-with purix

# Update prices for products already in the catalog
python autobot.py --update-prices --dry-run

# Process with detailed logging and save results
python autobot.py --verbose --output batch_results.json

# Run v2 with parallel processing (faster for large batches)
python "autobot v2.py" --parallel --workers 4 --supplier purix

# Fetch sample data (useful for testing and debugging)
python "autobot v2.py" --fetch-sample --limit 2 --output sample_data.json
```

#### Configuration

The bot reads supplier API credentials from environment variables:

```bash
# Set before running
set HYPERPURE_API_KEY=your_api_key_here
set PURIX_API_KEY=your_api_key_here

# Then run
python autobot.py --supplier hyperpure
```

#### Output Files

When AutoBot completes, check for:

- `batch_results.json` — Summary of processed products with success/failure status
- `price_comparison.csv` — Side-by-side pricing from different suppliers
- `autobot.log` — Detailed execution log with API calls and errors
- `failed_products.csv` — Products that failed processing (for manual review)

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

### Primary Scraper Outputs

After running the image scraper, outputs appear in `prockured_output/` (or your custom `--output` folder):

| File | Description |
|------|-------------|
| `summary.csv` | Match status for every product (matched/not found/skipped) |
| `all_images.csv` | All image URLs found, with source platform and match quality score |
| `hyperpure_prices.csv` | Prices pulled from Hyperpure matches with currency and timestamp |
| `scraper.log` | Full run log with timestamps, API calls, and error details |
| `images/<Brand>/<Product>/` | Downloaded images, organized hierarchically by brand and product |

### Multi-Source Scraper Outputs

For the multi-source scraper (`multi_source_image_scraper_AMAZON_STRICT.py`), outputs go to `image_scraper_output/`:

| File | Description |
|------|-------------|
| `image_links.xlsx` | All image links per product with source, URL, and metadata |
| `catalog_review.xlsx` | Match quality review sheet with confidence scores |
| `download_report.xlsx` | Download status per image (success/failed/duplicate) |

### AutoBot Outputs

When running the batch automation bot, output files are generated in the current directory:

| File | Description |
|------|-------------|
| `batch_results.json` | Structured JSON with processed product count, successes, failures, and warnings |
| `price_comparison.csv` | Side-by-side price comparison across suppliers (brand, product, supplier pricing) |
| `failed_products.csv` | List of products that failed processing with error reasons for manual review |
| `autobot.log` | Full execution log with API responses, data transformations, and debugging info |
| `sample_data.json` | (When using `--fetch-sample`) Raw API response samples for the specified supplier |

---

## Sample Data Files (Reference & Testing)

This package includes sample JSON files from actual supplier APIs. These serve as reference documentation for the data structures returned by each supplier's APIs and can be used for testing without making live API calls.

### `bakery_ingredients_hyperpure_single_sale_price.json`

**Source:** Hyperpure API (`/products` endpoint)

**Purpose:** Reference sample showing a typical Hyperpure product response for bakery ingredients

**Contains:**
- Product metadata (name, SKU, brand, category)
- Pricing information (MRP, wholesale price, single unit price)
- Inventory status (available quantity, reorder level)
- Variant options (pack sizes, units)
- Supplier information and lead times

**Usage:**
- Reference for AutoBot when developing Hyperpure API integration
- Test data for building JSON parsers
- Understanding Hyperpure's product data structure for validation rules

**Example Structure:**
```json
{
  "product_id": "...",
  "name": "...",
  "brand": "...",
  "category": "Bakery Ingredients",
  "price": {
    "mrp": 500.00,
    "wholesale_price": 400.00,
    "single_unit_price": 450.00,
    "currency": "INR"
  },
  "inventory": {
    "available": true,
    "quantity": 1000,
    "reorder_level": 100
  }
}
```

---

### `batch_products_purix_2_products.json`

**Source:** Purix Supplier API (batch product listing endpoint)

**Purpose:** Reference sample showing Purix batch product format (2 product sample)

**Contains:**
- Batch product identifiers (batch_id, product_id, batch_number)
- Quantity and unit information
- Pricing for bulk orders
- Batch expiry dates and manufacturing dates
- Quality certifications and compliance info

**Usage:**
- Reference for AutoBot when developing Purix API integration
- Test cases for batch product matching logic
- Understanding bulk quantity and pricing tiers

**Example Structure:**
```json
{
  "batch_id": "...",
  "products": [
    {
      "product_id": "...",
      "name": "...",
      "quantity": 500,
      "unit": "kg",
      "batch_number": "...",
      "manufacturing_date": "2025-01-15",
      "expiry_date": "2026-01-14",
      "price_per_unit": 45.50
    },
    {
      "product_id": "...",
      "name": "...",
      "quantity": 1000,
      "unit": "units",
      "batch_number": "...",
      "manufacturing_date": "2025-02-01",
      "expiry_date": "2025-08-01",
      "price_per_unit": 28.75
    }
  ]
}
```

---

### How to Use Sample Files

1. **For Development:** Load these JSON files in your IDE to understand the API structure
2. **For Testing:** Use with `--fetch-sample` or mock data loaders instead of live API calls
3. **For Debugging:** Compare actual API responses against these samples to spot data inconsistencies
4. **For Documentation:** Share with team members to explain what data comes from each supplier

---

## Environment Variables

You can override default paths and API credentials without editing the code:

### Scraper Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PROCKURED_OUTPUT_DIR` | `prockured_output` | Where scraper output is saved |
| `PROCKURED_IMAGE_ROOT` | `prockured_output/images` | Where downloaded images go |
| `PROCKURED_MAX_MEDIA_UPLOADS` | `8` | Max images to upload per product in listing tool |

### AutoBot Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `HYPERPURE_API_KEY` | (required) | API key for Hyperpure supplier integration |
| `HYPERPURE_API_URL` | (auto) | Hyperpure API base URL (usually auto-configured) |
| `PURIX_API_KEY` | (required) | API key for Purix supplier integration |
| `AUTOBOT_BATCH_SIZE` | `50` | Number of products to process per batch |
| `AUTOBOT_TIMEOUT` | `300` | Timeout in seconds for API calls |
| `AUTOBOT_LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `AUTOBOT_RETRY_COUNT` | `3` | Number of retries for failed API calls |

### Example Configuration

```bash
# Windows (Command Prompt)
set PROCKURED_OUTPUT_DIR=D:\work\output
set HYPERPURE_API_KEY=your_key_here
set AUTOBOT_LOG_LEVEL=DEBUG
python prockured_scraper.py products.csv

# Windows (PowerShell)
$env:PROCKURED_OUTPUT_DIR="D:\work\output"
$env:HYPERPURE_API_KEY="your_key_here"
python autobot.py --supplier hyperpure

# Linux/macOS (Bash)
export PROCKURED_OUTPUT_DIR=/home/user/work/output
export HYPERPURE_API_KEY=your_key_here
python autobot.py --supplier hyperpure
```

---

## Notes

### General

- The listing automation script connects over **CDP (port 9222)**. If you see a connection error, make sure `start.bat` was run before the script.
- Matching is strict by design: brand, quantity (kg/g/ml), and pack count all have to line up before an image is accepted. This avoids wrong variants getting listed.
- The `prockured_output/` and `prockured_scraper_output/` folders are gitignored since they contain downloaded images and internal data.
- This project was written and tested on **Windows 10/11**. Linux/macOS would need minor path adjustments in `start.bat` and `Listing Final.py`.

### AutoBot-Specific

- **API Rate Limits:** AutoBot implements exponential backoff for rate-limited API responses. Check logs if you see throttling warnings.
- **Data Validation:** AutoBot validates product data before updating the catalog. Invalid entries are logged in `failed_products.csv` for manual review.
- **Dry-Run Mode:** Always test with `--dry-run` flag first to preview changes before actually updating the database.
- **Parallel Processing:** v2's `--parallel` flag can significantly speed up large batches, but may increase API load. Use with caution on production systems.
- **Credential Security:** Never commit API keys to version control. Use environment variables or a `.env` file (added to `.gitignore`).
- **Supplier Downtime:** The bot gracefully handles supplier API downtime with retry logic. Check `autobot.log` for details on skipped/failed suppliers.

### Sample Data

- The included JSON sample files (`bakery_ingredients_hyperpure_single_sale_price.json`, `batch_products_purix_2_products.json`) are real API responses and serve as reference documentation for data structure validation.
- Use these samples to test JSON parsing and data transformation logic without making live API calls during development.

---

## Built With

### Core Dependencies

- [Playwright](https://playwright.dev/python/) — Browser automation for web scraping and listing automation
- [RapidFuzz](https://github.com/maxbachmann/RapidFuzz) — Fuzzy string matching for intelligent product matching
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing for scraped web content
- [Requests](https://docs.python-requests.org/) — HTTP library for API calls and web requests
- [Pandas](https://pandas.pydata.org/) — Data manipulation and CSV/Excel handling
- [pynput](https://pynput.readthedocs.io/) — Keyboard hotkey listener for listing automation
- [pyperclip](https://pypi.org/project/pyperclip/) — Clipboard access for data input
- [tqdm](https://tqdm.github.io/) — Progress bars for user feedback

### Image Processing

- [Pillow](https://python-pillow.org/) — Image handling and manipulation
- [imagehash](https://github.com/JohannesBuchner/imagehash) — Perceptual image hashing for deduplication

### AutoBot-Specific

- [aiohttp](https://docs.aiohttp.org/) — Async HTTP client for concurrent API requests (v2)
- [python-dotenv](https://pypi.org/project/python-dotenv/) — Environment variable management for API credentials
- [jsonschema](https://python-jsonschema.readthedocs.io/) — JSON validation against supplier API schemas
- [tenacity](https://tenacity.readthedocs.io/) — Retry logic with exponential backoff for resilient API calls

---

## Recent Updates (v2)

**New in this release:**
- Added AutoBot and AutoBot v2 for batch supplier product processing
- Added sample JSON reference files from Hyperpure and Purix APIs
- Enhanced project documentation with detailed usage examples
- Improved error handling and logging across all modules
- Added support for multi-source price comparison workflows

---

*Internship project — Prockured, 2025*
*Extended with AutoBot capabilities for scalable batch automation — 2025 v2 Update*
