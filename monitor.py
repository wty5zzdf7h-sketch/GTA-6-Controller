#!/usr/bin/env python3
"""Restock-Wächter für den DualSense „Grand Theft Auto VI" Limited Edition.

Prüft Shop-Produktseiten, Shop-Suchseiten und RSS-Feeds (siehe config.yaml)
und schickt bei Änderungen eine Push-Nachricht über ntfy.

Aufruf:
    python monitor.py               # normaler Lauf
    python monitor.py --test        # nur eine Testnachricht senden
    python monitor.py --dry-run     # prüfen, aber nichts senden/speichern
    python monitor.py --config config-local.yaml   # andere Konfiguration (z. B. Notebook)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = Path(os.environ.get("STATE_FILE", ROOT / "state" / "state.json"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,fr-CH;q=0.8,en;q=0.7",
}

# Status-Werte
AVAILABLE = "lieferbar"
STORE_ONLY = "nur_im_laden"
UNAVAILABLE = "nicht_lieferbar"
FOUND = "gefunden"
NOT_FOUND = "nicht_gefunden"
UNKNOWN = "unbekannt"
BLOCKED = "blockiert"
ERROR = "fehler"

FAILED_STATES = {UNKNOWN, BLOCKED, ERROR}

# schema.org availability -> Status
SCHEMA_AVAILABILITY = {
    "instock": AVAILABLE,
    "limitedavailability": AVAILABLE,
    "onlineonly": AVAILABLE,
    "preorder": AVAILABLE,
    "presale": AVAILABLE,
    "backorder": AVAILABLE,
    "instoreonly": STORE_ONLY,
    "outofstock": UNAVAILABLE,
    "soldout": UNAVAILABLE,
    "discontinued": UNAVAILABLE,
}

OUT_OF_STOCK_WORDS = [
    "ausverkauft", "nicht verfügbar", "nicht lieferbar", "derzeit nicht",
    "momentan nicht", "zurzeit nicht", "nicht mehr erhältlich", "épuisé",
    "epuise", "non disponible", "indisponible", "pas de livraison possible",
    "sold out", "out of stock", "currently unavailable", "esaurito",
    "non disponibile",
]
IN_STOCK_WORDS = [
    "in den warenkorb", "ajouter au panier", "add to cart", "add to basket",
    "sofort lieferbar", "an lager", "auf lager", "en stock", "jetzt kaufen",
    "vorbestellen", "précommander", "pre-order", "aggiungi al carrello",
]
BLOCK_WORDS = [
    "captcha", "access denied", "pardon our interruption", "cf-challenge",
    "attention required", "are you a robot", "bist du ein mensch",
    "request unsuccessful", "px-captcha", "zugriff verweigert",
]


@dataclass
class Result:
    status: str
    detail: str = ""
    price: str | None = None


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\\u002F", "/").replace("\\/", "/")
    return re.sub(r"\s+", " ", text).lower()


def compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns]


def fetch(url: str, timeout: int = 25) -> requests.Response:
    return requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)


def looks_blocked(resp_status: int, body_lower: str) -> str | None:
    if resp_status in (401, 403, 429, 503):
        return f"HTTP {resp_status}"
    head = body_lower[:20000]
    for word in BLOCK_WORDS:
        if word in head:
            return f"Bot-Schutz ('{word}')"
    return None


def _iter_jsonld(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_jsonld(item)
    elif isinstance(obj, dict):
        yield obj
        for key in ("@graph", "offers", "itemListElement", "item", "mainEntity"):
            if key in obj:
                yield from _iter_jsonld(obj[key])


def jsonld_availability(raw_html: str) -> tuple[str | None, str | None]:
    """Liefert (status, preis) aus schema.org JSON-LD, falls vorhanden."""
    statuses: list[str] = []
    price = None
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        raw_html, re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        try:
            data = json.loads(html.unescape(block.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _iter_jsonld(data):
            avail = node.get("availability")
            if isinstance(avail, str):
                key = avail.rstrip("/").split("/")[-1].lower()
                if key in SCHEMA_AVAILABILITY:
                    statuses.append(SCHEMA_AVAILABILITY[key])
                    if price is None and node.get("price") is not None:
                        cur = node.get("priceCurrency", "")
                        price = f"{node['price']} {cur}".strip()
    if not statuses:
        # Fallback: availability ausserhalb von JSON-LD (z. B. Next.js-Daten)
        m = re.search(r'schema\.org/(InStock|OutOfStock|PreOrder|SoldOut|'
                      r'InStoreOnly|LimitedAvailability|BackOrder)', raw_html)
        if m:
            statuses.append(SCHEMA_AVAILABILITY[m.group(1).lower()])
    for preferred in (AVAILABLE, STORE_ONLY, UNAVAILABLE):
        if preferred in statuses:
            return preferred, price
    return None, price


# --------------------------------------------------------------------------- #
# Prüfungen
# --------------------------------------------------------------------------- #
def analyse_product(raw_html: str, status_code: int, must_contain: list[re.Pattern],
                    sold_out: list[re.Pattern] | None = None,
                    in_stock: list[re.Pattern] | None = None,
                    ignore_schema: bool = False) -> Result:
    """sold_out/in_stock: shop-eigene Muster (auf dem rohen HTML), die Vorrang haben.
    Sind welche angegeben und keines passt, ist das Ergebnis „unbekannt“ (Seite umgebaut?)."""
    body = normalize(raw_html)
    blocked = looks_blocked(status_code, body)
    if blocked:
        return Result(BLOCKED, blocked)
    if status_code == 404:
        return Result(ERROR, "Seite nicht gefunden (404)")
    if status_code >= 400:
        return Result(ERROR, f"HTTP {status_code}")
    if must_contain and not any(p.search(body) for p in must_contain):
        return Result(UNKNOWN, "Produktname nicht auf der Seite (Weiterleitung?)")

    status, price = jsonld_availability(raw_html)
    if sold_out or in_stock:
        if sold_out and any(p.search(raw_html) for p in sold_out):
            return Result(UNAVAILABLE, "Shop-Muster", price)
        if in_stock and any(p.search(raw_html) for p in in_stock):
            return Result(AVAILABLE, "Shop-Muster", price)
        return Result(UNKNOWN, "Shop-Muster passt nicht (Seite umgebaut?)")
    if ignore_schema:
        status = None
    has_out = any(w in body for w in OUT_OF_STOCK_WORDS)
    has_in = any(w in body for w in IN_STOCK_WORDS)
    if status == AVAILABLE and has_out and not has_in:
        # Versteckte Daten sagen „InStock“, sichtbarer Text sagt „ausverkauft“ -> Text gewinnt
        return Result(UNAVAILABLE, "Text widerspricht schema.org", price)
    if status:
        return Result(status, "schema.org", price)

    if has_out:
        return Result(UNAVAILABLE, "Stichwort")
    if has_in:
        return Result(AVAILABLE, "Stichwort")
    return Result(UNKNOWN, "Verfügbarkeit nicht erkennbar")


def analyse_search(raw_html: str, status_code: int, patterns: list[re.Pattern]) -> Result:
    body = normalize(raw_html)
    blocked = looks_blocked(status_code, body)
    if blocked:
        return Result(BLOCKED, blocked)
    if status_code >= 400:
        return Result(ERROR, f"HTTP {status_code}")
    for p in patterns:
        m = p.search(body)
        if m:
            return Result(FOUND, m.group(0)[:120])
    return Result(NOT_FOUND)


def parse_feed(raw_xml: str) -> list[dict]:
    """Einfacher RSS/Atom-Parser -> [{id, title, link, summary}]."""
    root = ET.fromstring(raw_xml)
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in el:
            ctag = child.tag.split("}")[-1]
            if ctag == "link":
                fields.setdefault("link", child.get("href") or (child.text or ""))
            elif ctag in ("title", "guid", "id", "description", "summary", "content"):
                fields.setdefault(ctag, child.text or "")
        link = fields.get("link", "").strip()
        items.append({
            "id": (fields.get("guid") or fields.get("id") or link or fields.get("title", "")).strip(),
            "title": html.unescape(fields.get("title", "")).strip(),
            "link": link,
            "summary": fields.get("description") or fields.get("summary") or fields.get("content") or "",
        })
    return items


def matching_feed_items(items: list[dict], patterns: list[re.Pattern]) -> list[dict]:
    hits = []
    for item in items:
        text = normalize(f"{item['title']} {re.sub(r'<[^>]+>', ' ', item['summary'])}")
        if any(p.search(text) for p in patterns):
            hits.append(item)
    return hits


# --------------------------------------------------------------------------- #
# Benachrichtigung
# --------------------------------------------------------------------------- #
class Notifier:
    def __init__(self, server: str, topic: str | None, dry_run: bool = False):
        self.server = server.rstrip("/")
        self.topic = topic
        self.dry_run = dry_run

    def send(self, title: str, message: str, priority: int = 3,
             tags: list[str] | None = None, click: str | None = None) -> None:
        payload = {"topic": self.topic, "title": title, "message": message,
                   "priority": priority, "tags": tags or []}
        if click:
            payload["click"] = click
        print(f"[ntfy p{priority}] {title}: {message}")
        if self.dry_run:
            return
        if not self.topic:
            print("  (kein NTFY_TOPIC gesetzt – nicht gesendet)")
            return
        for attempt in range(3):
            try:
                r = requests.post(self.server, json=payload, timeout=20)
                if r.ok:
                    return
                print(f"  ntfy antwortete HTTP {r.status_code}: {r.text[:200]}")
            except requests.RequestException as exc:
                print(f"  ntfy-Fehler: {exc}")
            time.sleep(2 * (attempt + 1))


# --------------------------------------------------------------------------- #
# Hauptablauf
# --------------------------------------------------------------------------- #
def load_state(path: Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def state_path_for(config: dict) -> Path:
    """STATE_FILE (Umgebung) > settings.state_file (relativ zum Repo) > state/state.json."""
    if os.environ.get("STATE_FILE"):
        return Path(os.environ["STATE_FILE"])
    custom = config.get("settings", {}).get("state_file")
    return (ROOT / custom) if custom else STATE_PATH


def label(target: dict) -> str:
    variant = target.get("variant")
    return f"{target['name']} ({variant})" if variant else target["name"]


def run(config: dict, state: dict, notifier: Notifier) -> dict:
    settings = config.get("settings", {})
    default_patterns = compile_patterns(config.get("match_patterns", []))
    fail_threshold = int(settings.get("failure_alert_after", 18))
    targets_state: dict = state.setdefault("targets", {})
    newly_broken: list[str] = []

    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        tid = target["id"]
        kind = target["type"]
        url = target["url"]
        link = target.get("link", url)
        ts = targets_state.setdefault(tid, {})
        patterns = compile_patterns(target["patterns"]) if "patterns" in target else default_patterns

        try:
            resp = fetch(url, int(target.get("timeout", 25)))
            resp.encoding = resp.encoding or "utf-8"
            text = resp.text
        except requests.RequestException as exc:
            result = Result(ERROR, f"{type(exc).__name__}")
            text, resp = "", None

        if resp is not None:
            if kind == "product":
                must = compile_patterns(target.get("must_contain", [r"grand theft auto|gta ?vi|gta 6"]))
                result = analyse_product(
                    text, resp.status_code, must,
                    sold_out=compile_patterns(target.get("sold_out_patterns", [])),
                    in_stock=compile_patterns(target.get("in_stock_patterns", [])),
                    ignore_schema=bool(target.get("ignore_schema", False)),
                )
            elif kind == "search":
                result = analyse_search(text, resp.status_code, patterns)
            elif kind == "feed":
                result = handle_feed(target, ts, text, resp.status_code, patterns, notifier)
            else:
                result = Result(ERROR, f"unbekannter Typ {kind}")

        prev = ts.get("status")
        print(f"- {label(target):45s} {result.status:15s} {result.detail}")

        # Statuswechsel melden
        if kind == "product" and result.status in (AVAILABLE, STORE_ONLY) and prev != result.status:
            if result.status == AVAILABLE:
                title = f"🎮 BESTELLBAR: {label(target)}"
                prio, tags = 5, ["rotating_light", "video_game"]
            else:
                title = f"Nur im Laden: {label(target)}"
                prio, tags = 4, ["shopping_cart"]
            msg = "GTA VI DualSense"
            if result.price:
                msg += f" – {result.price}"
            msg += f"\n{link}"
            notifier.send(title, msg, prio, tags, click=link)
        elif kind == "search" and result.status == FOUND and prev != FOUND:
            notifier.send(f"🔎 Aufgetaucht: {label(target)}",
                          f"Der GTA-VI-DualSense erscheint in der Shop-Suche. Bitte prüfen!\n{link}",
                          4, ["mag"], click=link)

        # Zustand aktualisieren (Fehler überschreiben den letzten guten Status nicht)
        if result.status in FAILED_STATES:
            ts["failures"] = ts.get("failures", 0) + 1
            ts["last_error"] = f"{result.status}: {result.detail}"
            if ts["failures"] == fail_threshold:
                newly_broken.append(f"{label(target)} – {ts['last_error']}")
        else:
            if ts.get("failures", 0) >= fail_threshold:
                print(f"  wieder erreichbar: {label(target)}")
            ts["failures"] = 0
            ts["status"] = result.status
            ts.pop("last_error", None)

    if newly_broken:
        notifier.send("⚠️ Wächter: Quellen nicht prüfbar",
                      "Diese Quellen liefern seit einer Weile kein Ergebnis:\n- "
                      + "\n- ".join(newly_broken), 2, ["warning"])

    maybe_daily_summary(config, state, notifier)
    return state


def handle_feed(target: dict, ts: dict, text: str, status_code: int,
                patterns: list[re.Pattern], notifier: Notifier) -> Result:
    blocked = looks_blocked(status_code, normalize(text[:5000]))
    if blocked:
        return Result(BLOCKED, blocked)
    if status_code >= 400:
        return Result(ERROR, f"HTTP {status_code}")
    try:
        items = parse_feed(text)
    except ET.ParseError:
        return Result(ERROR, "Feed nicht lesbar")

    seen_list = ts.get("seen", [])
    seen = set(seen_list)
    first_run = "seen" not in ts
    hits = matching_feed_items(items, patterns)
    new_hits = [h for h in hits if h["id"] not in seen]
    if not first_run:
        for h in new_hits[:5]:
            notifier.send(f"📰 {label(target)}", f"{h['title']}\n{h['link']}", 3,
                          ["newspaper"], click=h["link"] or None)
    # Nur die letzten 300 IDs merken
    ts["seen"] = (seen_list + [h["id"] for h in new_hits])[-300:]
    return Result("ok", f"{len(items)} Einträge, {len(new_hits)} neue Treffer"
                  + (" (Erstlauf, still)" if first_run and new_hits else ""))


def maybe_daily_summary(config: dict, state: dict, notifier: Notifier) -> None:
    settings = config.get("settings", {})
    hour = settings.get("daily_summary_hour")
    if hour is None:
        return
    tz = ZoneInfo(settings.get("timezone", "Europe/Zurich"))
    now = dt.datetime.now(tz)
    today = now.date().isoformat()
    if now.hour < int(hour) or state.get("last_summary") == today:
        return
    lines, problems = [], []
    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        ts = state.get("targets", {}).get(target["id"], {})
        if ts.get("failures", 0) > 0:
            problems.append(label(target))
        if target["type"] != "feed":
            lines.append(f"{label(target)}: {ts.get('status', '–')}")
    msg = "\n".join(lines)
    if problems:
        msg += "\n\nGerade nicht prüfbar: " + ", ".join(problems)
    notifier.send("✅ Wächter läuft – Tagesstatus", msg, 1, ["white_check_mark"])
    state["last_summary"] = today


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="nur Testnachricht senden")
    parser.add_argument("--dry-run", action="store_true", help="nichts senden/speichern")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH,
                        help="Konfigurationsdatei (Standard: config.yaml)")
    args = parser.parse_args()

    # Windows-Konsole/Aufgabenplanung nutzt sonst cp1252 und scheitert an Emojis
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    config_path = args.config if args.config.is_absolute() else Path.cwd() / args.config
    if not config_path.exists():
        config_path = ROOT / args.config
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    # Optional: Ausgabe in eine Log-Datei (für die Windows-Aufgabenplanung)
    log_file = config.get("settings", {}).get("log_file")
    if log_file:
        log_path = ROOT / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            log_path.replace(log_path.with_suffix(".old.log"))
        log = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        print(f"\n=== {dt.datetime.now().isoformat(timespec='seconds')} ===")
    server = os.environ.get("NTFY_SERVER") or config.get("settings", {}).get("ntfy_server", "https://ntfy.sh")
    notifier = Notifier(server, os.environ.get("NTFY_TOPIC"), dry_run=args.dry_run)

    if args.test:
        notifier.send("🎮 Test: GTA-VI-Controller-Wächter",
                      "Wenn du das liest, funktionieren die Benachrichtigungen.", 4, ["tada"])
        return 0

    state_path = state_path_for(config)
    state = load_state(state_path)
    state = run(config, state, notifier)
    state["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not args.dry_run:
        save_state(state, state_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
