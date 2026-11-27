# eBay Store Scraper

Scrapes every product, including the full image gallery, from eBay stores:

- https://www.ebay.de/str/etvggmbhersatzteilehausgerate
- https://www.ebay.de/str/k11ersatzteilshop
- https://www.ebay.de/str/ersatzteilwelt24

Output: one CSV per store, a combined CSV / JSON / XLSX, and a `summary.json`, ready to import into Google Sheets.

## Why a real browser

eBay is protected by Akamai Bot Manager. Plain HTTP clients (requests, curl, TLS-impersonating clients) and stock headless browsers receive `HTTP 403`. The scraper drives an installed Google Chrome through Playwright with a persistent profile, which passes the checks. If eBay shows a CAPTCHA, the run pauses until it is solved in the open window, and the solved session is kept in the profile for later runs.

## How it works

| Phase | What it does | Output |
|---|---|---|
| `listing` | Pages through the store grid (`_ipg=240`, newest first so pagination stays stable while sellers list new items). Records item id, title, price and main image for every product, and stops at the real end of the store. | `data/<store>/listing.jsonl` |
| `details` | Opens every item page and extracts the complete gallery (1600 px), condition, brand, MPN, EAN, category path, availability, all item specifics, and whether the listing has ended. | `data/<store>/details.jsonl` |
| `export` | Merges both phases. Listing data is the fallback for items whose detail page could not be fetched. | `output/*.csv`, `output/all_stores.json`, `output/all_stores.xlsx`, `output/summary.json` |

### Reliability

- **Resumable.** Every record is appended and fsynced as soon as it is scraped. After a crash, Ctrl+C, reboot or block, re-running the same command continues where it stopped. A torn last line from a crash is skipped.
- **Deduplicated** by eBay item id, so repeats from shifting pagination never produce duplicate rows.
- **403 handling.** Akamai rejects a share of requests at random. Each URL gets quick jittered retries, then a cooldown and warm-up through the eBay home page. The run aborts cleanly (and stays resumable) only if 403s continue for a long time across all tabs.
- **CAPTCHA handling.** The run pauses with a terminal bell until the CAPTCHA is solved. In `--headless` mode it exits with instructions instead.
- **Failure isolation.** One bad item is recorded as `error` / `blocked` and retried on the next run; it never stops the scrape. Ended or redirected listings are marked `ended`.
- **Safe output.** CSV and JSON files are written atomically. Cells that start with `=`, `+`, `-` or `@` are escaped to prevent formula injection, and long cells are truncated to stay under the Google Sheets 50,000-character limit.
- **Polite.** Random delays between requests and a small number of tabs. The browser skips downloading images, fonts and media because only their URLs are needed.

## Setup

Python 3.10+ and Google Chrome are required. Without Chrome, the bundled Chromium is used as a fallback.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
python ebay_store_scraper.py
```

Runs all three stores through all three phases. Useful options:

```bash
python ebay_store_scraper.py --phases listing export
python ebay_store_scraper.py --stores k11ersatzteilshop --phases details export
python ebay_store_scraper.py --max-pages 1 --max-items 20
python ebay_store_scraper.py --concurrency 3 --delay 0.8 2.0
python ebay_store_scraper.py --restart-listing
python ebay_store_scraper.py --refresh-details
```

On macOS, use `caffeinate -i python ebay_store_scraper.py` for long runs so the machine does not go to sleep. Run `python ebay_store_scraper.py --help` for every flag.

## Output columns

| Column | Description |
|---|---|
| store | store slug |
| item_id | eBay item number |
| title | listing title |
| price, price_max, currency | price (`price_max` is set for variation price ranges) |
| condition, brand, mpn, ean | from the item specifics |
| category_path | eBay category breadcrumb |
| availability | for example "3 verfügbar" |
| item_url | link to the listing |
| main_image_url | first gallery image, 1600 px |
| image_count, image_urls | every gallery image, separated by `" \| "` |
| item_specifics | all item specifics as JSON |
| detail_status | `ok`, `ended`, `blocked`, `error` or `not_fetched` |
| first_seen, details_fetched_at | UTC timestamps |

## Google Sheets

**Manual import.** In the Sheet, choose File > Import > Upload, pick `output/all_stores.csv` (or one store's CSV), then choose "Replace current sheet" or "Insert new sheet".

**Automatic upload.** This uses a Google Cloud service account:

1. In Google Cloud Console, enable the Google Sheets API.
2. Create a service account, add a JSON key, and save it as `service_account.json`.
3. Share the Sheet with the service account's email address as Editor.
4. Run:

```bash
python sheets_upload.py --sheet "https://docs.google.com/spreadsheets/d/<id>/edit" --image-preview
```

It writes an "All stores" tab, one tab per store and a "Summary" tab, in chunks, with retries on API rate limits. `--image-preview` adds an `=IMAGE()` thumbnail column.

## Tests

```bash
pytest -q
```
