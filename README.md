# GTA VI DualSense – Restock-Wächter

Prüft alle 10 Minuten automatisch (auf GitHub, ohne eigenes Gerät), ob der
**DualSense Wireless-Controller „Grand Theft Auto VI“ Limited Edition** (Schwarz und Weiss)
wieder bestellbar ist, und schickt eine Push-Nachricht aufs Handy über [ntfy](https://ntfy.sh).

## Was überwacht wird

| Bereich | Quellen |
|---|---|
| Schweizer Shops | MediaMarkt CH (Produktseite + Suche), Galaxus, Digitec, Brack, Interdiscount, Microspot, Manor, Fust |
| Sony | PlayStation Direct DE/AT (Schwarz ist dort exklusiv), DE (Weiss) |
| Ausland (Lieferung DE/AT) | gameshop.at, Galaxus AT, MediaMarkt DE |
| News / Social | PlayStation Blog, Preispirat, Reddit r/PS5, r/Switzerland |

Die Liste steht in [`config.yaml`](config.yaml) und lässt sich dort ergänzen oder abschalten.

## Welche Nachrichten kommen

- 🎮 **BESTELLBAR** (höchste Priorität, klingelt auch im „Nicht stören“-Modus, wenn erlaubt): eine Produktseite wechselt auf lieferbar oder vorbestellbar. Tippen öffnet den Shop.
- 🔎 **Aufgetaucht**: der Controller erscheint neu in einer Shop-Suche.
- 📰 **News**: ein neuer Beitrag im Blog, auf Preispirat oder Reddit erwähnt den Controller.
- ⚠️ **Warnung**: eine Quelle liefert seit 3 Stunden kein Ergebnis (z. B. Bot-Schutz).
- ✅ **Tagesstatus** (leise, 1× täglich ab 9 Uhr): zeigt, dass der Wächter läuft.

Gemeldet wird nur bei **Änderungen**, nicht bei jedem Lauf.

## Einrichtung (einmalig, ca. 5 Minuten)

1. **ntfy-App** installieren ([iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) und
   mit „+“ ein Thema (Topic) abonnieren. Der Name ist dein „Passwort“: lang und zufällig wählen.
2. Auf GitHub im Repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `NTFY_TOPIC`
   - Wert: derselbe Thema-Name wie in der App
3. **Actions**-Tab öffnen und Workflows aktivieren, falls GitHub danach fragt.
4. Test: **Actions → „GTA VI Controller Wächter“ → Run workflow**, Haken bei
   „Nur eine Testnachricht senden“ setzen und starten. Die Nachricht sollte sofort aufs Handy kommen.
5. Noch einmal **Run workflow** ohne Haken: Das ist der erste echte Lauf. Im Protokoll siehst du
   für jede Quelle den Status (`lieferbar`, `nicht_lieferbar`, `blockiert`, …).

Ab dann läuft alles alle 10 Minuten von selbst.

> Zeitpläne laufen nur auf dem **Standard-Branch** des Repos (Settings → General → Default branch).

## Hinweise

- Einige Shops blockieren automatische Abfragen von Cloud-Servern. Solche Quellen erscheinen im
  Protokoll als `blockiert`, und nach 3 Stunden kommt eine Warnung. Den Eintrag dann in
  `config.yaml` mit `enabled: false` abschalten oder durch eine andere Quelle ersetzen.
- X/Twitter und Instagram werden nicht abgefragt, weil beide automatische Zugriffe sperren
  (die offizielle X-Schnittstelle kostet Geld).
- Die schwarze Version wird laut Berichten nur über PlayStation Direct verkauft, und PlayStation
  Direct beliefert die Schweiz nicht. Dafür braucht es eine Lieferadresse in DE oder AT.

## Lokal testen

```bash
pip install -r requirements.txt pytest
python -m pytest -q
NTFY_TOPIC=dein-thema python monitor.py --dry-run
```
