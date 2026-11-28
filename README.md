# eBay Store Product Scraper

A resumable Python tool that extracts the complete product catalogue of eBay stores, including the image gallery and item specifics of every listing, and exports it as CSV, XLSX and JSON for analysis or import into Google Sheets.

## Stores covered

| Store | Products |
|---|---|
| [etvggmbhersatzteilehausgerate](https://www.ebay.de/str/etvggmbhersatzteilehausgerate) | 22,859 |
| [k11ersatzteilshop](https://www.ebay.de/str/k11ersatzteilshop) | 22,709 |
| [ersatzteilwelt24](https://www.ebay.de/str/ersatzteilwelt24) | 27,314 |
| **Total** | **72,882** |

The results are in [`output/`](output): one CSV and one XLSX per store, `all_stores.xlsx` (all stores as tabs, every Artikelmerkmal as a column) and `summary.json`. Running the export locally also writes the combined `all_stores.csv` and `all_stores.json`, which are too large to keep in the repository.

## How it works

The scraper runs in three independent phases. Each phase saves its progress continuously and can be re-run at any time.

| Phase | Description | Output |
|---|---|---|
| `listing` | Walks every page of the store catalogue (200 products per page) and records the item ID, title, price, link and main image of each product. | `data/<store>/listing.jsonl` |
| `details` | Retrieves each product page and extracts the full image gallery (1600 px), condition, brand, MPN, EAN, category path, availability and all item specifics. | `data/<store>/details.jsonl` |
| `export` | Merges both phases into the final files. | `output/` |

Pages are loaded with Playwright in a regular Chrome browser. In the `details` phase, product pages are requested in parallel and parsed without being rendered, which keeps the run fast.

## Design

- **Resumable.** Every record is written to disk as soon as it is collected. An interrupted run continues from where it stopped when restarted with the same command.
- **Complete pagination.** The catalogue is read in a stable sort order (newest first) so items cannot shift between pages during a run. A run ends only on a genuinely short last page or on repeated pages with no new items. An optional second pass with a different sort order (`--sort`) adds any items the first pass did not see.
- **Deduplicated.** Records are keyed by eBay item ID, so each product appears exactly once.
- **Fault tolerant.** Network errors and server errors are retried with jittered exponential backoff. An item that cannot be retrieved is recorded with its status and retried on the next run, without stopping the rest.
- **Accurate images.** Gallery images are normalised to the largest available size and deduplicated. Images hosted outside eBay are kept.
- **Ended listings** are detected and marked `ended`.
- **Safe output.** Files are written atomically. Cells that could be interpreted as spreadsheet formulas are escaped, and cell lengths stay within the Google Sheets limit.
- **Considerate.** Requests are paced with random delays and limited concurrency.

## Requirements

- Python 3.10 or newer
- Google Chrome (the Chromium build bundled with Playwright is used if Chrome is not installed)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

Run all stores through all phases:

```bash
python ebay_store_scraper.py
```

Common options:

```bash
python ebay_store_scraper.py --stores k11ersatzteilshop
python ebay_store_scraper.py --phases listing export
python ebay_store_scraper.py --phases details --parallel-fetches 10
python ebay_store_scraper.py --phases listing --sort 15
python ebay_store_scraper.py --max-pages 1 --max-items 20
python ebay_store_scraper.py --phases export
```

| Option | Purpose |
|---|---|
| `--stores` | Store URLs or names (defaults to the three stores above) |
| `--phases` | Any of `listing`, `details`, `export` |
| `--sort` | Catalogue sort order for the listing pass: `10` newest, `15` price ascending, `16` price descending, `12` best match |
| `--detail-mode` | `fetch` (parallel, default) or `browser` (opens each page individually) |
| `--parallel-fetches`, `--batch-size` | Throughput of the details phase |
| `--delay MIN MAX` | Random pause between requests, in seconds |
| `--max-pages`, `--max-items` | Limits for test runs |
| `--restart-listing`, `--refresh-details` | Discard saved progress and collect again |
| `--until-complete` | Keep running, with a fresh browser profile for each run, until every product is complete |
| `--headless` | Run without a browser window |

For long unattended runs:

```bash
python ebay_store_scraper.py --until-complete --parallel-fetches 4 --batch-size 100 --delay 1.5 3
```

Run `python ebay_store_scraper.py --help` for the full list. On macOS, prefix long runs with `caffeinate -i` so the machine does not sleep.

## Output format

| Column | Description |
|---|---|
| `store` | Store name |
| `item_id` | eBay item number |
| `title` | Listing title |
| `price`, `price_max`, `currency` | Price; `price_max` is set when variations have a price range |
| `condition`, `brand`, `mpn`, `ean` | From the item specifics |
| `category_path` | eBay category breadcrumb |
| `availability` | Quantity information, for example "3 verfügbar" |
| `item_url` | Link to the listing |
| `main_image_url` | Main product image, 1600 px |
| `image_count`, `image_urls` | Every gallery image, separated by `" \| "` |
| `item_specifics` | All item specifics as JSON |
| `detail_status` | `ok`, `ended`, `not_fetched`, `blocked` or `error` |
| `first_seen`, `details_fetched_at` | UTC timestamps |

Every row has a title, price, link and main image. Gallery and item-specific columns are filled for each row whose `detail_status` is `ok`, and running the `details` phase again fills in the rest.

## Google Sheets

**Import.** In Google Sheets, choose File > Import > Upload and select `output/all_stores.xlsx`, then "Replace spreadsheet". This creates one tab per store. A single store's CSV can also be imported into its own tab.

**Automated upload.** `sheets_upload.py` writes an "All stores" tab, one tab per store and a "Summary" tab, using a Google Cloud service account:

1. Enable the Google Sheets API in a Google Cloud project.
2. Create a service account, download a JSON key, and save it as `service_account.json`.
3. Share the spreadsheet with the service account's email address as Editor.
4. Run:

```bash
python sheets_upload.py --sheet "https://docs.google.com/spreadsheets/d/<id>/edit" --image-preview
```

`--image-preview` adds a thumbnail column using `=IMAGE()`.

## Tests

```bash
pytest -q
```

## Project structure

```
ebay_store_scraper.py   scraper: listing, details and export phases
sheets_upload.py        Google Sheets uploader
tests/                  unit tests
output/                 exported results
requirements.txt
```
