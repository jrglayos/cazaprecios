"""Cazaprecios — servidor web local. Ábrelo con los lanzadores Iniciar.*"""
from __future__ import annotations

import os
import socket
import threading
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

import core

app = Flask(__name__, static_folder="static")
scheduler = core.Scheduler()


def err(msg, code=400):
    return jsonify({"error": msg}), code


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        raise ValueError("El precio objetivo debe ser un número.")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def state():
    since = int(request.args.get("since", 0))
    return jsonify({"products": core.list_products(), "status": scheduler.state,
                    "alerts": core.alerts_since(since),
                    "source": "keepa" if core.using_keepa() else "web"})


@app.post("/api/products")
def add():
    data = request.get_json(force=True)
    try:
        product = core.add_product(data.get("url", ""), _num(data.get("target")))
    except (ValueError, core.ScrapeError) as e:
        return err(str(e))
    return jsonify(product)


@app.patch("/api/products/<asin>")
def update(asin):
    try:
        ok = core.set_target(asin, _num(request.get_json(force=True).get("target")))
    except ValueError as e:
        return err(str(e))
    return jsonify(core.get_product(asin)) if ok else err("No existe", 404)


@app.delete("/api/products/<asin>")
def delete(asin):
    return jsonify({"ok": core.remove_product(asin)})


@app.get("/api/products/<asin>/history")
def hist(asin):
    return jsonify(core.history(asin))


@app.post("/api/check")
def check():
    return jsonify({"started": scheduler.run_now()})


@app.get("/api/settings")
def get_settings():
    return jsonify(core.get_settings())


@app.post("/api/settings")
def set_settings():
    s = core.save_settings(request.get_json(force=True))
    scheduler.reschedule()
    return jsonify(s)


@app.post("/api/settings/test")
def test_telegram():
    try:
        core.send_telegram("✅ Cazaprecios conectado. Aquí recibirás los avisos.",
                           {**core.get_settings(), **(request.get_json(silent=True) or {})})
    except Exception as e:
        return err(str(e))
    return jsonify({"ok": True})


@app.post("/api/settings/test-keepa")
def test_keepa():
    key = (request.get_json(silent=True) or {}).get("keepa_key") or core.get_settings().get("keepa_key")
    if not key:
        return err("Pega primero tu clave de Keepa.")
    try:
        return jsonify(core.keepa.tokens(key.strip()))
    except core.keepa.KeepaError as e:
        return err(str(e))


def free_port(preferred=5173):
    for port in range(preferred, preferred + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return 0


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or free_port())
    url = f"http://127.0.0.1:{port}"
    scheduler.start()
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"\n  Cazaprecios funcionando en {url}\n  Deja esta ventana abierta mientras lo uses.\n")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
