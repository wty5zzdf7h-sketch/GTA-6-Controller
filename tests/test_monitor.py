import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

import monitor as m  # noqa: E402

CONFIG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
PATTERNS = m.compile_patterns(CONFIG["match_patterns"])
MUST = m.compile_patterns([r"grand theft auto|gta ?vi|gta 6"])


def page(availability=None, body=""):
    ld = ""
    if availability:
        ld = ('<script type="application/ld+json">' + json.dumps({
            "@context": "https://schema.org", "@type": "Product",
            "name": "DualSense Grand Theft Auto VI White Limited Edition",
            "offers": {"@type": "Offer", "price": "89.90", "priceCurrency": "CHF",
                       "availability": f"https://schema.org/{availability}"},
        }) + "</script>")
    return f"<html><head><title>DualSense GTA VI</title>{ld}</head><body>{body}</body></html>"


def test_jsonld_in_stock():
    r = m.analyse_product(page("InStock", "Zubehör ausverkauft <button>In den Warenkorb</button>"), 200, MUST)
    assert r.status == m.AVAILABLE
    assert r.price == "89.90 CHF"


def test_visible_sold_out_beats_schema_instock():
    # gameshop.at: schema.org sagt InStock, Seite zeigt „leider ausverkauft“
    r = m.analyse_product(page("InStock", "<img alt='leider ausverkauft'>"), 200, MUST)
    assert r.status == m.UNAVAILABLE


def test_jsonld_out_of_stock():
    assert m.analyse_product(page("OutOfStock"), 200, MUST).status == m.UNAVAILABLE


def test_jsonld_store_only():
    assert m.analyse_product(page("InStoreOnly"), 200, MUST).status == m.STORE_ONLY


def test_preorder_counts_as_orderable():
    assert m.analyse_product(page("PreOrder"), 200, MUST).status == m.AVAILABLE


def test_keyword_fallback():
    assert m.analyse_product(page(body="Leider ausverkauft"), 200, MUST).status == m.UNAVAILABLE
    assert m.analyse_product(page(body="<button>In den Warenkorb</button>"), 200, MUST).status == m.AVAILABLE
    assert m.analyse_product(page(body="nichts"), 200, MUST).status == m.UNKNOWN


def test_blocked_and_errors():
    assert m.analyse_product(page("InStock"), 403, MUST).status == m.BLOCKED
    assert m.analyse_product("<html>Please solve the captcha</html>", 200, MUST).status == m.BLOCKED
    assert m.analyse_product(page("InStock"), 404, MUST).status == m.ERROR


def test_redirect_to_other_product_is_unknown():
    html = page().replace("GTA VI", "Fortnite")
    assert m.analyse_product(html, 200, MUST).status == m.UNKNOWN


def test_search_found_and_query_echo_ignored():
    hit = '<a>Sony DualSense Wireless-Controller Grand Theft Auto VI White Limited Edition</a>'
    assert m.analyse_search(hit, 200, PATTERNS).status == m.FOUND
    echo = '<input value="dualsense grand theft auto"> Keine Ergebnisse für „dualsense grand theft auto“'
    assert m.analyse_search(echo, 200, PATTERNS).status == m.NOT_FOUND


FEED = """<?xml version="1.0"?><rss><channel>
<item><title>GTA VI DualSense wieder verfügbar bei Brack</title><link>https://x/1</link><guid>1</guid></item>
<item><title>Neue PS5 Pro Bundles</title><link>https://x/2</link><guid>2</guid></item>
</channel></rss>"""


class FakeNotifier(m.Notifier):
    def __init__(self):
        super().__init__("https://ntfy.sh", "t", dry_run=True)
        self.sent = []

    def send(self, title, message, priority=3, tags=None, click=None):
        self.sent.append((title, message, priority, click))


def test_feed_first_run_silent_then_notifies_new():
    target = {"id": "f", "name": "Feed", "type": "feed", "url": "u"}
    ts, n = {}, FakeNotifier()
    m.handle_feed(target, ts, FEED, 200, PATTERNS, n)
    assert n.sent == [] and ts["seen"] == ["1"]
    newer = FEED.replace("<guid>1</guid>", "<guid>1</guid>").replace(
        "</channel>", "<item><title>Grand Theft Auto VI Controller Restock</title>"
                      "<link>https://x/3</link><guid>3</guid></item></channel>")
    m.handle_feed(target, ts, newer, 200, PATTERNS, n)
    assert len(n.sent) == 1 and "Restock" in n.sent[0][0] + n.sent[0][1]
    m.handle_feed(target, ts, newer, 200, PATTERNS, n)
    assert len(n.sent) == 1


def test_atom_feed():
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>GTA 6 DualSense drop</title>
    <link href="https://r/1"/><id>t3_1</id></entry></feed>"""
    items = m.parse_feed(atom)
    assert items[0]["link"] == "https://r/1"
    assert m.matching_feed_items(items, PATTERNS)


def test_run_notifies_once_on_restock(monkeypatch):
    responses = {"html": page("OutOfStock")}

    class Resp:
        status_code = 200
        encoding = "utf-8"

        @property
        def text(self):
            return responses["html"]

    monkeypatch.setattr(m, "fetch", lambda url, timeout=25: Resp())
    config = {"settings": {}, "match_patterns": CONFIG["match_patterns"],
              "targets": [{"id": "p", "name": "Shop", "variant": "Weiss", "type": "product",
                           "url": "https://shop/p"}]}
    state, n = {}, FakeNotifier()
    m.run(config, state, n)
    assert n.sent == []
    responses["html"] = page("InStock")
    m.run(config, state, n)
    m.run(config, state, n)
    assert len(n.sent) == 1 and n.sent[0][2] == 5 and n.sent[0][3] == "https://shop/p"
    # Ein blockierter Lauf darf den Status nicht zurücksetzen (kein Doppel-Alarm)
    Resp.status_code = 403
    m.run(config, state, n)
    Resp.status_code = 200
    m.run(config, state, n)
    assert len(n.sent) == 1


def test_failure_warning_after_threshold(monkeypatch):
    class Resp:
        status_code = 403
        encoding = "utf-8"
        text = ""

    monkeypatch.setattr(m, "fetch", lambda url, timeout=25: Resp())
    config = {"settings": {"failure_alert_after": 3}, "match_patterns": [],
              "targets": [{"id": "p", "name": "Shop", "type": "product", "url": "u"}]}
    state, n = {}, FakeNotifier()
    for _ in range(5):
        m.run(config, state, n)
    assert len(n.sent) == 1 and "nicht prüfbar" in n.sent[0][0]
