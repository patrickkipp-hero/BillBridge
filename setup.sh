#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  BillBridge einrichten – eine eigenständige Installation pro Person.
#
#  Es läuft nichts zusammen: eigene Zugangsdaten, eigener Rechnungsordner,
#  eigener Slack-Kanal. Der Server lauscht ausschließlich auf 127.0.0.1 und
#  ist aus dem Netz nicht erreichbar; die Sicherheitsgrenze ist dein
#  Mac-Benutzerkonto, nicht ein Passwortfeld.
#
#  Aufruf:  ./setup.sh
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

PROJEKT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DATEI="$PROJEKT/.env"
PLIST="$HOME/Library/LaunchAgents/de.hero.billbridge.plist"
LABEL="de.hero.billbridge"
PORT=5757

rot=$'\033[31m'; gruen=$'\033[32m'; gelb=$'\033[33m'; dim=$'\033[2m'; fett=$'\033[1m'; aus=$'\033[0m'
schritt() { printf '\n%s▸ %s%s\n' "$fett" "$1" "$aus"; }
ok()      { printf '  %s✓%s %s\n' "$gruen" "$aus" "$1"; }
warn()    { printf '  %s!%s %s\n' "$gelb" "$aus" "$1"; }
fehler()  { printf '  %s✗%s %s\n' "$rot" "$aus" "$1" >&2; }
abbruch() { fehler "$1"; exit 1; }

frage() {  # frage <Text> <Vorgabe> -> Antwort auf stdout
  local text="$1" vorgabe="${2:-}" antwort
  if [ -n "$vorgabe" ]; then
    read -r -p "    $text [$vorgabe]: " antwort </dev/tty || true
    printf '%s' "${antwort:-$vorgabe}"
  else
    read -r -p "    $text: " antwort </dev/tty || true
    printf '%s' "$antwort"
  fi
}

geheim() {  # wie frage, aber ohne Bildschirmausgabe
  local text="$1" antwort
  read -r -s -p "    $text: " antwort </dev/tty || true
  printf '\n' >&2
  printf '%s' "$antwort"
}

janein() {  # janein <Text> <j|n>  -> Rückgabewert 0 = ja
  local vorgabe="${2:-j}" hinweis antwort
  [ "$vorgabe" = j ] && hinweis="[J/n]" || hinweis="[j/N]"
  read -r -p "    $1 $hinweis " antwort </dev/tty || true
  antwort="${antwort:-$vorgabe}"
  [[ "$antwort" =~ ^[jJyY] ]]
}

printf '%s\n' "╔════════════════════════════════════════════════════════╗"
printf '%s\n' "║  BillBridge – Einrichtung                              ║"
printf '%s\n' "╚════════════════════════════════════════════════════════╝"
printf '%sProjekt: %s%s\n' "$dim" "$PROJEKT" "$aus"

[ "$(uname)" = "Darwin" ] || abbruch "Dieses Skript ist für macOS gedacht."

# ── 1. Python ──────────────────────────────────────────────────────────────
# Wichtig: Auf dem PATH kann ein Python liegen, das die Abhängigkeiten NICHT
# hat (z.B. ein frisch installiertes 3.14 unter /usr/local/bin). Der hier
# gewählte Interpreter wird überall verwendet – auch im LaunchAgent.
schritt "Python wählen"
PY="${BILLBRIDGE_PYTHON:-}"
if [ -z "$PY" ]; then
  for kandidat in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    [ -x "$kandidat" ] || continue
    if "$kandidat" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      PY="$kandidat"; break
    fi
  done
fi
[ -n "$PY" ] || abbruch "Kein Python ≥ 3.9 gefunden. Setze BILLBRIDGE_PYTHON=/pfad/zu/python3."
ok "$PY ($("$PY" -V 2>&1))"
"$PY" -m pip --version >/dev/null 2>&1 || abbruch "pip fehlt für $PY ($PY -m ensurepip --user)."

# ── 2. Abhängigkeiten ──────────────────────────────────────────────────────
schritt "Python-Pakete installieren"
if "$PY" -m pip install --user --quiet --disable-pip-version-check -r "$PROJEKT/requirements.txt"; then
  ok "$(grep -c '[a-z]' "$PROJEKT/requirements.txt") Pakete bereit"
else
  abbruch "Installation der Pakete fehlgeschlagen."
fi

# ── 3. Playwright-Browser ──────────────────────────────────────────────────
schritt "Browser für die Automatisierung"
if "$PY" -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); b.close()' >/dev/null 2>&1; then
  ok "Chromium ist einsatzbereit"
else
  warn "Chromium fehlt – Download etwa 150 MB von Microsofts CDN."
  if janein "Jetzt herunterladen?" j; then
    "$PY" -m playwright install chromium || abbruch "Download fehlgeschlagen."
    ok "Chromium installiert"
  else
    warn "Übersprungen – ohne Browser funktioniert kein Rechnungsabruf."
  fi
fi

# ── 4. Zugangsdaten ────────────────────────────────────────────────────────
schritt "Zugangsdaten (.env)"
if [ -f "$ENV_DATEI" ]; then
  warn "Es gibt bereits eine .env."
  if janein "Bestehende behalten und diesen Schritt überspringen?" j; then
    ok "Bestehende .env unverändert"
    ENV_UEBERSPRUNGEN=1
  else
    sicherung="$ENV_DATEI.$(date +%Y%m%d%H%M%S).bak"
    cp "$ENV_DATEI" "$sicherung"; chmod 600 "$sicherung"
    ok "Sicherung: $(basename "$sicherung")"
  fi
fi

if [ -z "${ENV_UEBERSPRUNGEN:-}" ]; then
  printf '%s    Eingaben werden nur in die lokale .env geschrieben (Rechte 600)\n' "$dim"
  printf '    und nirgendwo angezeigt oder versendet.%s\n\n' "$aus"

  AMAZON_EMAIL="$(frage 'Amazon-E-Mail')"
  AMAZON_PASSWORD="$(geheim 'Amazon-Passwort')"
  AMAZON_DOMAIN="$(frage 'Amazon-Domain' 'amazon.de')"

  printf '\n%s    Slack: Du brauchst einen EIGENEN Kanal – so läuft nichts zusammen.\n' "$dim"
  printf '    Kanal anlegen, BillBridge hineinholen, dann in Slack per\n'
  printf '    Rechtsklick auf den Kanal → "View channel details" die ID unten ablesen.%s\n\n' "$aus"
  SLACK_CHANNEL_ID="$(frage 'Slack-Kanal-ID (C…)')"
  SLACK_BOT_TOKEN="$(geheim 'Slack Bot-Token (xoxb-…)')"
  SLACK_USER_TOKEN="$(geheim 'Slack User-Token (xoxp-…, persönlich, für die Moss-DM)')"
  MOSS_BOT_USER_ID="$(frage 'Moss Bot-User-ID (U…, leer lassen wenn ungenutzt)' '')"

  printf '\n'
  GMAIL_EMAIL="$(frage 'Gmail-Adresse (leer lassen wenn ungenutzt)' '')"
  if [ -n "$GMAIL_EMAIL" ]; then
    GMAIL_APP_PASSWORD="$(geheim 'Gmail-App-Passwort')"
    GMAIL_LABEL="$(frage 'Gmail-Label' 'Orders')"
  else
    GMAIL_APP_PASSWORD=""; GMAIL_LABEL="Orders"
  fi
  MONTHS_BACK="$(frage 'Wie viele Monate zurück suchen?' '1')"

  umask 077
  cat > "$ENV_DATEI" <<EOF
# Von setup.sh erzeugt am $(date '+%d.%m.%Y %H:%M') – enthält Zugangsdaten.
# Steht in .gitignore und darf das Gerät nicht verlassen.

AMAZON_EMAIL=$AMAZON_EMAIL
AMAZON_PASSWORD=$AMAZON_PASSWORD
AMAZON_DOMAIN=$AMAZON_DOMAIN

SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN
SLACK_USER_TOKEN=$SLACK_USER_TOKEN
SLACK_CHANNEL_ID=$SLACK_CHANNEL_ID
MOSS_BOT_USER_ID=$MOSS_BOT_USER_ID

GMAIL_EMAIL=$GMAIL_EMAIL
GMAIL_APP_PASSWORD=$GMAIL_APP_PASSWORD
GMAIL_LABEL=$GMAIL_LABEL

MONTHS_BACK=$MONTHS_BACK
EOF
  chmod 600 "$ENV_DATEI"
  ok ".env geschrieben (Rechte 600)"
  unset AMAZON_PASSWORD SLACK_BOT_TOKEN SLACK_USER_TOKEN GMAIL_APP_PASSWORD
fi

mkdir -p "$PROJEKT/invoices" "$PROJEKT/sessions"
ok "Ordner invoices/ und sessions/ vorhanden"

# ── 5. Hintergrunddienst ───────────────────────────────────────────────────
schritt "Als Hintergrunddienst einrichten"
printf '%s    Startet BillBridge beim Login und nach einem Absturz neu.%s\n' "$dim" "$aus"
if janein "LaunchAgent einrichten?" j; then
  if belegt="$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null)" && [ -n "$belegt" ]; then
    warn "Port $PORT ist belegt (PID $belegt) – vermutlich eine ältere Instanz."
  fi
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$PY</string><string>$PROJEKT/app.py</string></array>
    <!-- Pflicht: main.py arbeitet mit relativen Pfaden (invoices/, sessions/, .env) -->
    <key>WorkingDirectory</key><string>$PROJEKT</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>$PROJEKT/billbridge.log</string>
    <key>StandardErrorPath</key><string>$PROJEKT/billbridge.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- Ohne das puffert Python und das Log bleibt minutenlang leer -->
        <key>PYTHONUNBUFFERED</key><string>1</string>
        <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF
  chmod 644 "$PLIST"
  plutil -lint "$PLIST" >/dev/null || abbruch "Erzeugte plist ist fehlerhaft."
  launchctl bootstrap "gui/$(id -u)" "$PLIST" || abbruch "LaunchAgent konnte nicht geladen werden."

  for _ in $(seq 1 30); do
    curl -fsS -o /dev/null "http://localhost:$PORT/api/status" 2>/dev/null && break
    sleep 1
  done
  if curl -fsS -o /dev/null "http://localhost:$PORT/api/status" 2>/dev/null; then
    ok "Dienst läuft auf http://localhost:$PORT"
  else
    warn "Dienst antwortet noch nicht – siehe billbridge.log"
  fi
else
  warn "Übersprungen. Manueller Start: $PY app.py"
fi

# ── 6. Was noch von Hand fehlt ─────────────────────────────────────────────
cat <<EOF

$fett▸ Fertig – diese Schritte bleiben bei dir$aus

  1. ${fett}Amazon-Login${aus}
     In BillBridge unter Anbieter → Amazon einmal anmelden (2FA im Browser).

  2. ${fett}Moss-Login${aus}
     Für den Beleg-Upload: 🔑 Session einrichten klicken und einloggen.
     Das Fenster muss offen bleiben – Moss-Sitzungen lassen sich nicht
     im Hintergrund wiederverwenden.

  3. ${fett}Slack prüfen${aus}
     BillBridge muss Mitglied deines Kanals sein, sonst schlägt das Posten
     fehl. Für private Kanäle braucht der Token zusätzlich groups:history.

$dim  Bedienung
    Stoppen:      launchctl bootout gui/\$(id -u)/$LABEL
    Starten:      launchctl bootstrap gui/\$(id -u) $PLIST
    Neu starten:  launchctl kickstart -k gui/\$(id -u)/$LABEL
    Log:          tail -f $PROJEKT/billbridge.log

  Deine Daten bleiben auf diesem Rechner: $PROJEKT/invoices/
  Der Server lauscht nur auf 127.0.0.1 und ist aus dem Netz nicht erreichbar.$aus

EOF
