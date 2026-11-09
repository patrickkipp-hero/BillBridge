# BillBridge

Rechnungen automatisch von Amazon Business und Galaxus herunterladen und per Slack weiterleiten – mit lokaler Web-UI zur Verwaltung.

[![Tests](https://github.com/patrickkipp-hero/BillBridge/actions/workflows/tests.yml/badge.svg)](https://github.com/patrickkipp-hero/BillBridge/actions/workflows/tests.yml) ![Flask](https://img.shields.io/badge/Flask-Web--UI-blue) ![Playwright](https://img.shields.io/badge/Playwright-Automatisierung-green) ![Slack](https://img.shields.io/badge/Slack-Integration-purple)

## Features

- **Amazon Business**: Rechnungen der letzten N Monate automatisch als PDF herunterladen
- **Galaxus**: Bestellrechnungen per Browser-Session herunterladen
- **Weitere Anbieter**: Beliebig viele Custom-Provider in der UI hinzufügen
- **Slack-Integration**: PDFs direkt in einen Channel oder als DM an eine App (z.B. Moss) senden
- **Web-UI**: Übersicht, manuelle Ausführung, Einstellungen – erreichbar unter `http://localhost:5757`
- **Automatischer Tagesrun**: Per macOS LaunchAgent täglich um 10 Uhr (konfigurierbar)
- **Störungsmeldungen**: Abgebrochene Läufe und abgelaufene Sessions melden sich selbst in Slack, statt still im Log zu landen

## Voraussetzungen

- macOS (getestet auf macOS 15)
- Python 3.9+
- Google Chrome (für Galaxus-Session-Setup)

## Setup

### Schnellweg: `./setup.sh`

Für eine **eigene Installation pro Person** – jede mit eigenen Zugangsdaten,
eigenem Rechnungsordner und eigenem Slack-Kanal. Es läuft nichts zusammen.

```bash
./setup.sh
```

Das Skript wählt den passenden Python-Interpreter (auf dem PATH kann ein
Python liegen, das die Abhängigkeiten nicht hat), installiert Pakete und
Browser, fragt die Zugangsdaten ab und schreibt sie mit Rechten 600 in die
lokale `.env`, richtet auf Wunsch den LaunchAgent ein und nennt am Ende die
Schritte, die von Hand bleiben: Amazon-Login, Moss-Login und die Prüfung,
dass BillBridge Mitglied deines Slack-Kanals ist.

Eine vorhandene `.env` wird nie ohne Rückfrage überschrieben; vor dem
Ersetzen legt das Skript eine Sicherung an.

**Zur Abgrenzung:** Der Server lauscht ausschließlich auf `127.0.0.1` und ist
aus dem Netz nicht erreichbar. Die Sicherheitsgrenze ist dein
Mac-Benutzerkonto, nicht ein Passwortfeld in der Oberfläche.

### Von Hand


### 1. Repository klonen

```bash
git clone https://github.com/hero-software-de/BillBridge.git
cd BillBridge
```

### 2. Abhängigkeiten installieren

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Konfiguration

```bash
cp .env.example .env
```

`.env` mit eigenen Zugangsdaten befüllen:

```env
# Amazon Business
AMAZON_EMAIL=deine@email.de
AMAZON_PASSWORD=dein-passwort
AMAZON_DOMAIN=amazon.de
MONTHS_BACK=1

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C0123456789

# Optional: DM an eine Slack-App (z.B. Moss)
SLACK_USER_TOKEN=xoxp-...
MOSS_BOT_USER_ID=U0123456789
```

Für Galaxus und weitere Anbieter:

```bash
cp providers.example.json providers.json
```

`providers.json` mit eigenen Zugangsdaten befüllen (oder direkt in der Web-UI unter „Anbieter" anlegen).

### 4. Starten

```bash
python app.py
```

Dann im Browser öffnen: [http://localhost:5757](http://localhost:5757)

## Galaxus einrichten

Da Galaxus automatisierten Login blockiert, läuft das Setup manuell einmalig:

1. In der Web-UI auf **🔑 Session einrichten** klicken
2. Ein neues Chrome-Fenster öffnet sich auf der Galaxus-Login-Seite
3. Im neuen Fenster normal einloggen (Magic Link, Passwort, Google)
4. In BillBridge auf **💾 Session speichern** klicken
5. Ab sofort läuft der Download automatisch

Die Session bleibt dauerhaft erhalten. Bei Ablauf einfach erneut einrichten.

## Slack Bot erstellen

1. [api.slack.com/apps](https://api.slack.com/apps) → „Create New App" → „From scratch"
2. „OAuth & Permissions" → Bot Token Scopes hinzufügen:
   - `files:write`, `chat:write`
3. „Install to Workspace" → `SLACK_BOT_TOKEN` kopieren
4. Bot zum Ziel-Channel einladen: `/invite @BillBridge`
5. Channel-ID: Rechtsklick auf Channel → „View channel details" → ID ganz unten

## Automatischer Tagesrun (macOS)

BillBridge läuft täglich um 10 Uhr via LaunchAgent. Einmalig aktivieren:

```bash
# LaunchAgent-Datei liegt bereits im Projekt
cp de.hero-software.billbridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/de.hero-software.billbridge.plist
```

## Störungsmeldungen

Fehler landeten früher nur in `billbridge.log` – ein Lauf gegen eine abgelaufene
Session sah in der Oberfläche aus wie ein normaler Lauf ohne neue Rechnungen.
Jetzt meldet BillBridge solche Störungen zusätzlich in Slack, mit Hinweis, was
zu tun ist:

- fehlgeschlagener Amazon-, Gmail- oder Anbieter-Lauf
- abgebrochener Moss-Abgleich
- Moss-Fenster geschlossen oder nicht mehr eingeloggt, während Buchungstexte warten

Dieselbe Störung wird höchstens alle 6 Stunden gemeldet (`ALERT_COOLDOWN_HOURS`);
wie oft sie zwischendurch auftrat, steht in der nächsten Nachricht. Läuft es
wieder, kommt eine Entwarnung. Standardziel ist `SLACK_CHANNEL_ID`,
`SLACK_ALERT_CHANNEL_ID` lenkt die Meldungen woanders hin. Welche Störungen
gerade offen sind, steht in `alerts_sent.json`.

## Protokoll

`billbridge.log` wuchs unbegrenzt – nach wenigen Wochen 1,9 MB und 27.772
Zeilen. Jetzt wird bei 2 MB auf das letzte 1 MB gekürzt (`LOG_MAX_BYTES`,
`LOG_KEEP_BYTES`).

Gekürzt wird **in der Datei selbst**, nicht durch Umbenennen wie bei
`logging.handlers.RotatingFileHandler`: Der LaunchAgent leitet stdout und
stderr des Dienstes auf genau diese Datei um und hält sie mit `O_APPEND`
offen (nachprüfbar mit `lsof -p <pid> +fg`, Flag `AP`). Nach einem Umbenennen
schriebe launchd in die umbenannte Datei weiter und das Protokoll liefe an
zwei Stellen auseinander. Weil launchd mit `O_APPEND` schreibt, landet sein
nächster Eintrag nach dem Kürzen am neuen Dateiende – es entsteht keine Lücke.

Zwei Drittel des Inhalts waren außerdem Lärm und fallen jetzt weg:

* **6.300 Zeilen** „Überspringe (kein Rechnungs-PDF)" – jede
  Bestellbestätigung bringt AGB und Widerrufsformular mit, bei jedem Lauf
  aufs Neue. Statt einer Zeile je Anhang steht am Ende eine Summenzeile.
* **6.002 Zeilen** Flask-Zugriffsprotokoll (23 %) – die Oberfläche fragt
  `/api/status` im Sekundentakt ab. Erfolgreiche Abfragen werden gefiltert,
  Fehler (404, 500) bleiben drin.

## Buchungstext-Vorschläge: Lebenszyklus

Ein Vorschlag wird in den Slack-Thread gepostet und wartet dort auf eine
Antwort. Was danach passiert:

| Status | Bedeutung |
|---|---|
| `open` | wartet auf eine Antwort im Thread oder auf das Eintragen |
| `applied` | Buchungstext steht in Moss |
| `ersetzt` | wurde durch einen neu geposteten Thread abgelöst |
| `verfallen` | aufgegeben – der Grund steht in `verfallen_grund` |

Aufgegeben wird aus drei Gründen, jeder mit genau einem Hinweis im Thread:

* **`nicht_auffindbar`** – die Transaktion fehlt seit 14 Tagen durchgehend in
  Moss (`MOSS_PROPOSAL_MISSING_DAYS`), vermutlich exportiert. Gezählt wird die
  Dauer, nicht der einzelne Fehlversuch: Eine Transaktion kann kurzzeitig aus
  der Ansicht fallen, und taucht sie wieder auf, beginnt die Frist von vorn.
* **`ohne_antwort`** – seit 30 Tagen keine Antwort im Thread
  (`MOSS_PROPOSAL_NO_ANSWER_DAYS`). Gilt nur für unbeantwortete; wer geantwortet
  hat, verliert seine Antwort nicht durch Zeitablauf.
* **`gesperrt`** – Moss lässt das Feld nicht mehr bearbeiten. Erkennbar daran,
  dass sich das Detailfeld öffnet, die Steuerung darin aber fehlt. Beim
  Beleg-Upload erscheinen solche Transaktionen im Protokoll als eigene Gruppe
  („in Moss gesperrt") statt in der Sammelzahl unterzugehen.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

Bei jedem Push und jedem Pull Request laufen sie zusätzlich als GitHub Action
(`.github/workflows/tests.yml`), auf Python 3.9 und 3.13. Dort werden
**nur** die Test-Abhängigkeiten installiert – schleicht sich eine Abhängigkeit
auf Playwright, pdfplumber, Slack oder das Netz ein, fällt das in der CI auf
und nicht erst auf dem Rechner einer Kollegin.

Die Tests decken die Stellen ab, an denen falsche Daten nach Moss gelangen
könnten. Sie brauchen weder Browser noch Netz noch `slack_sdk` und laufen
deshalb auch mit einem Python, das die Abhängigkeiten nicht hat – anders als
`app.py` und `main.py` selbst, die denselben Interpreter brauchen, den auch
`setup.sh` wählt (in der Regel `/usr/bin/python3`):

| Datei | Was festgeschrieben ist |
|---|---|
| `test_beleg_zuordnung.py` | Betrag auf den Cent genau, Datumsfenster ±14 Tage, nächstes Datum gewinnt |
| `test_positionen.py` | Produktzeilen aus Amazon-Tabellen, Galaxus (mit/ohne MwSt.-Spalte) und Airalo |
| `test_slack_antwort.py` | Säubern der Thread-Antworten: HTML-Entitäten, Aufzählungen, Mengenangaben |
| `test_stoerungsmeldung.py` | Cooldown, Entwarnung, Verhalten ohne Token und bei Slack-Ausfall |
| `test_vorschlag_lebenszyklus.py` | Fristen zum Ablegen, Zurücksetzen bei wiedergefundener Transaktion, Hinweis im Thread |
| `test_protokoll.py` | Kürzen in derselben Datei (Inode), Zeilengrenzen, Filter fürs Zugriffsprotokoll |

## Projektstruktur

```
BillBridge/
├── app.py                  # Flask Web-UI (Port 5757)
├── main.py                 # Scraping-Logik (Amazon, Galaxus)
├── templates/index.html    # Single-Page-App
├── static/                 # CSS/Assets
├── tests/                  # pytest, ohne Browser und Netz lauffähig
├── .github/workflows/      # CI: pytest auf 3.9 und 3.13
├── .env.example            # Konfigurationsvorlage
├── providers.example.json  # Vorlage für Custom-Provider
└── requirements.txt
```

## Eigene Anbieter hinzufügen

In der Web-UI unter „Anbieter" → „+ Anbieter hinzufügen". Für automatischen Download muss in `main.py` eine `_scrape_<provider_id>`-Funktion implementiert werden (analog zu `_scrape_galaxus`).

## Lizenz

MIT
