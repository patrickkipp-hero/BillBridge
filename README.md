# BillBridge

Rechnungen automatisch von Amazon Business und Galaxus herunterladen und per Slack weiterleiten – mit lokaler Web-UI zur Verwaltung.

![Flask](https://img.shields.io/badge/Flask-Web--UI-blue) ![Playwright](https://img.shields.io/badge/Playwright-Automatisierung-green) ![Slack](https://img.shields.io/badge/Slack-Integration-purple)

## Features

- **Amazon Business**: Rechnungen der letzten N Monate automatisch als PDF herunterladen
- **Galaxus**: Bestellrechnungen per Browser-Session herunterladen
- **Weitere Anbieter**: Beliebig viele Custom-Provider in der UI hinzufügen
- **Slack-Integration**: PDFs direkt in einen Channel oder als DM an eine App (z.B. Moss) senden
- **Web-UI**: Übersicht, manuelle Ausführung, Einstellungen – erreichbar unter `http://localhost:5757`
- **Automatischer Tagesrun**: Per macOS LaunchAgent täglich um 10 Uhr (konfigurierbar)

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

## Projektstruktur

```
BillBridge/
├── app.py                  # Flask Web-UI (Port 5757)
├── main.py                 # Scraping-Logik (Amazon, Galaxus)
├── templates/index.html    # Single-Page-App
├── static/                 # CSS/Assets
├── .env.example            # Konfigurationsvorlage
├── providers.example.json  # Vorlage für Custom-Provider
└── requirements.txt
```

## Eigene Anbieter hinzufügen

In der Web-UI unter „Anbieter" → „+ Anbieter hinzufügen". Für automatischen Download muss in `main.py` eine `_scrape_<provider_id>`-Funktion implementiert werden (analog zu `_scrape_galaxus`).

## Lizenz

MIT
