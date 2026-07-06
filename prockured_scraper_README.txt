PROCKURED CSV PRODUCT IMAGE SCRAPER

Files included:
1. prockured_product_image_scraper.py
   Main reusable scraper.

2. veeba_input.csv
   Sample input CSV made from the Veeba list.

Recommended folder setup:
    scraper_work/
        prockured_product_image_scraper.py
        veeba_input.csv
        saved_pages/
            veebaa.htm
            amazon_page_1.htm
            amazon_page_2.htm
            bigbasket_page_1.html

Best first run, safe review only:
    py prockured_product_image_scraper.py veeba_input.csv --html-folder saved_pages --no-download

Download after checking matches.csv:
    py prockured_product_image_scraper.py veeba_input.csv --html-folder saved_pages

Live search fallback:
    py prockured_product_image_scraper.py veeba_input.csv --html-folder saved_pages --live --sources hyperpure,bigbasket,amazon

Browser live mode if normal requests fail:
    py -m pip install playwright
    py -m playwright install chromium
    py prockured_product_image_scraper.py veeba_input.csv --html-folder saved_pages --live --browser-live

Important options:
    --min-score 72             Default strict match score.
    --loose-pack               Allows candidate to match even when pack count is missing.
    --enrich-pages             Opens matched product pages and tries to collect more gallery images.
    --sources hyperpure,amazon  Run only selected sources.
    --no-download              Make reports only, do not download files.

Output:
    prockured_image_scraper_output/indexed_products.csv
    prockured_image_scraper_output/matches.csv
    prockured_image_scraper_output/unmatched_products.csv
    prockured_image_scraper_output/image_links.csv
    prockured_image_scraper_output/download_report.csv
    prockured_image_scraper_output/images/<Brand>/<Product Title - Prockured>/

How the matching is made strict:
    - Brand must match.
    - Major quantity must match, e.g. 1kg vs 300g is rejected.
    - Pack count must match, e.g. Pack of 100 vs Pack of 90 is rejected.
    - Multipack/combo candidates are rejected when the input is a single pack.
    - Required phrases like Tasty Pixel and Chef's Choice must remain present.
    - Distinct flavour/style tokens like Jalapeno, Tandoori, Schezwan, Chipotle, Mint, etc. are checked so wrong variants are avoided.
