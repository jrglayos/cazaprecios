"""Lógica del rastreador: lectura de Amazon, base de datos y avisos."""
from __future__ import annotations

import os
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import keepa

DB_PATH = Path(os.environ.get("CAZAPRECIOS_DB", Path(__file__).with_name("cazaprecios.db")))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

PRICE_SELECTORS = [
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    ".a-price .a-offscreen",
]

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", re.I)

DEFAULT_SETTINGS = {"interval_hours": "6", "telegram_token": "", "telegram_chat_id": "", "keepa_key": ""}


class ScrapeError(Exception):
    pass


@dataclass
class Product:
    title: str
    price: float | None
    currency: str
    image: str = ""


# ------------------------------------------------------------------ lectura

def extract_asin(url: str) -> str:
    m = ASIN_RE.search(url or "")
    if not m:
        raise ValueError("Ese enlace no parece de un producto de Amazon (no encuentro el código /dp/...).")
    return m.group(1).upper()


def canonical_url(url: str) -> str:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    host = urlparse(url).netloc or "www.amazon.com"
    if "amazon." not in host and "amzn." not in host:
        raise ValueError("El enlace debe ser de Amazon.")
    return f"https://{host}/dp/{extract_asin(url)}"


def parse_price(text: str) -> tuple[float, str] | None:
    """'1.299,99 €' -> (1299.99, '€');  '$1,299.99' -> (1299.99, '$')."""
    if not text:
        return None
    text = text.replace("\xa0", " ").strip()
    m = re.search(r"\d[\d.,\s]*", text)
    if not m:
        return None
    num = re.sub(r"\s", "", m.group(0)).rstrip(".,")
    currency = re.sub(r"[\d.,\s]", "", text).strip()
    last_sep = max(num.rfind(","), num.rfind("."))
    if last_sep != -1 and len(num) - last_sep - 1 in (1, 2):
        integer = re.sub(r"[.,]", "", num[:last_sep])
        value = float(f"{integer or 0}.{num[last_sep + 1:]}")
    else:
        value = float(re.sub(r"[.,]", "", num))
    return value, currency


def _image(soup: BeautifulSoup) -> str:
    for sel in ("#landingImage", "#imgBlkFront", "#main-image"):
        el = soup.select_one(sel)
        if el:
            src = el.get("data-old-hires") or el.get("src") or ""
            if src.startswith("http"):
                return src
    og = soup.select_one("meta[property='og:image']")
    return og.get("content", "") if og else ""


def parse_product_html(html: str) -> Product:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("form[action*='validateCaptcha']") or "api-services-support@amazon.com" in html:
        raise ScrapeError("Amazon ha pedido un CAPTCHA (bloqueo anti-bots). Se reintentará más tarde.")

    title_el = soup.select_one("#productTitle") or soup.select_one("title")
    title = title_el.get_text(strip=True) if title_el else "(sin título)"
    image = _image(soup)

    for sel in PRICE_SELECTORS:
        el = soup.select_one(sel)
        if el:
            parsed = parse_price(el.get_text())
            if parsed and parsed[0] > 0:
                return Product(title, parsed[0], parsed[1], image)

    whole = soup.select_one(".a-price-whole")
    if whole:
        frac = soup.select_one(".a-price-fraction")
        sym = soup.select_one(".a-price-symbol")
        w = re.sub(r"\D", "", whole.get_text())
        f = re.sub(r"\D", "", frac.get_text()) if frac else ""
        if w:
            return Product(title, float(f"{w}.{f or '00'}"), sym.get_text(strip=True) if sym else "", image)

    return Product(title, None, "", image)  # agotado / sin vendedor


def fetch_product(url: str, retries: int = 3) -> Product:
    session = requests.Session()
    last_err: Exception | None = None
    for attempt in range(retries):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            r = session.get(url, headers=headers, timeout=20)
            if r.status_code == 404:
                raise ScrapeError("Amazon dice que ese producto no existe (404).")
            r.raise_for_status()
            return parse_product_html(r.text)
        except requests.RequestException as e:
            last_err = ScrapeError(f"No se pudo conectar con Amazon: {e.__class__.__name__}")
        except ScrapeError as e:
            last_err = e
            if "404" in str(e):
                break
        if attempt < retries - 1:
            time.sleep(2 ** attempt + random.random() * 2)
    raise last_err or ScrapeError("Error desconocido")


def using_keepa() -> bool:
    return bool(get_settings().get("keepa_key"))


def fetch(url: str, with_history: bool = False) -> tuple[Product, list]:
    """Lee el producto con Keepa si hay clave configurada; si no, de la web de Amazon."""
    key = get_settings().get("keepa_key")
    if not key:
        return fetch_product(url), []
    try:
        k = keepa.fetch(url, extract_asin(url), key, history=with_history)
    except keepa.KeepaError as e:
        raise ScrapeError(str(e))
    return Product(k["title"], k["price"], k["currency"], k["image"]), k["history"]


# ------------------------------------------------------------------ base de datos

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            asin TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, image TEXT,
            target_price REAL, currency TEXT, added_at TEXT NOT NULL, last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL REFERENCES products(asin) ON DELETE CASCADE,
            price REAL, checked_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prices_asin ON prices(asin, checked_at);
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asin TEXT, kind TEXT,
            title TEXT, message TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return conn


def get_settings() -> dict:
    with db() as conn:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
    return {**DEFAULT_SETTINGS, **rows}


def save_settings(values: dict) -> dict:
    with db() as conn:
        for k, v in values.items():
            if k in DEFAULT_SETTINGS:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v).strip()))
    return get_settings()


# ------------------------------------------------------------------ avisos

def send_telegram(text: str, settings: dict | None = None) -> None:
    s = settings or get_settings()
    token, chat = s.get("telegram_token"), s.get("telegram_chat_id")
    if not (token and chat):
        raise ValueError("Falta configurar el token o el chat de Telegram.")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=10)
    if not r.ok:
        raise ValueError(f"Telegram respondió: {r.json().get('description', r.status_code)}")


def record_alert(conn, asin: str, kind: str, title: str, message: str) -> None:
    conn.execute("INSERT INTO alerts (asin, kind, title, message, created_at) VALUES (?, ?, ?, ?, ?)",
                 (asin, kind, title, message, now()))
    try:
        send_telegram(f"{title}\n{message}")
    except Exception:
        pass  # Telegram es opcional


def fmt(price, cur) -> str:
    if price is None:
        return "—"
    s = f"{price:,.2f}"
    if cur in ("€", "EUR"):
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{s} €"
    return f"{cur}{s}"


# ------------------------------------------------------------------ operaciones

def add_product(url: str, target: float | None) -> dict:
    url = canonical_url(url)
    asin = extract_asin(url)
    p, past = fetch(url, with_history=True)
    with db() as conn:
        is_new = conn.execute("SELECT 1 FROM prices WHERE asin = ? LIMIT 1", (asin,)).fetchone() is None
        conn.execute(
            """INSERT INTO products (asin, url, title, image, target_price, currency, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(asin) DO UPDATE SET target_price = excluded.target_price,
                   title = excluded.title, image = excluded.image""",
            (asin, url, p.title, p.image, target, p.currency, now()),
        )
        if is_new:  # historial que ya conoce Keepa
            conn.executemany("INSERT INTO prices (asin, price, checked_at) VALUES (?, ?, ?)",
                             [(asin, v, t.isoformat(timespec="seconds")) for t, v in past])
        conn.execute("INSERT INTO prices (asin, price, checked_at) VALUES (?, ?, ?)", (asin, p.price, now()))
    return get_product(asin)


def check_product(conn, row) -> None:
    asin = row["asin"]
    prev = conn.execute("SELECT price FROM prices WHERE asin = ? AND price IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1", (asin,)).fetchone()
    lowest = conn.execute("SELECT MIN(price) m FROM prices WHERE asin = ?", (asin,)).fetchone()["m"]
    try:
        p, _ = fetch(row["url"])
    except ScrapeError as e:
        conn.execute("UPDATE products SET last_error = ? WHERE asin = ?", (str(e), asin))
        return
    conn.execute("INSERT INTO prices (asin, price, checked_at) VALUES (?, ?, ?)", (asin, p.price, now()))
    conn.execute("UPDATE products SET last_error = NULL, title = ?, image = COALESCE(NULLIF(?, ''), image), "
                  "currency = COALESCE(NULLIF(?, ''), currency) WHERE asin = ?",
                  (p.title, p.image, p.currency, asin))
    if p.price is None:
        return
    cur = p.currency or row["currency"]
    prev_price = prev["price"] if prev else None
    target = row["target_price"]
    name = (p.title or row["title"] or asin)[:90]
    if target is not None and p.price <= target and (prev_price is None or prev_price > target):
        record_alert(conn, asin, "target", "🎯 ¡Precio objetivo alcanzado!",
                     f"{name}\nAhora {fmt(p.price, cur)} (objetivo {fmt(target, cur)})\n{row['url']}")
    elif lowest is not None and p.price < lowest:
        record_alert(conn, asin, "low", "📉 Nuevo mínimo histórico",
                     f"{name}\nAhora {fmt(p.price, cur)} (antes {fmt(lowest, cur)})\n{row['url']}")


def check_all(progress=None) -> None:
    pause = not using_keepa()  # con Keepa no hace falta esperar entre productos
    with db() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY added_at").fetchall()
        for i, row in enumerate(rows):
            if progress:
                progress(i, len(rows), row["asin"])
            check_product(conn, row)
            conn.commit()
            if pause and i < len(rows) - 1:
                time.sleep(random.uniform(3, 8))  # ser amable con Amazon


def _product_dict(conn, r) -> dict:
    hist = conn.execute("SELECT price, checked_at FROM prices WHERE asin = ? ORDER BY id", (r["asin"],)).fetchall()
    series = [h["price"] for h in hist if h["price"] is not None]
    last_row = hist[-1] if hist else None
    current = last_row["price"] if last_row else None
    prev = series[-2] if len(series) >= 2 else None
    return {
        "asin": r["asin"], "url": r["url"], "title": r["title"], "image": r["image"],
        "currency": r["currency"] or "", "target": r["target_price"], "added_at": r["added_at"],
        "error": r["last_error"], "current": current, "previous": prev,
        "low": min(series) if series else None, "high": max(series) if series else None,
        "spark": series[-40:], "checked_at": last_row["checked_at"] if last_row else None,
    }


def list_products() -> list[dict]:
    with db() as conn:
        return [_product_dict(conn, r) for r in conn.execute("SELECT * FROM products ORDER BY added_at DESC")]


def get_product(asin: str) -> dict | None:
    with db() as conn:
        r = conn.execute("SELECT * FROM products WHERE asin = ?", (asin.upper(),)).fetchone()
        return _product_dict(conn, r) if r else None


def history(asin: str) -> list[dict]:
    with db() as conn:
        return [dict(h) for h in conn.execute(
            "SELECT price, checked_at FROM prices WHERE asin = ? ORDER BY id", (asin.upper(),))]


def set_target(asin: str, target: float | None) -> bool:
    with db() as conn:
        return conn.execute("UPDATE products SET target_price = ? WHERE asin = ?",
                            (target, asin.upper())).rowcount > 0


def remove_product(asin: str) -> bool:
    with db() as conn:
        return conn.execute("DELETE FROM products WHERE asin = ?", (asin.upper(),)).rowcount > 0


def alerts_since(since_id: int = 0, limit: int = 30) -> list[dict]:
    with db() as conn:
        return [dict(a) for a in conn.execute(
            "SELECT * FROM alerts WHERE id > ? ORDER BY id DESC LIMIT ?", (since_id, limit))]


# ------------------------------------------------------------------ programador

class Scheduler:
    """Comprueba los precios en segundo plano cada N horas."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"checking": False, "done": 0, "total": 0, "last_check": None, "next_check": None}
        self._wake = threading.Event()

    def run_now(self) -> bool:
        if self.state["checking"]:
            return False
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.state.update(checking=True, done=0, total=0)

            def progress(i, total, _asin):
                self.state.update(done=i, total=total)

            check_all(progress)
        finally:
            self.state.update(checking=False, last_check=now())
            self.lock.release()
            self._wake.set()

    def loop(self):
        while True:
            hours = float(get_settings().get("interval_hours") or 6)
            nxt = datetime.now() + timedelta(hours=hours)
            self.state["next_check"] = nxt.isoformat(timespec="seconds")
            self._wake.clear()
            if self._wake.wait(timeout=hours * 3600):
                continue  # se reinicia la cuenta tras una comprobación manual o cambio de ajustes
            self._run()

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def reschedule(self):
        self._wake.set()
