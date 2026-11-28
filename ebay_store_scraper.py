#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

DEFAULT_STORES = [
    "https://www.ebay.de/str/etvggmbhersatzteilehausgerate",
    "https://www.ebay.de/str/k11ersatzteilshop",
    "https://www.ebay.de/str/ersatzteilwelt24",
]

ITEMS_PER_PAGE = 240
SORT_NEWEST_FIRST = "10"
IMAGE_SIZE = 1600
FULL_PAGE = 200
FETCH_TIMEOUT_MS = 30000

log = logging.getLogger("ebay_scraper")


def store_slug(store: str) -> str:
    store = store.strip()
    if not store:
        raise ValueError("empty store")
    if "://" not in store and "/" not in store:
        slug = store
    else:
        if "://" not in store:
            store = "https://" + store
        parts = [p for p in urlparse(store).path.split("/") if p]
        if len(parts) < 2 or parts[0] != "str":
            raise ValueError(f"not an eBay store URL: {store!r}")
        slug = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", slug):
        raise ValueError(f"invalid store slug: {slug!r}")
    return slug


def headful_user_agent(chrome_major: str, platform: str = sys.platform) -> str:
    if platform == "darwin":
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
    elif platform.startswith("win"):
        os_part = "Windows NT 10.0; Win64; x64"
    else:
        os_part = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major}.0.0.0 Safari/537.36")


def store_domain(store: str) -> str:
    if "://" in store or store.startswith("www."):
        host = urlparse(store if "://" in store else "https://" + store).netloc
        if host:
            return host
    return "www.ebay.de"


def listing_url(domain: str, slug: str, page: int, sort: str = SORT_NEWEST_FIRST) -> str:
    q = {"_ipg": ITEMS_PER_PAGE, "_pgn": page, "_sop": sort, "rt": "nc"}
    return f"https://{domain}/str/{slug}?{urlencode(q)}"


_IMG_SIZE_RE = re.compile(r"/s-l\d+(\.(?:jpg|jpeg|png|webp|gif))", re.I)
_IMG_GUID_RE = re.compile(r"/images/g/([^/]+)/", re.I)


def upscale_image(url: str | None, size: int = IMAGE_SIZE) -> str:
    if not url:
        return ""
    url = url.strip().split(" ")[0]
    if url.startswith("//"):
        url = "https:" + url
    if "ebayimg.com" not in url:
        return url
    if _IMG_SIZE_RE.search(url):
        return _IMG_SIZE_RE.sub(f"/s-l{size}.jpg", url)
    return url


def image_key(url: str) -> str:
    m = _IMG_GUID_RE.search(url or "")
    return m.group(1) if m else (url or "")


def dedupe_images(urls: Iterable[str | None]) -> list[str]:
    out, seen = [], set()
    for u in urls:
        u = upscale_image(u)
        if not u or not u.startswith(("https://", "http://")) or "ebaystatic.com" in u:
            continue
        k = image_key(u)
        if k in seen:
            continue
        seen.add(k)
        out.append(u)
    return out


_PRICE_RE = re.compile(r"(EUR|€|CHF|GBP|£|USD|US \$|\$)\s*([\d.,]+)|([\d.,]+)\s*(EUR|€)")


def parse_price(text: str | None) -> tuple[float | None, str, float | None]:
    if not text:
        return None, "", None
    found = []
    for m in _PRICE_RE.finditer(text):
        cur = (m.group(1) or m.group(4) or "").replace("€", "EUR").replace("£", "GBP")
        cur = {"US $": "USD", "$": "USD"}.get(cur, cur)
        num = m.group(2) or m.group(3)
        val = _parse_number(num, cur)
        if val is not None:
            found.append((val, cur))
    if not found:
        return None, "", None
    lo = found[0]
    hi = found[1][0] if len(found) > 1 and ("bis" in text or " to " in text or "-" in text) else None
    return lo[0], lo[1], hi


def _parse_number(num: str, currency: str) -> float | None:
    num = num.strip(".,")
    if not num:
        return None
    if currency in ("USD", "GBP"):
        num = num.replace(",", "")
    elif "," in num:
        num = num.replace(".", "").replace(",", ".")
    elif num.count(".") == 1 and len(num.split(".")[1]) == 3:
        num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


def clean_spec_value(v: str) -> str:
    v = re.split(r"\s*(?:\.\.\.\s*)?(?:Mehr zum Thema|Mehr erfahren|Read more|See all condition definitions)", v)[0]
    return re.sub(r"\s+", " ", v).strip()


def condition_from_specs(specs: dict[str, str]) -> str:
    for k in ("Artikelzustand", "Zustand", "Condition", "Item condition"):
        if k in specs:
            return clean_spec_value(specs[k]).split(":")[0].strip()
    return ""


def pick_spec(specs: dict[str, str], *names: str) -> str:
    low = {k.lower(): v for k, v in specs.items()}
    for n in names:
        if n.lower() in low:
            return clean_spec_value(low[n.lower()])
    return ""


def is_block_page(title: str, url: str, status: int | None) -> str | None:
    t = (title or "").lower()
    u = (url or "").lower()
    if "splashui/captcha" in u or "sicherheitsmaßnahme" in t or "security measure" in t \
            or "pardon our interruption" in t:
        return "captcha"
    if status == 403 or "error page | ebay" in t or "access denied" in t:
        return "blocked"
    return None


def is_complete(rec: dict | None) -> bool:
    if not rec:
        return False
    if rec.get("status") == "ended":
        return True
    return rec.get("status") == "ok" and bool(rec.get("specs") or rec.get("images"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class JsonlStore:

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, dict] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("%s:%d unreadable line skipped", path.name, n)
                        continue
                    if rec.get("item_id"):
                        self.records[str(rec["item_id"])] = rec
        self._fh = path.open("a", encoding="utf-8")

    def put(self, rec: dict) -> None:
        self.records[str(rec["item_id"])] = rec
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()

    def __contains__(self, item_id: str) -> bool:
        return str(item_id) in self.records

    def __len__(self) -> int:
        return len(self.records)


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


CARDS_JS = r"""
() => {
  const cards = [...document.querySelectorAll('article.str-item-card')];
  const out = [];
  for (const c of cards) {
    const a = c.querySelector('a.str-item-card__link') || c.querySelector('a[href*="/itm/"]');
    const href = a ? a.href : '';
    let id = (c.getAttribute('data-testid') || '').replace(/^ig-/, '');
    const m = href.match(/\/itm\/(?:[^/?#]+\/)?(\d{9,})/);
    if (m) id = m[1];
    if (!/^\d{9,}$/.test(id)) continue;
    const img = c.querySelector('img');
    const src = img ? (img.getAttribute('data-src') || img.getAttribute('src') || '') : '';
    const source = c.querySelector('source');
    const lines = (c.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const priceLine = lines.find(l => /(EUR|€|CHF|US \$|£)\s*[\d]/.test(l)) || '';
    out.push({
      item_id: id,
      title: (a && (a.getAttribute('aria-label') || '').trim()) || lines[0] || '',
      price_raw: priceLine,
      card_text: lines.join(' | '),
      thumb: src && !src.startsWith('data:') ? src : (source ? source.getAttribute('srcset') : ''),
      url: href ? href.split('?')[0] : '',
    });
  }
  const pg = Number(new URL(location.href).searchParams.get('_pgn') || '1');
  const hasNext = !!document.querySelector(
      `a[href*="_pgn=${pg + 1}&"], a[href$="_pgn=${pg + 1}"], a.pagination__next:not([aria-disabled="true"])`);
  const noResults = /Keine Artikel gefunden|keine Treffer|No items found|0 Ergebnisse/i.test(document.body.innerText || '');
  return {cards: out, rawCardCount: cards.length, hasNext, noResults, title: document.title, url: location.href};
}
"""

EXTRACT_JS = r"""
(doc, pageUrl) => {
  const txt = s => (s && (s.innerText || s.textContent) || '').replace(/\s+/g, ' ').trim();
  const imgs = [];
  const carousel = doc.querySelector('[data-testid="ux-image-carousel"], .ux-image-carousel, .ux-image-carousel-container, #PicturePanel');
  const scope = carousel || doc;
  scope.querySelectorAll('.ux-image-carousel-item img, [data-testid="ux-image-carousel"] img, #PicturePanel img').forEach(i => {
    imgs.push(i.getAttribute('data-zoom-src') || i.getAttribute('data-src') || i.getAttribute('src'));
  });
  doc.querySelectorAll('.ux-image-grid-item img, .ux-image-filmstrip-carousel img').forEach(i =>
    imgs.push(i.getAttribute('data-zoom-src') || i.getAttribute('data-src') || i.getAttribute('src')));
  const og = doc.querySelector('meta[property="og:image"]');
  const specs = {};
  doc.querySelectorAll('.ux-layout-section-evo__col, .ux-labels-values, dl.ux-labels-values').forEach(r => {
    const l = r.querySelector('.ux-labels-values__labels'), v = r.querySelector('.ux-labels-values__values');
    if (l && v) { const k = txt(l).replace(/:$/, ''); if (k && !(k in specs)) specs[k] = txt(v); }
  });
  const crumbs = [...doc.querySelectorAll('nav.breadcrumbs a span, nav.breadcrumbs a, .seo-breadcrumb-text span')]
      .map(txt).filter(Boolean);
  const status = txt(doc.querySelector('.d-statusmessage, .vim-message, [data-testid="d-statusmessage"]'));
  const head = status || txt(doc.body).slice(0, 4000);
  const ended = /Dieses Angebot wurde (vom Verkäufer )?beendet|Der Verkäufer hat diesen Artikel beendet|Angebot beendet|This listing (was ended|has ended)|listing ended/i.test(head);
  const canonEl = doc.querySelector('link[rel="canonical"]');
  const canon = (canonEl && canonEl.getAttribute('href')) || pageUrl;
  const idm = canon.match(/\/itm\/(?:[^/?#]+\/)?(\d{9,})/) || pageUrl.match(/\/itm\/(?:[^/?#]+\/)?(\d{9,})/);
  return {
    page_item_id: idm ? idm[1] : '',
    title: txt(doc.querySelector('h1.x-item-title__mainTitle, .x-item-title__mainTitle, h1')),
    price_raw: txt(doc.querySelector('.x-price-primary, [data-testid="x-price-primary"], #prcIsum')),
    images: imgs.filter(Boolean),
    og_image: og ? (og.getAttribute('content') || '') : '',
    specs,
    categories: [...new Set(crumbs)],
    availability: txt(doc.querySelector('#qtyAvailability, .x-quantity__availability, [data-testid="x-quantity"] .ux-textspans--SECONDARY')),
    ended,
    doc_title: txt(doc.querySelector('title')),
    url: pageUrl,
  };
}
"""

DETAIL_JS = "() => (" + EXTRACT_JS + ")(document, location.href)"

FETCH_JS = r"""
async ({ids, parallel, retries, timeoutMs}) => {
  const extract = """ + EXTRACT_JS + r""";
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const results = {};
  const queue = ids.slice();
  const one = async (id) => {
    let last = '';
    for (let a = 0; a < retries; a++) {
      try {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), timeoutMs);
        let r;
        try {
          r = await fetch('/itm/' + id, {credentials: 'include', redirect: 'follow', signal: ctl.signal});
        } finally {
          clearTimeout(timer);
        }
        if (/splashui\/captcha/.test(r.url)) return {id, kind: 'captcha'};
        if (r.status === 200) {
          const html = await r.text();
          const doc = new DOMParser().parseFromString(html, 'text/html');
          const t = (doc.querySelector('title') || {}).textContent || '';
          if (/Sicherheitsmaßnahme|Security Measure|Pardon Our Interruption/i.test(t)) return {id, kind: 'captcha'};
          return {id, kind: 'ok', data: extract(doc, r.url)};
        }
        if (r.status === 404 || r.status === 410) return {id, kind: 'gone', http: r.status};
        last = 'HTTP ' + r.status;
      } catch (e) {
        last = String(e && e.message || e);
      }
      await sleep(400 + a * 600 + Math.random() * 400);
    }
    return {id, kind: 'blocked', error: last};
  };
  const workers = Array.from({length: Math.max(1, parallel)}, async () => {
    while (queue.length) { const id = queue.shift(); results[id] = await one(id); }
  });
  await Promise.all(workers);
  return ids.map(id => results[id]);
}
"""

NON_SPEC_KEYS = {
    "versand", "lieferung", "rücknahme", "rückgabe", "zahlungen", "zahlung", "standort",
    "shipping", "delivery", "returns", "payments", "located in", "ebay-artikelnummer",
}

CONSENT_SELECTORS = ["#gdpr-banner-accept", "button#gdpr-banner-accept", "button[data-testid='gdpr-banner-accept']"]


class BlockedError(RuntimeError):
    pass


class SessionBurnedError(BlockedError):
    pass


@dataclass
class Config:
    out_dir: Path
    profile_dir: Path
    headless: bool
    delay: tuple[float, float]
    concurrency: int
    max_pages: int
    max_items: int | None
    captcha_timeout: int
    retries: int
    nav_timeout: int
    refresh_details: bool
    block_retries: int = 10
    load_images: bool = False
    detail_mode: str = "fetch"
    sort: str = SORT_NEWEST_FIRST
    batch_size: int = 60
    parallel_fetches: int = 12
    block_abort: int = 60


BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


async def _block_heavy(route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


class Browser:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self.ctx = None
        self._captcha_lock = asyncio.Lock()
        self.stop = asyncio.Event()
        self.consecutive_blocks = 0

    async def __aenter__(self) -> "Browser":
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self.cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for channel in ("chrome", None):
            try:
                kwargs = await self._launch_kwargs(channel)
                self.ctx = await self._pw.chromium.launch_persistent_context(
                    str(self.cfg.profile_dir), **kwargs)
                break
            except Exception as e:
                last = e
                log.warning("browser channel %s not usable: %s", channel or "chromium",
                            str(e).splitlines()[0])
        if self.ctx is None:
            raise RuntimeError(f"could not start a browser: {last}")
        self.ctx.set_default_navigation_timeout(self.cfg.nav_timeout * 1000)
        if not self.cfg.load_images:
            await self.ctx.route("**/*", _block_heavy)
        return self

    async def _launch_kwargs(self, channel: str | None) -> dict:
        kwargs: dict[str, Any] = dict(
            headless=self.cfg.headless,
            locale="de-DE",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if channel:
            kwargs["channel"] = channel
        if self.cfg.headless:
            probe = await self._pw.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
            try:
                major = probe.version.split(".")[0]
            finally:
                await probe.close()
            kwargs["user_agent"] = headful_user_agent(major)
        return kwargs

    async def __aexit__(self, *exc) -> None:
        try:
            if self.ctx:
                await self.ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def new_page(self):
        return await self.ctx.new_page()

    async def polite_pause(self, scale: float = 1.0) -> None:
        lo, hi = self.cfg.delay
        await asyncio.sleep(random.uniform(lo, hi) * scale)

    async def goto(self, page, url: str, referer: str | None = None) -> None:
        last_err = ""
        blocks = 0
        failures = 0
        while True:
            if self.stop.is_set():
                raise asyncio.CancelledError()
            status = None
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", referer=referer)
                status = resp.status if resp else None
                await self._accept_consent(page)
            except Exception as e:
                failures += 1
                last_err = str(e).splitlines()[0]
                log.warning("nav error (%d/%d) %s: %s", failures, self.cfg.retries, url, last_err)
                if failures >= self.cfg.retries:
                    break
                await asyncio.sleep(min(60, 3 * 2 ** failures))
                continue
            kind = is_block_page(await self._title(page), page.url, status)
            if kind is None:
                if status and status >= 500:
                    failures += 1
                    last_err = f"HTTP {status}"
                    if failures >= self.cfg.retries:
                        break
                    await asyncio.sleep(min(60, 3 * 2 ** failures))
                    continue
                self.consecutive_blocks = 0
                return
            if kind == "captcha":
                await self._solve_captcha(page, url)
                continue
            blocks += 1
            self.consecutive_blocks += 1
            last_err = "HTTP 403 from Akamai"
            if self.consecutive_blocks >= self.cfg.block_abort:
                raise SessionBurnedError(
                    f"{self.consecutive_blocks} consecutive 403s; eBay is refusing this session. "
                    "Wait a while (or solve a CAPTCHA in the window) and re-run to resume.")
            if blocks >= self.cfg.block_retries:
                break
            if blocks % 6 == 0:
                wait = 45 + random.uniform(0, 15)
                log.warning("repeated 403s on %s, cooling down %.0fs", url, wait)
                await asyncio.sleep(wait)
                try:
                    await page.goto(f"https://{urlparse(url).netloc}/", wait_until="domcontentloaded")
                except Exception:
                    pass
            else:
                await asyncio.sleep(min(30, 2 * blocks) + random.uniform(0, 1.5))
        raise BlockedError(f"giving up on {url}: {last_err}")

    async def _title(self, page) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    async def _accept_consent(self, page) -> None:
        for sel in CONSENT_SELECTORS:
            try:
                btn = page.locator(sel)
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.click(timeout=2000)
                    return
            except Exception:
                return

    async def _solve_captcha(self, page, url: str) -> None:
        if self.cfg.headless:
            raise SessionBurnedError(
                "eBay asked for a CAPTCHA. Re-run without --headless and solve it in the "
                "browser window (the solved session is saved in the profile directory).")
        async with self._captcha_lock:
            if is_block_page(await self._title(page), page.url, None) != "captcha":
                return
            try:
                await page.bring_to_front()
            except Exception:
                pass
            print("\a", file=sys.stderr)
            log.warning(">>> eBay CAPTCHA: solve it in the Chrome window. Waiting up to %ds ...",
                        self.cfg.captcha_timeout)
            deadline = time.monotonic() + self.cfg.captcha_timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                if is_block_page(await self._title(page), page.url, None) != "captcha":
                    log.info("CAPTCHA solved, continuing")
                    await asyncio.sleep(2)
                    return
            raise SessionBurnedError("CAPTCHA was not solved in time")


async def scrape_listing(br: Browser, store: str, cfg: Config) -> dict:
    slug, domain = store_slug(store), store_domain(store)
    sdir = cfg.out_dir / "data" / slug
    sdir.mkdir(parents=True, exist_ok=True)
    state_path = sdir / ("listing_state.json" if cfg.sort == SORT_NEWEST_FIRST else f"listing_state_sop{cfg.sort}.json")
    state = read_json(state_path, {})
    if state.get("complete"):
        log.info("[%s] listing already complete (%s items). Use --restart-listing to redo.",
                 slug, state.get("unique_items"))
        return state

    db = JsonlStore(sdir / "listing.jsonl")
    page_no = int(state.get("next_page", 1))
    stale_pages = int(state.get("stale_pages", 0))
    run_id = state.get("run_id") or now_iso()
    page = await br.new_page()
    stop_reason = "max pages reached"
    try:
        while page_no <= cfg.max_pages:
            url = listing_url(domain, slug, page_no, cfg.sort)
            await br.goto(page, url)
            data = None
            for _ in range(3):
                try:
                    await page.wait_for_selector("article.str-item-card", timeout=8000)
                except Exception:
                    pass
                data = await page.evaluate(CARDS_JS)
                if data["cards"] or data["noResults"]:
                    break
                await asyncio.sleep(2)
                await br.goto(page, url)

            if page_no == 1 and not data["cards"] and "/str/" not in data["url"]:
                raise BlockedError(f"store {slug!r} redirected to {data['url']} (store missing or renamed?)")

            new = 0
            for c in data["cards"]:
                if c["item_id"] in db:
                    continue
                price, cur, price_max = parse_price(c["price_raw"])
                db.put({
                    "item_id": c["item_id"], "store": slug,
                    "title": c["title"], "price": price, "price_max": price_max, "currency": cur,
                    "price_raw": c["price_raw"], "card_text": c["card_text"],
                    "thumbnail_url": upscale_image(c["thumb"], 300) if c["thumb"] else "",
                    "image_url": upscale_image(c["thumb"]),
                    "item_url": f"https://{domain}/itm/{c['item_id']}",
                    "listing_page": page_no, "first_seen": now_iso(),
                })
                new += 1
            log.info("[%s] page %d: %d cards, %d new, %d unique total",
                     slug, page_no, len(data["cards"]), new, len(db))

            stale_pages = stale_pages + 1 if new == 0 else 0
            state = {"store": slug, "domain": domain, "run_id": run_id, "next_page": page_no + 1,
                     "stale_pages": stale_pages, "unique_items": len(db), "complete": False,
                     "updated": now_iso()}
            write_json_atomic(state_path, state)

            if not data["cards"]:
                stop_reason = "empty page"
                break
            if not data["hasNext"] and len(data["cards"]) < FULL_PAGE:
                stop_reason = "no next page"
                break
            if stale_pages >= 3:
                stop_reason = "3 pages without new items"
                break
            if cfg.max_items and len(db) >= cfg.max_items:
                stop_reason = "--max-items reached"
                break
            page_no += 1
            await br.polite_pause()
    finally:
        await page.close()
        db.close()

    natural_end = stop_reason in ("empty page", "no next page", "3 pages without new items")
    state.update({"complete": natural_end, "stop_reason": stop_reason, "unique_items": len(db),
                  "updated": now_iso()})
    write_json_atomic(state_path, state)
    log.info("[%s] listing finished: %d unique items (%s)", slug, len(db), stop_reason)
    return state


async def scrape_details(br: Browser, store: str, cfg: Config) -> None:
    slug = store_slug(store)
    sdir = cfg.out_dir / "data" / slug
    listing = JsonlStore(sdir / "listing.jsonl")
    listing.close()
    details = JsonlStore(sdir / "details.jsonl")
    todo = [r for r in listing.records.values()
            if cfg.refresh_details or not is_complete(details.records.get(r["item_id"]))]
    if cfg.max_items:
        todo = todo[: cfg.max_items]
    total = len(todo)
    log.info("[%s] details: %d to fetch (%d already done)", slug, total, len(details))
    if not total:
        details.close()
        return

    if cfg.detail_mode == "fetch":
        try:
            await scrape_details_fetch(br, slug, store_domain(store), todo, details, cfg)
        finally:
            details.close()
        failed = sum(1 for r in details.records.values() if r.get("status") in ("error", "blocked"))
        log.info("[%s] details finished (%d failed; re-run to retry them)", slug, failed)
        return

    queue: asyncio.Queue = asyncio.Queue()
    for r in todo:
        queue.put_nowait(r)
    done = 0
    started = time.monotonic()
    fatal: list[BaseException] = []

    async def worker(wid: int) -> None:
        nonlocal done
        page = await br.new_page()
        try:
            while not queue.empty() and not fatal and not br.stop.is_set():
                rec = queue.get_nowait()
                try:
                    out = await fetch_detail(br, page, rec)
                except SessionBurnedError as e:
                    fatal.append(e)
                    return
                except BlockedError as e:
                    log.warning("[%s] item %s still blocked after retries", slug, rec["item_id"])
                    out = {"item_id": rec["item_id"], "status": "blocked", "error": str(e)[:300],
                           "fetched_at": now_iso()}
                except Exception as e:
                    log.warning("[%s] item %s failed: %s", slug, rec["item_id"], str(e).splitlines()[0])
                    out = {"item_id": rec["item_id"], "status": "error", "error": str(e)[:300],
                           "fetched_at": now_iso()}
                    try:
                        await page.close()
                    except Exception:
                        pass
                    page = await br.new_page()
                details.put(out)
                done += 1
                if done % 25 == 0 or done == total:
                    rate = done / max(1e-6, time.monotonic() - started)
                    eta = (total - done) / rate if rate else 0
                    log.info("[%s] details %d/%d (%.2f/s, ETA %s)", slug, done, total, rate,
                             time.strftime("%H:%M:%S", time.gmtime(eta)))
                await br.polite_pause(0.6)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    try:
        await asyncio.gather(*(worker(i) for i in range(max(1, cfg.concurrency))))
    finally:
        details.close()
    if fatal:
        raise fatal[0]
    errors = sum(1 for r in details.records.values() if r.get("status") in ("error", "blocked"))
    log.info("[%s] details finished (%d failed; re-run to retry them)", slug, errors)


async def fetch_detail(br: Browser, page, rec: dict) -> dict:
    await br.goto(page, rec["item_url"], referer=f"https://{urlparse(rec['item_url']).netloc}/str/{rec['store']}")
    try:
        await page.wait_for_selector(".x-item-title, h1, .ux-image-carousel", timeout=10000)
    except Exception:
        pass
    return build_detail(rec["item_id"], await page.evaluate(DETAIL_JS))


def build_detail(item_id: str, d: dict) -> dict:
    images = dedupe_images(d["images"]) or dedupe_images([d["og_image"]])
    specs = {k: clean_spec_value(v) for k, v in d["specs"].items() if k.lower() not in NON_SPEC_KEYS}
    price, cur, price_max = parse_price(d["price_raw"])
    redirected = bool(d["page_item_id"]) and d["page_item_id"] != item_id
    status = "ended" if (d["ended"] or redirected) else "ok"
    if not d["title"] and not images:
        status = "error"
    elif status == "ok" and not specs and not dedupe_images(d["images"]):
        status = "partial"
    return {
        "item_id": item_id, "status": status,
        "redirected_to": d["page_item_id"] if redirected else "",
        "title": d["title"], "price": price, "price_max": price_max, "currency": cur,
        "price_raw": d["price_raw"],
        "condition": condition_from_specs(specs),
        "brand": pick_spec(specs, "Marke", "Brand"),
        "mpn": pick_spec(specs, "Herstellernummer", "MPN", "Manufacturer Part Number"),
        "ean": pick_spec(specs, "EAN", "GTIN"),
        "category_path": " > ".join(c for c in d["categories"] if c.lower() not in ("ebay", "zurück zur startseite")),
        "availability": d["availability"],
        "images": images,
        "specs": specs,
        "fetched_at": now_iso(),
    }


def session_degraded(batch_len: int, partial: int, completed: int) -> bool:
    if batch_len == 0:
        return False
    if partial * 2 > batch_len:
        return True
    return completed * 10 < batch_len and partial * 4 >= batch_len


def batch_timeout(batch_len: int, cfg: Config) -> float:
    rounds = -(-batch_len // max(1, cfg.parallel_fetches))
    per_item = FETCH_TIMEOUT_MS / 1000 * cfg.block_retries + 2 * cfg.block_retries
    return rounds * per_item + 60


async def scrape_details_fetch(br: Browser, slug: str, domain: str, todo: list[dict],
                               details: JsonlStore, cfg: Config) -> None:
    store_url = f"https://{domain}/str/{slug}"
    page = await br.new_page()
    total, done, started = len(todo), 0, time.monotonic()
    dead_batches = 0
    degraded = 0
    try:
        await br.goto(page, store_url)
        i = 0
        while i < total:
            if br.stop.is_set():
                raise asyncio.CancelledError()
            batch = todo[i:i + cfg.batch_size]
            try:
                results = await asyncio.wait_for(
                    page.evaluate(FETCH_JS, {"ids": [r["item_id"] for r in batch],
                                             "parallel": cfg.parallel_fetches,
                                             "retries": cfg.block_retries,
                                             "timeoutMs": FETCH_TIMEOUT_MS}),
                    timeout=batch_timeout(len(batch), cfg))
            except asyncio.TimeoutError:
                log.warning("[%s] batch did not finish in time; reopening the tab", slug)
                try:
                    await page.close()
                except Exception:
                    pass
                page = await br.new_page()
                await br.goto(page, store_url)
                continue
            except Exception as e:
                log.warning("[%s] batch evaluate failed (%s); reloading store page", slug, str(e).splitlines()[0])
                await br.goto(page, store_url)
                continue
            by_id = {r["item_id"]: r for r in batch}
            captcha = [r for r in results if r["kind"] == "captcha"]
            blocked = [r for r in results if r["kind"] == "blocked"]
            for r in results:
                rec = by_id[r["id"]]
                if r["kind"] == "ok":
                    try:
                        out = build_detail(rec["item_id"], r["data"])
                    except Exception as e:
                        out = {"item_id": rec["item_id"], "status": "error", "error": str(e)[:300], "fetched_at": now_iso()}
                elif r["kind"] == "gone":
                    out = {"item_id": rec["item_id"], "status": "ended", "error": f"HTTP {r.get('http')}",
                           "images": [], "specs": {}, "fetched_at": now_iso()}
                elif r["kind"] == "blocked":
                    out = {"item_id": rec["item_id"], "status": "blocked", "error": r.get("error", "")[:300],
                           "fetched_at": now_iso()}
                else:
                    continue
                details.put(out)
                done += 1
            if captcha:
                log.warning("[%s] CAPTCHA during fetch; opening an item page to solve it", slug)
                await br.goto(page, by_id[captcha[0]["id"]]["item_url"])
                await br.goto(page, store_url)
                continue_from = [by_id[r["id"]] for r in captcha]
                todo.extend(continue_from)
                total = len(todo)
            partial = sum(1 for r in results if r["kind"] == "ok" and r["data"] and not r["data"]["specs"]
                          and not dedupe_images(r["data"]["images"]))
            completed = sum(1 for r in results if r["kind"] == "ok" and r["data"]
                            and (r["data"]["specs"] or dedupe_images(r["data"]["images"])))
            degraded = degraded + 1 if session_degraded(len(batch), partial, completed) else 0
            if degraded >= 2:
                raise SessionBurnedError("eBay is serving stripped pages to this browser session; "
                                         "re-run with a fresh --profile to continue")
            if len(blocked) == len(batch):
                dead_batches += 1
                if dead_batches >= 5:
                    raise SessionBurnedError("every request in 5 batches was refused; re-run later to resume")
                wait = 30 * dead_batches
                log.warning("[%s] whole batch blocked, cooling down %ds", slug, wait)
                await asyncio.sleep(wait)
                await br.goto(page, store_url)
            else:
                dead_batches = 0
            i += len(batch)
            rate = done / max(1e-6, time.monotonic() - started)
            log.info("[%s] details %d/%d (%.1f/s, %d blocked in batch, ETA %s)", slug, min(i, total), total, rate,
                     len(blocked), time.strftime("%H:%M:%S", time.gmtime((total - i) / rate if rate else 0)))
            await br.polite_pause(0.3)
    finally:
        await page.close()


COLUMNS = [
    "store", "item_id", "title", "price", "price_max", "currency", "condition", "brand", "mpn",
    "ean", "category_path", "availability", "item_url", "main_image_url", "image_count",
    "image_urls", "item_specifics", "detail_status", "first_seen", "details_fetched_at",
]
CELL_LIMIT = 49000


def build_rows(out_dir: Path, slug: str) -> list[dict]:
    sdir = out_dir / "data" / slug
    lst = JsonlStore(sdir / "listing.jsonl"); lst.close()
    det = JsonlStore(sdir / "details.jsonl"); det.close()
    rows = []
    for item_id, L in lst.records.items():
        D = det.records.get(item_id, {})
        ok = is_complete(D)
        images = D.get("images") if ok and D.get("images") else ([L["image_url"]] if L.get("image_url") else [])
        rows.append({
            "store": slug,
            "item_id": item_id,
            "title": (D.get("title") if ok and D.get("title") else L.get("title", "")),
            "price": D.get("price") if ok and D.get("price") is not None else L.get("price"),
            "price_max": D.get("price_max") if ok and D.get("price") is not None else L.get("price_max"),
            "currency": (D.get("currency") if ok and D.get("currency") else L.get("currency", "")),
            "condition": D.get("condition", ""),
            "brand": D.get("brand", ""),
            "mpn": D.get("mpn", ""),
            "ean": D.get("ean", ""),
            "category_path": D.get("category_path", ""),
            "availability": D.get("availability", ""),
            "item_url": L.get("item_url", ""),
            "main_image_url": images[0] if images else "",
            "image_count": len(images),
            "image_urls": " | ".join(images)[:CELL_LIMIT],
            "item_specifics": json.dumps(D.get("specs", {}), ensure_ascii=False)[:CELL_LIMIT] if ok else "",
            "specs": D.get("specs", {}) if ok else {},
            "detail_status": ("partial" if D.get("status") == "ok" and not ok else D.get("status", "not_fetched")),
            "first_seen": L.get("first_seen", ""),
            "details_fetched_at": D.get("fetched_at", ""),
        })
    rank = {"ok": 0, "ended": 1, "partial": 2, "blocked": 3, "error": 3, "not_fetched": 4}
    rows.sort(key=lambda r: r["item_id"], reverse=True)
    rows.sort(key=lambda r: rank.get(r["detail_status"], 5))
    return rows


def _excel_safe(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


LEADING_COLUMNS = ["store", "item_id", "title", "price", "price_max", "currency", "item_url",
                   "main_image_url", "image_count", "image_urls"]
TRAILING_COLUMNS = ["category_path", "availability", "item_specifics", "detail_status",
                    "first_seen", "details_fetched_at"]
SPEC_MIN_ITEMS = 20
SPEC_MIN_SHARE = 0.05
CONDITION_KEYS = {"Artikelzustand", "Zustand", "Condition", "Item condition"}


def spec_key(k: str) -> str:
    return re.sub(r"\s+", " ", k).strip().casefold()


def spec_columns(rows: list[dict], min_items: int = SPEC_MIN_ITEMS, min_share: float = SPEC_MIN_SHARE) -> list[str]:
    with_specs = [r for r in rows if r.get("specs")]
    if not with_specs:
        return []
    counts: dict[str, int] = {}
    spellings: dict[str, dict[str, int]] = {}
    for r in with_specs:
        for k in {spec_key(k): k for k in r["specs"]}.values():
            key = spec_key(k)
            counts[key] = counts.get(key, 0) + 1
            spellings.setdefault(key, {})[k] = spellings.setdefault(key, {}).get(k, 0) + 1
    reserved = {c.casefold() for c in LEADING_COLUMNS + TRAILING_COLUMNS}
    needed = max(min_items, int(len(with_specs) * min_share))
    keep = [max(spellings[k], key=spellings[k].get) for k, n in counts.items() if n >= needed and k not in reserved]
    condition = {spec_key(k) for k in CONDITION_KEYS}
    return sorted(keep, key=lambda k: (spec_key(k) not in condition, -counts[spec_key(k)], k.casefold()))


def flatten(row: dict, spec_cols: list[str]) -> dict:
    out = {c: row.get(c) for c in LEADING_COLUMNS}
    specs = {}
    for k, v in (row.get("specs") or {}).items():
        specs.setdefault(spec_key(k), v)
    condition = {spec_key(k) for k in CONDITION_KEYS}
    for col in spec_cols:
        v = specs.get(spec_key(col), "")
        out[col] = condition_from_specs({"Artikelzustand": v}) if spec_key(col) in condition and v else v
    for c in TRAILING_COLUMNS:
        out[c] = row.get(c)
    return out


def export(out_dir: Path, slugs: list[str]) -> dict[str, int]:
    exp = out_dir / "output"
    exp.mkdir(parents=True, exist_ok=True)
    everything, counts, per_store = [], {}, {}
    for slug in slugs:
        rows = build_rows(out_dir, slug)
        counts[slug] = len(rows)
        everything.extend(rows)
        cols = spec_columns(rows)
        per_store[slug] = (LEADING_COLUMNS + cols + TRAILING_COLUMNS, [flatten(r, cols) for r in rows])
        _write_csv(exp / f"{slug}.csv", *per_store[slug])
    all_cols = spec_columns(everything)
    _write_csv(exp / "all_stores.csv", LEADING_COLUMNS + all_cols + TRAILING_COLUMNS,
               [flatten(r, all_cols) for r in everything])
    write_json_atomic(exp / "all_stores.json", [{k: v for k, v in r.items() if k != "item_specifics"} for r in everything])
    summary = {"generated": now_iso(), "counts": counts, "total": len(everything),
               "with_details": sum(1 for r in everything if r["detail_status"] in ("ok", "ended")),
               "ended": sum(1 for r in everything if r["detail_status"] == "ended"),
               "errors": sum(1 for r in everything if r["detail_status"] in ("error", "blocked")),
               "partial": sum(1 for r in everything if r["detail_status"] == "partial"),
               "total_images": sum(r["image_count"] for r in everything),
               "artikelmerkmale_columns": len(all_cols)}
    try:
        _write_xlsx(exp / "all_stores.xlsx", per_store, summary)
        for slug, data in per_store.items():
            _write_xlsx(exp / f"{slug}.xlsx", {slug: data}, None)
    except ImportError:
        log.warning("openpyxl not installed; skipping XLSX")
    write_json_atomic(exp / "summary.json", summary)
    log.info("export: %s", json.dumps(summary))
    return counts


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _excel_safe(r.get(k)) for k in columns})
    os.replace(tmp, path)


def _write_xlsx(path: Path, per_store: dict[str, tuple[list[str], list[dict]]], summary: dict | None) -> None:
    from openpyxl import Workbook
    wb = Workbook(write_only=True)
    for slug, (columns, rows) in per_store.items():
        ws = wb.create_sheet(slug[:31])
        ws.freeze_panes = "C2"
        ws.append(columns)
        for r in rows:
            ws.append([_excel_safe(r.get(c)) for c in columns])
    if summary is None:
        tmp = path.with_suffix(".xlsx.tmp")
        wb.save(tmp)
        os.replace(tmp, path)
        return
    ws = wb.create_sheet("Summary")
    ws.append(["metric", "value"])
    for k, v in summary.items():
        if k == "counts":
            for store, n in v.items():
                ws.append([f"products in {store}", n])
        else:
            ws.append([k, v])
    tmp = path.with_suffix(".xlsx.tmp")
    wb.save(tmp)
    os.replace(tmp, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape every listing and image from eBay stores.")
    p.add_argument("--stores", nargs="+", default=DEFAULT_STORES, help="store URLs or slugs")
    p.add_argument("--phases", nargs="+", default=["listing", "details", "export"],
                   choices=["listing", "details", "export"])
    p.add_argument("--out", default=".", help="base directory for data/ and output/")
    p.add_argument("--profile", default=".browser-profile", help="persistent browser profile dir")
    p.add_argument("--headless", action="store_true", help="no window (CAPTCHAs then abort the run)")
    p.add_argument("--concurrency", type=int, default=2, help="parallel tabs for item pages")
    p.add_argument("--delay", type=float, nargs=2, default=[1.0, 2.5], metavar=("MIN", "MAX"),
                   help="random pause between requests, seconds")
    p.add_argument("--max-pages", type=int, default=1000, help="safety cap per store")
    p.add_argument("--max-items", type=int, default=None, help="limit items per store (testing)")
    p.add_argument("--captcha-timeout", type=int, default=900)
    p.add_argument("--retries", type=int, default=5, help="retries for network errors and 5xx")
    p.add_argument("--block-retries", type=int, default=10, help="403 retries per URL")
    p.add_argument("--block-abort", type=int, default=60, help="abort after this many 403s in a row")
    p.add_argument("--nav-timeout", type=int, default=45)
    p.add_argument("--sort", default=SORT_NEWEST_FIRST, choices=["10", "15", "16", "12"],
                   help="store sort for the listing pass: 10 newest, 15 price asc, 16 price desc, 12 best match; "
                        "extra passes with another sort add any items the first pass missed")
    p.add_argument("--detail-mode", choices=["fetch", "browser"], default="fetch",
                   help="fetch: parallel in-tab HTTP fetches (fast); browser: navigate each item page (slow, most robust)")
    p.add_argument("--parallel-fetches", type=int, default=12, help="simultaneous item fetches in fetch mode")
    p.add_argument("--batch-size", type=int, default=60, help="items per fetch batch (progress is saved per batch)")
    p.add_argument("--load-images", action="store_true", help="let the browser download images (slower)")
    p.add_argument("--restart-listing", action="store_true", help="re-crawl the store grids")
    p.add_argument("--refresh-details", action="store_true", help="re-fetch item pages already done")
    p.add_argument("--until-complete", action="store_true",
                   help="keep running with a fresh browser profile each time until every item is complete")
    p.add_argument("--max-runs", type=int, default=500, help="upper bound on runs for --until-complete")
    p.add_argument("--cooldown", type=int, default=600,
                   help="pause in seconds after a run that fails within 3 minutes (--until-complete)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    if a.concurrency < 1 or a.concurrency > 10:
        p.error("--concurrency must be 1..10")
    if a.delay[0] < 0 or a.delay[1] < a.delay[0]:
        p.error("--delay MIN MAX with 0 <= MIN <= MAX")
    try:
        a.slugs = [store_slug(s) for s in a.stores]
    except ValueError as e:
        p.error(str(e))
    if len(set(a.slugs)) != len(a.slugs):
        p.error("duplicate stores given")
    return a


def setup_logging(out_dir: Path, verbose: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in (logging.StreamHandler(sys.stderr), logging.FileHandler(out_dir / "scraper.log", encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)


async def run(a: argparse.Namespace) -> int:
    out = Path(a.out).resolve()
    cfg = Config(out_dir=out, profile_dir=Path(a.profile).resolve(), headless=a.headless,
                 delay=(a.delay[0], a.delay[1]), concurrency=a.concurrency, max_pages=a.max_pages,
                 max_items=a.max_items, captcha_timeout=a.captcha_timeout, retries=a.retries, block_retries=a.block_retries, block_abort=a.block_abort, load_images=a.load_images,
                 detail_mode=a.detail_mode, sort=a.sort, parallel_fetches=a.parallel_fetches, batch_size=a.batch_size,
                 nav_timeout=a.nav_timeout, refresh_details=a.refresh_details)
    if a.restart_listing:
        for slug in a.slugs:
            sdir = out / "data" / slug
            for f in [*sdir.glob("listing_state*.json"), sdir / "listing.jsonl"]:
                f.unlink(missing_ok=True)

    failures: dict[str, str] = {}
    if {"listing", "details"} & set(a.phases):
        async with Browser(cfg) as br:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, br.stop.set)
                except NotImplementedError:
                    pass
            for store, slug in zip(a.stores, a.slugs):
                if br.stop.is_set():
                    break
                try:
                    if "listing" in a.phases:
                        await scrape_listing(br, store, cfg)
                    if "details" in a.phases:
                        await scrape_details(br, store, cfg)
                except SessionBurnedError as e:
                    failures[slug] = str(e)
                    log.error("[%s] %s", slug, e)
                    break
                except BlockedError as e:
                    failures[slug] = str(e)
                    log.error("[%s] %s", slug, e)
                except asyncio.CancelledError:
                    log.warning("interrupted; progress is saved, re-run the same command to resume")
                    break
    if "export" in a.phases:
        export(out, [s for s in a.slugs if (out / "data" / s / "listing.jsonl").exists()])
    if failures:
        log.error("finished with failures: %s  (re-run to resume)", failures)
        return 2
    return 0


def remaining_items(out_dir: Path, slugs: list[str]) -> int | None:
    left = 0
    for slug in slugs:
        sdir = out_dir / "data" / slug
        if not (sdir / "listing.jsonl").exists():
            return None
        lst = JsonlStore(sdir / "listing.jsonl")
        lst.close()
        det = JsonlStore(sdir / "details.jsonl")
        det.close()
        left += sum(1 for i in lst.records if not is_complete(det.records.get(i)))
    return left


def run_until_complete(a: argparse.Namespace) -> int:
    out = Path(a.out).resolve()
    base = Path(a.profile).resolve()
    attempt = 0
    while True:
        attempt += 1
        a.profile = str(base.with_name(f"{base.name}-{int(time.time())}"))
        started = time.monotonic()
        log.info("run %d with fresh browser profile %s", attempt, Path(a.profile).name)
        code = asyncio.run(run(a))
        shutil.rmtree(a.profile, ignore_errors=True)
        if "listing" in a.phases and all(
                read_json(out / "data" / slug / "listing_state.json", {}).get("complete") for slug in a.slugs):
            a.phases = [ph for ph in a.phases if ph != "listing"] or ["details"]
        left = remaining_items(out, a.slugs)
        log.info("run %d exited with %d; %s items still incomplete", attempt, code,
                 "unknown" if left is None else left)
        if left == 0:
            if "export" not in a.phases:
                export(out, a.slugs)
            return 0
        if attempt >= a.max_runs:
            log.error("stopping after %d runs; re-run to continue", attempt)
            return 2
        pause = a.cooldown if time.monotonic() - started < 180 else 60
        log.info("next run in %ds", pause)
        time.sleep(pause)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    setup_logging(Path(a.out).resolve(), a.verbose)
    try:
        if a.until_complete:
            return run_until_complete(a)
        return asyncio.run(run(a))
    except KeyboardInterrupt:
        log.warning("interrupted; progress is saved, re-run the same command to resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())
