import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sheets_upload as su
from ebay_store_scraper import LEADING_COLUMNS, TRAILING_COLUMNS


def test_spreadsheet_id_from_url_and_id():
    sid = "1cxncB7GDEkebt8LnhNNJu1GTUNO1j1USQzWC9SejQn0"
    assert su.spreadsheet_id(f"https://docs.google.com/spreadsheets/d/{sid}/edit?gid=0#gid=0") == sid
    assert su.spreadsheet_id(sid) == sid
    with pytest.raises(ValueError):
        su.spreadsheet_id("https://example.com/x")


def test_to_matrix_preview_and_escaping():
    rows = [{"store": "s", "item_id": "1", "title": "=cmd()", "price": 2.5, "price_max": None,
             "main_image_url": "https://i.ebayimg.com/images/g/A/s-l1600.jpg", "image_count": 1}]
    m = su.to_matrix(rows, image_preview=True)
    assert m[0] == ["image_preview"] + LEADING_COLUMNS + TRAILING_COLUMNS
    assert m[1][0] == '=IMAGE("https://i.ebayimg.com/images/g/A/s-l1600.jpg")'
    row = dict(zip(m[0], m[1]))
    assert row["title"] == "'=cmd()" and row["price"] == 2.5 and row["price_max"] == ""


def test_to_matrix_without_preview():
    m = su.to_matrix([{"item_id": "1"}], image_preview=False)
    assert m[0] == LEADING_COLUMNS + TRAILING_COLUMNS and len(m[1]) == len(m[0])
