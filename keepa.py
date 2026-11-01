"""Precios de Amazon a través de la API de Keepa (https://keepa.com/#!api)."""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

API = "https://api.keepa.com"

# Dominio de Amazon -> id de Keepa y moneda
DOMAINS = {
    "amazon.com": (1, "$"), "amazon.co.uk": (2, "£"), "amazon.de": (3, "€"), "amazon.fr": (4, "€"),
    "amazon.co.jp": (5, "¥"), "amazon.ca": (6, "$"), "amazon.it": (8, "€"), "amazon.es": (9, "€"),
    "amazon.in": (10, "₹"), "amazon.com.mx": (11, "$"), "amazon.com.br": (12, "R$"),
}

AMAZON, NEW = 0, 1  # índices de las series de precio de Keepa
HISTORY_DAYS = 365


class KeepaError(Exception):
    pass


def domain_for(url: str) -> tuple[int, str]:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host not in DOMAINS:
        raise KeepaError(f"Keepa no admite la tienda {host}.")
    return DOMAINS[host]


def keepa_time(minutes: int) -> datetime:
    """Keepa cuenta minutos desde el 1 de enero de 2011 (UTC)."""
    return datetime.fromtimestamp((minutes + 21564000) * 60)


def _call(path: str, params: dict) -> dict:
    try:
        r = requests.get(f"{API}/{path}", params=params, timeout=30)
    except requests.RequestException as e:
        raise KeepaError(f"No se pudo conectar con Keepa ({e.__class__.__name__}).")
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code in (401, 403) or (data.get("error") or {}).get("type") == "invalidKey":
        raise KeepaError("La clave de Keepa no es válida. Revísala en Ajustes.")
    if r.status_code == 402:
        raise KeepaError("Tu cuenta de Keepa no tiene una suscripción a la API activa.")
    if r.status_code == 429:
        raise KeepaError("Keepa: se han agotado los tokens por ahora. Se reintentará en la próxima ronda.")
    if not r.ok or data.get("error"):
        msg = (data.get("error") or {}).get("message") or f"error {r.status_code}"
        raise KeepaError(f"Keepa respondió: {msg}")
    return data


def tokens(key: str) -> dict:
    data = _call("token", {"key": key})
    return {"tokens_left": data.get("tokensLeft"), "refill_rate": data.get("refillRate")}


def _image(p: dict) -> str:
    if p.get("imagesCSV"):
        return "https://m.media-amazon.com/images/I/" + p["imagesCSV"].split(",")[0]
    imgs = p.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        name = imgs[0].get("l") or imgs[0].get("m")
        if name:
            return "https://m.media-amazon.com/images/I/" + name
    return ""


def _series(csv: list | None, idx: int) -> list[tuple[datetime, float]]:
    if not csv or idx >= len(csv) or not csv[idx]:
        return []
    raw, cutoff, out = csv[idx], datetime.now() - timedelta(days=HISTORY_DAYS), []
    for i in range(0, len(raw) - 1, 2):
        t, v = keepa_time(raw[i]), raw[i + 1]
        if t >= cutoff and v is not None and v > 0:
            out.append((t, v / 100))
    return out


def parse_product(p: dict, currency: str) -> dict:
    """Devuelve {title, image, currency, price, history:[(fecha, precio)]}."""
    current = ((p.get("stats") or {}).get("current")) or []
    price, source = None, AMAZON
    for idx in (AMAZON, NEW):  # primero vendido por Amazon; si no, el nuevo más barato
        if idx < len(current) and current[idx] is not None and current[idx] > 0:
            price, source = current[idx] / 100, idx
            break
    return {
        "title": p.get("title") or "",
        "image": _image(p),
        "currency": currency,
        "price": price,
        "history": _series(p.get("csv"), source),
    }


def fetch(url: str, asin: str, key: str, history: bool = False) -> dict:
    domain, currency = domain_for(url)
    data = _call("product", {"key": key, "domain": domain, "asin": asin,
                             "stats": 1, "history": int(history)})
    products = data.get("products") or []
    if not products or not products[0].get("title"):
        raise KeepaError("Keepa no conoce ese producto (¿enlace correcto?).")
    return parse_product(products[0], currency)
