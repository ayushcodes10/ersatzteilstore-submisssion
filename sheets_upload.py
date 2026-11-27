#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

from ebay_store_scraper import COLUMNS, _excel_safe

log = logging.getLogger("sheets_upload")

CHUNK_ROWS = 5000
MAX_CELLS = 10_000_000


def spreadsheet_id(value: str) -> str:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise ValueError(f"not a Google Sheet URL or id: {value!r}")


def to_matrix(rows: list[dict], image_preview: bool) -> list[list[Any]]:
    header = (["image_preview"] if image_preview else []) + COLUMNS
    out: list[list[Any]] = [header]
    for r in rows:
        line = [_cell(r.get(c)) for c in COLUMNS]
        if image_preview:
            url = r.get("main_image_url") or ""
            line.insert(0, f'=IMAGE("{url}")' if url.startswith("https://") and '"' not in url else "")
        out.append(line)
    return out


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return v
    return _excel_safe(str(v))


def with_retry(fn, *args, attempts: int = 6, **kwargs):
    from gspread.exceptions import APIError
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code not in (429, 500, 502, 503) or i == attempts - 1:
                raise
            wait = min(90, 5 * 2 ** i)
            log.warning("Sheets API %s, retrying in %ss", code, wait)
            time.sleep(wait)


def write_tab(sh, title: str, matrix: list[list[Any]]) -> None:
    import gspread
    rows, cols = len(matrix), len(matrix[0])
    try:
        ws = sh.worksheet(title)
        with_retry(ws.clear)
        with_retry(ws.resize, rows=max(rows, 2), cols=cols)
    except gspread.WorksheetNotFound:
        ws = with_retry(sh.add_worksheet, title=title, rows=max(rows, 2), cols=cols)
    for start in range(0, rows, CHUNK_ROWS):
        block = matrix[start:start + CHUNK_ROWS]
        with_retry(ws.update, values=block, range_name=f"A{start + 1}", value_input_option="USER_ENTERED")
        log.info("%s: %d/%d rows", title, min(start + CHUNK_ROWS, rows) - 1, rows - 1)
    with_retry(ws.freeze, rows=1)
    if matrix[0][0] == "image_preview" and rows > 1:
        with_retry(sh.batch_update, {"requests": [
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 1, "endIndex": rows},
                "properties": {"pixelSize": 80}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 90}, "fields": "pixelSize"}},
        ]})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Upload scraper output to a Google Sheet.")
    p.add_argument("--sheet", required=True, help="Google Sheet URL or id (share it with the service account)")
    p.add_argument("--credentials", default="service_account.json", help="service account JSON key")
    p.add_argument("--input", default="output/all_stores.json")
    p.add_argument("--image-preview", action="store_true", help="add an =IMAGE() thumbnail column")
    p.add_argument("--no-store-tabs", action="store_true", help="only write the combined tab")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    try:
        sid = spreadsheet_id(a.sheet)
    except ValueError as e:
        p.error(str(e))
    src = Path(a.input)
    if not src.exists():
        p.error(f"{src} not found; run ebay_store_scraper.py first")
    if not Path(a.credentials).exists():
        p.error(f"{a.credentials} not found; see README (Google Sheets upload)")
    rows = json.loads(src.read_text(encoding="utf-8"))
    if not rows:
        p.error("no rows to upload")

    tabs: list[tuple[str, list[dict]]] = [("All stores", rows)]
    if not a.no_store_tabs:
        for store in sorted({r["store"] for r in rows}):
            tabs.append((store[:99], [r for r in rows if r["store"] == store]))
    cells = sum((len(t[1]) + 1) * (len(COLUMNS) + 1) for t in tabs)
    if cells > MAX_CELLS:
        log.warning("%d cells exceeds the Sheets limit; writing the combined tab only", cells)
        tabs = tabs[:1]

    import gspread
    gc = gspread.service_account(filename=a.credentials)
    sh = with_retry(gc.open_by_key, sid)
    for title, subset in tabs:
        write_tab(sh, title, to_matrix(subset, a.image_preview))

    summary_path = src.with_name("summary.json")
    if summary_path.exists():
        s = json.loads(summary_path.read_text(encoding="utf-8"))
        matrix = [["metric", "value"], ["generated", s.get("generated", "")], ["total products", s.get("total", 0)],
                  ["products with full detail", s.get("with_details", 0)], ["ended listings", s.get("ended", 0)],
                  ["detail fetch failures", s.get("errors", 0)], ["total image urls", s.get("total_images", 0)]]
        matrix += [[f"products in {k}", v] for k, v in s.get("counts", {}).items()]
        write_tab(sh, "Summary", matrix)

    for ws in with_retry(sh.worksheets):
        if ws.title in ("Sheet1", "Tabelle1") and ws.row_count and not with_retry(ws.get_all_values):
            with_retry(sh.del_worksheet, ws)
    log.info("done: https://docs.google.com/spreadsheets/d/%s", sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
