import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ebay_store_scraper as s


@pytest.mark.parametrize("inp,slug", [
    ("https://www.ebay.de/str/k11ersatzteilshop?_trksid=p3542580.m47492.l74602", "k11ersatzteilshop"),
    ("https://www.ebay.de/str/ersatzteilwelt24/", "ersatzteilwelt24"),
    ("www.ebay.de/str/etvggmbhersatzteilehausgerate", "etvggmbhersatzteilehausgerate"),
    ("https://www.ebay.de/str/ersatzteilwelt24/Haushaltsgerate/_i.html?_sacat=20710", "ersatzteilwelt24"),
    ("k11ersatzteilshop", "k11ersatzteilshop"),
])
def test_store_slug(inp, slug):
    assert s.store_slug(inp) == slug


@pytest.mark.parametrize("bad", ["", "https://www.ebay.de/itm/123", "https://www.ebay.de/", "bad slug!"])
def test_store_slug_rejects(bad):
    with pytest.raises(ValueError):
        s.store_slug(bad)


def test_store_domain():
    assert s.store_domain("https://www.ebay.com/str/x") == "www.ebay.com"
    assert s.store_domain("k11") == "www.ebay.de"


def test_listing_url_is_sorted_and_paged():
    u = s.listing_url("www.ebay.de", "k11", 3)
    assert "_pgn=3" in u and "_sop=10" in u and "_ipg=240" in u


@pytest.mark.parametrize("text,val,cur,mx", [
    ("EUR 18,74", 18.74, "EUR", None),
    ("EUR 1.234,56", 1234.56, "EUR", None),
    ("12,99 €", 12.99, "EUR", None),
    ("EUR 5,00 bis EUR 9,99", 5.0, "EUR", 9.99),
    ("US $1,234.50", 1234.5, "USD", None),
    ("EUR 1.234", 1234.0, "EUR", None),
    ("", None, "", None),
    ("Preis auf Anfrage", None, "", None),
])
def test_parse_price(text, val, cur, mx):
    assert s.parse_price(text) == (val, cur, mx)


def test_upscale_image():
    assert s.upscale_image("https://i.ebayimg.com/images/g/abc/s-l300.webp") == \
        "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"
    assert s.upscale_image("//i.ebayimg.com/images/g/abc/s-l64.jpg 2x") == \
        "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"
    assert s.upscale_image("https://i.ebayimg.com/thumbs/images/g/abc/s-l225.jpg", 300).endswith("s-l300.jpg")
    assert s.upscale_image("") == ""
    assert s.upscale_image(None) == ""


def test_dedupe_images_keeps_order_and_drops_junk():
    urls = [
        "https://i.ebayimg.com/images/g/AAA/s-l140.jpg",
        "https://i.ebayimg.com/images/g/AAA/s-l1600.webp",
        "data:image/gif;base64,R0lGOD",
        None,
        "https://ir.ebaystatic.com/spacer.gif",
        "https://i.ebayimg.com/images/g/BBB/s-l500.jpg",
        "https://kremplshop.de/file/shopimage/20488946586",
        "https://kremplshop.de/file/shopimage/20488946586",
    ]
    assert s.dedupe_images(urls) == [
        "https://i.ebayimg.com/images/g/AAA/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/BBB/s-l1600.jpg",
        "https://kremplshop.de/file/shopimage/20488946586",
    ]


def test_condition_and_specs():
    specs = {"Artikelzustand": "Neu: Neuer, unbenutzter Artikel ... Mehr zum Thema\nÜber den Zustand",
             "Marke": "Europart", "EAN": "4016417001581"}
    assert s.condition_from_specs(specs) == "Neu"
    assert s.pick_spec(specs, "Marke", "Brand") == "Europart"
    assert s.pick_spec(specs, "MPN") == ""


@pytest.mark.parametrize("title,url,status,kind", [
    ("Sicherheitsmaßnahme | eBay", "https://www.ebay.de/splashui/captcha?x", 200, "captcha"),
    ("Error Page | eBay", "https://www.ebay.de/str/x", 403, "blocked"),
    ("K11 Ersatzteilshop | eBay.de Shops", "https://www.ebay.de/str/k11", 200, None),
])
def test_is_block_page(title, url, status, kind):
    assert s.is_block_page(title, url, status) == kind


def test_jsonl_store_resume_and_torn_line(tmp_path):
    p = tmp_path / "x.jsonl"
    st = s.JsonlStore(p)
    st.put({"item_id": "1", "v": 1})
    st.put({"item_id": "2", "v": 1})
    st.put({"item_id": "1", "v": 2})
    st.close()
    with p.open("a") as fh:
        fh.write('{"item_id": "3", "v"')
    st2 = s.JsonlStore(p)
    st2.close()
    assert len(st2) == 2 and st2.records["1"]["v"] == 2 and "3" not in st2


def test_export_merges_listing_and_details(tmp_path):
    d = tmp_path / "data" / "shop"
    lst = s.JsonlStore(d / "listing.jsonl")
    lst.put({"item_id": "111", "store": "shop", "title": "Card title", "price": 1.0, "price_max": None,
             "currency": "EUR", "image_url": "https://i.ebayimg.com/images/g/A/s-l1600.jpg",
             "item_url": "https://www.ebay.de/itm/111", "first_seen": "t"})
    lst.put({"item_id": "222", "store": "shop", "title": "=HYPERLINK(\"x\")", "price": 2.0, "price_max": None,
             "currency": "EUR", "image_url": "", "item_url": "https://www.ebay.de/itm/222", "first_seen": "t"})
    lst.close()
    det = s.JsonlStore(d / "details.jsonl")
    det.put({"item_id": "111", "status": "ok", "title": "Full title", "price": 1.5, "price_max": None,
             "currency": "EUR", "images": ["https://i.ebayimg.com/images/g/A/s-l1600.jpg",
                                           "https://i.ebayimg.com/images/g/B/s-l1600.jpg"],
             "specs": {"Marke": "X"}, "brand": "X", "fetched_at": "t"})
    det.close()
    counts = s.export(tmp_path, ["shop"])
    assert counts == {"shop": 2}
    rows = json.loads((tmp_path / "output" / "all_stores.json").read_text())
    r1 = next(r for r in rows if r["item_id"] == "111")
    assert r1["title"] == "Full title" and r1["image_count"] == 2 and r1["price"] == 1.5
    r2 = next(r for r in rows if r["item_id"] == "222")
    assert r2["detail_status"] == "not_fetched" and r2["image_count"] == 0
    csv_text = (tmp_path / "output" / "shop.csv").read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in csv_text
    assert (tmp_path / "output" / "all_stores.xlsx").exists()


def test_build_detail_ended_and_redirect():
    d = {"images": [], "og_image": "https://i.ebayimg.com/images/g/Z/s-l500.jpg", "specs": {"Marke": "Bosch"},
         "price_raw": "EUR 9,99", "page_item_id": "999999999", "ended": False, "title": "T",
         "categories": ["eBay", "Haushaltsgeräte"], "availability": ""}
    out = s.build_detail("111111111", d)
    assert out["status"] == "ended" and out["redirected_to"] == "999999999"
    assert out["images"] == ["https://i.ebayimg.com/images/g/Z/s-l1600.jpg"]
    assert out["brand"] == "Bosch" and out["category_path"] == "Haushaltsgeräte" and out["price"] == 9.99


def test_spec_columns_and_flatten():
    rows = [{"specs": {"Marke": "Beko", "Artikelzustand": "Neu: Neuer, unbenutzter ... Mehr zum Thema", "Modell": "X"}}] * 3
    rows += [{"specs": {"Marke": "Bosch", "Selten": "y"}}]
    cols = s.spec_columns(rows, min_items=2)
    assert cols[0] == "Artikelzustand" and cols[1] == "Marke" and "Selten" not in cols
    flat = s.flatten({**rows[0], "title": "t", "item_id": "1"}, cols)
    assert flat["Marke"] == "Beko" and flat["Artikelzustand"] == "Neu" and flat["title"] == "t"
    assert list(flat)[:len(s.LEADING_COLUMNS)] == s.LEADING_COLUMNS


def test_export_writes_store_tabs(tmp_path):
    from openpyxl import load_workbook
    d = tmp_path / "data" / "shop"
    lst = s.JsonlStore(d / "listing.jsonl")
    det = s.JsonlStore(d / "details.jsonl")
    for i in range(25):
        iid = str(100000000 + i)
        lst.put({"item_id": iid, "store": "shop", "title": "T", "price": 1.0, "price_max": None, "currency": "EUR",
                 "image_url": "https://i.ebayimg.com/images/g/A/s-l1600.jpg",
                 "item_url": f"https://www.ebay.de/itm/{iid}", "first_seen": "t"})
        det.put({"item_id": iid, "status": "ok", "title": "T", "price": 1.0, "currency": "EUR",
                 "images": ["https://i.ebayimg.com/images/g/A/s-l1600.jpg"], "specs": {"Marke": "Beko"},
                 "fetched_at": "t"})
    lst.close(); det.close()
    s.export(tmp_path, ["shop"])
    wb = load_workbook(tmp_path / "output" / "all_stores.xlsx", read_only=True)
    assert wb.sheetnames == ["shop", "Summary"]
    header = [c.value for c in next(wb["shop"].iter_rows(max_row=1))]
    assert "Marke" in header and header.index("Marke") > header.index("image_urls")


def test_stripped_page_is_partial_and_not_complete():
    d = {"images": [], "og_image": "https://i.ebayimg.com/images/g/Z/s-l500.jpg", "specs": {}, "price_raw": "EUR 1,00",
         "page_item_id": "111111111", "ended": False, "title": "T", "categories": [], "availability": ""}
    out = s.build_detail("111111111", d)
    assert out["status"] == "partial" and not s.is_complete(out)
    assert s.is_complete({"status": "ok", "specs": {"Marke": "X"}, "images": []})
    assert s.is_complete({"status": "ended"}) and not s.is_complete({"status": "ok", "specs": {}, "images": []})
