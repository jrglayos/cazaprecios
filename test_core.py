import pytest
import core
import app as server


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(core.time, "sleep", lambda s: None)


@pytest.mark.parametrize("text,expected", [
    ("1.299,99 €", 1299.99), ("$1,299.99", 1299.99), ("49,90\xa0€", 49.90),
    ("£12.5", 12.5), ("€ 1.299", 1299.0), ("$15", 15.0),
])
def test_parse_price(text, expected):
    assert core.parse_price(text)[0] == pytest.approx(expected)


def test_urls():
    assert core.extract_asin("https://www.amazon.es/Algo/dp/B0ABCDEF12/ref=sr_1_1?x=1") == "B0ABCDEF12"
    assert core.canonical_url("www.amazon.com/gp/product/B0ABCDEF12?th=1") == "https://www.amazon.com/dp/B0ABCDEF12"
    with pytest.raises(ValueError):
        core.extract_asin("https://www.amazon.es/s?k=raton")
    with pytest.raises(ValueError):
        core.canonical_url("https://www.ebay.es/dp/B0ABCDEF12")


def test_parse_html():
    html = ('<span id="productTitle"> Ratón </span><img id="landingImage" data-old-hires="https://m.media-amazon.com/x.jpg">'
            '<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">24,99 €</span></span></div>')
    p = core.parse_product_html(html)
    assert (p.title, p.price, p.currency, p.image) == ("Ratón", 24.99, "€", "https://m.media-amazon.com/x.jpg")
    split = '<span class="a-price-symbol">$</span><span class="a-price-whole">1,024.</span><span class="a-price-fraction">50</span>'
    assert core.parse_product_html(split).price == 1024.50
    with pytest.raises(core.ScrapeError):
        core.parse_product_html('<form action="/errors/validateCaptcha"></form>')


def test_api_flow(monkeypatch):
    prices = iter([30.0, 25.0, 19.0])
    monkeypatch.setattr(core, "fetch_product", lambda url: core.Product("Cosa", next(prices), "€", ""))
    c = server.app.test_client()

    r = c.post("/api/products", json={"url": "https://www.amazon.es/dp/B0ABCDEF12", "target": "20,5"})
    assert r.status_code == 200 and r.json["target"] == 20.5
    assert c.post("/api/products", json={"url": "hola"}).status_code == 400

    core.check_all(); core.check_all()
    s = c.get("/api/state").json
    p = s["products"][0]
    assert (p["current"], p["previous"], p["low"], p["high"]) == (19.0, 25.0, 19.0, 30.0)
    assert [a["kind"] for a in s["alerts"]] == ["target", "low"]  # más reciente primero

    assert c.patch("/api/products/B0ABCDEF12", json={"target": ""}).json["target"] is None
    assert len(c.get("/api/products/B0ABCDEF12/history").json) == 3
    assert c.post("/api/settings", json={"interval_hours": "12"}).json["interval_hours"] == "12"
    assert c.delete("/api/products/B0ABCDEF12").json["ok"]
    assert c.get("/api/state").json["products"] == []
