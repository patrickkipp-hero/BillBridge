#!/usr/bin/env python3
"""BillBridge – lokale Web-App"""

import os
import re
import sys
import json
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_file, redirect, url_for
from dotenv import load_dotenv, set_key

load_dotenv()

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
INVOICES_DIR = BASE_DIR / "invoices"
SEEN_FILE = BASE_DIR / "seen_invoices.txt"
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "billbridge.log"
SETTINGS_FILE = BASE_DIR / "settings.json"
PROVIDERS_FILE = BASE_DIR / "providers.json"


def load_providers():
    gmail_configured = bool(os.getenv("GMAIL_EMAIL") and os.getenv("GMAIL_APP_PASSWORD"))
    builtin = [
        {"id": "amazon", "name": "Amazon Business", "color": "#FF9900", "email": os.getenv("AMAZON_EMAIL", ""), "custom": False},
        {"id": "gmail", "name": "Gmail", "color": "#EA4335", "email": os.getenv("GMAIL_EMAIL", ""), "custom": False, "configured": gmail_configured, "label": os.getenv("GMAIL_LABEL", "Orders")},
    ]
    if PROVIDERS_FILE.exists():
        custom = json.loads(PROVIDERS_FILE.read_text())
        return builtin + custom
    return builtin


def save_providers(providers):
    custom = [p for p in providers if p.get("custom")]
    PROVIDERS_FILE.write_text(json.dumps(custom, indent=2, ensure_ascii=False))

run_status = {"running": False, "log": [], "last_run": None, "last_count": 0, "last_new_count": 0}

# Laufende Login-Browser: provider_id → {"proc", "tmp_dir", "debug_port"}
_session_browsers: dict = {}


def load_settings():
    defaults = {
        "download_folder": str(INVOICES_DIR),
        "start_on_login": False,
        "keep_pdfs": True,
        "accent_color": "#FFD700",
        "schedule_enabled": True,
        "schedule_frequency": "daily",
        "schedule_time": "08:00",
        "schedule_days": ["Mo", "Di", "Mi", "Do", "Fr"],
        "rule_check_on_start": True,
        "rule_retry_on_error": True,
        "rule_skip_duplicates": True,
        "rule_macos_notification": False,
        "filename_pattern": "{Jahr}-{Monat}_{Anbieter}_{Betrag}€.pdf",
    }
    if SETTINGS_FILE.exists():
        saved = json.loads(SETTINGS_FILE.read_text())
        defaults.update(saved)
    return defaults


def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def get_invoices():
    INVOICES_DIR.mkdir(exist_ok=True)
    files = sorted(INVOICES_DIR.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        stat = f.stat()
        order_id = re.search(r'(\d{3}-\d{7}-\d{7})', f.name)
        result.append({
            "filename": f.name,
            "order_id": order_id.group(1) if order_id else f.stem,
            "size_kb": round(stat.st_size / 1024, 1),
            "date": datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y"),
            "provider": "Amazon",
        })
    return result


def get_seen_count():
    if SEEN_FILE.exists():
        lines = [l.strip() for l in SEEN_FILE.read_text().splitlines() if l.strip()]
        return len(lines)
    return 0


def get_env_val(key, default=""):
    return os.getenv(key, default)


def _provider_login_urls(provider: dict) -> dict:
    """Sign-in-URL, Order-URL und Logged-out-Marker je nach Provider."""
    if provider["id"] == "amazon":
        domain = get_env_val("AMAZON_DOMAIN", "amazon.de")
        return {
            "signin_url": f"https://www.{domain}/gp/sign-in.html",
            "order_url": f"https://www.{domain}/gp/css/order-history",
            "logged_out_markers": ["ap/signin", "ap/cvf", "ap/mfa", "ax/claim"],
        }
    if provider["id"] == "galaxus":
        return {
            "signin_url": "https://www.galaxus.de/en/sign-in",
            "order_url": "https://www.galaxus.de/en/order",
            "logged_out_markers": ["sign-in", "id.galaxus", "login"],
            "same_url_check": False,
        }
    # Generischer custom Provider (z.B. Moss): eigene Website als Sign-in-URL.
    # Da Login-URLs/-Flows (v.a. SSO) unbekannt sind, prüfen wir stattdessen,
    # ob die Seite nach dem erneuten Aufruf der Login-URL woanders hin
    # weiterleitet (= eingeloggt) oder auf der Login-URL bleibt (= nicht).
    website = provider.get("website") or ""
    return {
        "signin_url": website,
        "order_url": website,
        "logged_out_markers": [],
        "same_url_check": True,
    }


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
def api_dashboard():
    invoices = get_invoices()
    settings = load_settings()
    return jsonify({
        "user_name": get_env_val("AMAZON_EMAIL", "Nutzer").split("@")[0].replace(".", " ").title(),
        "total_invoices": len(invoices),
        "seen_count": get_seen_count(),
        "last_run": run_status["last_run"],
        "last_count": run_status["last_count"],
        "last_new_count": run_status["last_new_count"],
        "running": run_status["running"],
        "next_run": f"morgen {settings['schedule_time']} Uhr" if settings["schedule_enabled"] else "manuell",
        "recent_invoices": invoices[:5],
        "providers": [
            {"name": "Amazon", "status": "aktiv", "count": len(invoices), "color": "#FF9900"}
        ],
    })


@app.route("/api/invoices")
def api_invoices():
    return jsonify(get_invoices())


@app.route("/api/invoice/<filename>")
def api_invoice_file(filename):
    path = INVOICES_DIR / filename
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(path), mimetype="application/pdf")


@app.route("/api/slack", methods=["GET"])
def api_slack_get():
    return jsonify({
        "bot_token": _mask(get_env_val("SLACK_BOT_TOKEN")),
        "user_token": _mask(get_env_val("SLACK_USER_TOKEN")),
        "channel_id": get_env_val("SLACK_CHANNEL_ID"),
        "moss_user_id": get_env_val("MOSS_BOT_USER_ID"),
        "connected": bool(get_env_val("SLACK_BOT_TOKEN")),
    })


@app.route("/api/slack", methods=["POST"])
def api_slack_save():
    data = request.json
    _set_env("SLACK_BOT_TOKEN", data.get("bot_token", ""))
    _set_env("SLACK_USER_TOKEN", data.get("user_token", ""))
    _set_env("SLACK_CHANNEL_ID", data.get("channel_id", ""))
    _set_env("MOSS_BOT_USER_ID", data.get("moss_user_id", ""))
    return jsonify({"ok": True})


@app.route("/api/providers", methods=["GET"])
def api_providers_get():
    return jsonify(load_providers())


@app.route("/api/providers", methods=["POST"])
def api_providers_post():
    providers = load_providers()
    new = request.json
    new["id"] = re.sub(r'\W+', '_', new["name"].lower())
    new["custom"] = True
    # Duplikat-Check
    if not any(p["id"] == new["id"] for p in providers):
        providers.append(new)
    else:
        providers = [new if p["id"] == new["id"] else p for p in providers]
    save_providers(providers)
    return jsonify({"ok": True, "provider": new})


@app.route("/api/providers/<provider_id>", methods=["DELETE"])
def api_providers_delete(provider_id):
    providers = load_providers()
    providers = [p for p in providers if p["id"] != provider_id]
    save_providers(providers)
    return jsonify({"ok": True})


@app.route("/api/amazon", methods=["GET"])
def api_amazon_get():
    return jsonify({
        "email": get_env_val("AMAZON_EMAIL"),
        "password_set": bool(get_env_val("AMAZON_PASSWORD")),
        "domain": get_env_val("AMAZON_DOMAIN", "amazon.de"),
        "months_back": get_env_val("MONTHS_BACK", "1"),
    })


@app.route("/api/amazon", methods=["POST"])
def api_amazon_save():
    data = request.json
    _set_env("AMAZON_EMAIL", data.get("email", ""))
    if data.get("password"):
        _set_env("AMAZON_PASSWORD", data.get("password", ""))
    _set_env("AMAZON_DOMAIN", data.get("domain", "amazon.de"))
    _set_env("MONTHS_BACK", str(data.get("months_back", "1")))
    return jsonify({"ok": True})


@app.route("/api/gmail", methods=["GET"])
def api_gmail_get():
    return jsonify({
        "email": get_env_val("GMAIL_EMAIL"),
        "app_password_set": bool(get_env_val("GMAIL_APP_PASSWORD")),
        "label": get_env_val("GMAIL_LABEL", "Orders"),
    })


@app.route("/api/gmail", methods=["POST"])
def api_gmail_save():
    data = request.json
    _set_env("GMAIL_EMAIL", data.get("email", ""))
    if data.get("app_password"):
        _set_env("GMAIL_APP_PASSWORD", data.get("app_password", ""))
    _set_env("GMAIL_LABEL", data.get("label", "Orders"))
    return jsonify({"ok": True})


@app.route("/api/run_gmail", methods=["POST"])
def api_run_gmail():
    if run_status["running"]:
        return jsonify({"ok": False, "error": "Läuft bereits"})
    thread = threading.Thread(target=_run_gmail_sync)
    thread.daemon = True
    thread.start()
    return jsonify({"ok": True})


def _run_gmail_sync():
    run_status["running"] = True
    run_status["log"] = []

    def _log(msg):
        run_status["log"].append(msg)
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")

    _log(f"=== Gmail-Run ({datetime.now().isoformat()}) ===")
    count_before = len(get_invoices())
    args = [sys.executable, "-c",
        f"import sys; sys.path.insert(0,{str(BASE_DIR)!r}); "
        "from dotenv import load_dotenv; load_dotenv(); "
        "from main import download_gmail_invoices, post_to_slack; "
        "files = download_gmail_invoices(); post_to_slack(files)"
    ]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=str(BASE_DIR))
        for line in proc.stdout:
            line = line.rstrip()
            if line.strip():
                _log(line)
        proc.wait()
        run_status["last_run"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        run_status["last_count"] = len(get_invoices())
        run_status["last_new_count"] = run_status["last_count"] - count_before
    except Exception as e:
        _log(f"Fehler: {e}")
    finally:
        run_status["running"] = False


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    save_settings(request.json)
    return jsonify({"ok": True})


@app.route("/api/run", methods=["POST"])
def api_run():
    if run_status["running"]:
        return jsonify({"ok": False, "error": "Läuft bereits"})
    headless = request.json.get("headless", True)
    thread = threading.Thread(target=_run_sync, args=(headless,))
    thread.daemon = True
    thread.start()
    return jsonify({"ok": True})


@app.route("/api/session_status/<provider_id>")
def api_session_status(provider_id):
    session_file = BASE_DIR / "sessions" / f"{provider_id}_session.json"
    has_session = session_file.exists() and session_file.stat().st_size > 100
    browser_open = provider_id in _session_browsers
    return jsonify({"has_session": has_session, "browser_open": browser_open})


@app.route("/api/setup_session/<provider_id>", methods=["POST"])
def api_setup_session(provider_id):
    if run_status["running"]:
        return jsonify({"ok": False, "error": "Läuft bereits"})
    # Amazon ist ein eingebauter Provider – nicht in providers.json
    if provider_id == "amazon":
        provider = {"id": "amazon", "name": "Amazon"}
    else:
        providers = load_providers()
        provider = next((p for p in providers if p["id"] == provider_id), None)
        if not provider:
            return jsonify({"ok": False, "error": "Anbieter nicht gefunden"})
    thread = threading.Thread(target=_setup_session_sync, args=(provider,))
    thread.daemon = True
    thread.start()
    return jsonify({"ok": True})


def _setup_session_sync(provider: dict):
    """Schritt 1: Login-Chrome öffnen, direkt ohne importlib."""
    import shutil, tempfile

    provider_id = provider["id"]
    run_status["running"] = True
    run_status["log"] = []

    def _log(msg):
        run_status["log"].append(msg)
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")

    _log(f"=== Session-Setup: {provider['name']} ({datetime.now().isoformat()}) ===")

    # Alten Browser für diesen Provider aufräumen falls noch offen
    if provider_id in _session_browsers:
        try:
            _session_browsers[provider_id]["proc"].terminate()
        except Exception:
            pass
        old_tmp = _session_browsers.pop(provider_id, {}).get("tmp_dir")
        if old_tmp:
            shutil.rmtree(old_tmp, ignore_errors=True)

    chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(chrome_path).exists():
        _log("❌ Google Chrome nicht gefunden unter /Applications/.")
        run_status["running"] = False
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="billbridge_login_"))
    debug_port = 9223
    signin_url = _provider_login_urls(provider)["signin_url"]

    _log(f"Öffne neues Chrome-Fenster für den Login ({signin_url})...")
    try:
        proc = subprocess.Popen(
            [
                chrome_path,
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={str(tmp_dir)}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-http2",
                signin_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import time as _time
        _time.sleep(3)
        _session_browsers[provider_id] = {
            "proc": proc,
            "tmp_dir": str(tmp_dir),
            "debug_port": debug_port,
        }
        _log("✅ Chrome-Fenster offen. Bitte einloggen, dann 'Session speichern' klicken.")
    except Exception as e:
        _log(f"❌ Fehler beim Öffnen: {e}")
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

    run_status["running"] = False


@app.route("/api/save_session/<provider_id>", methods=["POST"])
def api_save_session(provider_id):
    """Schritt 2: Session aus dem laufenden Login-Chrome speichern."""
    if provider_id not in _session_browsers:
        return jsonify({"ok": False, "error": "Kein Login-Browser offen. Bitte zuerst 🔑 klicken."})

    providers = load_providers()
    provider = next((p for p in providers if p["id"] == provider_id), None)
    if not provider:
        return jsonify({"ok": False, "error": "Anbieter nicht gefunden"})

    thread = threading.Thread(target=_save_session_sync, args=(provider,))
    thread.daemon = True
    thread.start()
    return jsonify({"ok": True})


def _save_session_sync(provider: dict):
    """Schritt 2: Session per CDP aus dem Login-Chrome extrahieren und speichern."""
    import shutil, tempfile, time as _time

    provider_id = provider["id"]
    run_status["running"] = True
    run_status["log"] = []

    def _log(msg):
        run_status["log"].append(msg)
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")

    _log(f"=== Session speichern: {provider['name']} ({datetime.now().isoformat()}) ===")

    info = _session_browsers.get(provider_id)
    if not info:
        _log("❌ Kein Login-Browser offen. Bitte zuerst 🔑 klicken.")
        run_status["running"] = False
        return

    debug_port = info["debug_port"]
    urls = _provider_login_urls(provider)
    order_url = urls["order_url"]
    same_url_check = urls.get("same_url_check", False)
    if same_url_check:
        # Kein bekanntes Logged-out-Muster (z.B. generischer SSO-Provider):
        # NICHT erneut zur Login-URL navigieren (das würde bei manchen SSO-
        # Flows selbst einen Redirect zurück zum Login auslösen), sondern die
        # Seite so lassen, wie der Nutzer sie nach dem Login verlassen hat,
        # und nur prüfen, ob sie noch auf der Login-URL selbst steht.
        nav_snippet = 'print(f"Aktuelle Seite: {page.url}")'
        logged_out_check = f"page.url.split('?')[0].split('#')[0].rstrip('/') == {order_url!r}.rstrip('/')"
    else:
        nav_snippet = f"""print("Navigiere zur Bestellseite...")
    try:
        page.goto({order_url!r}, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        # Manche Login-Seiten (z.B. SSO-Redirects) brechen die Navigation ab,
        # obwohl die Weiterleitung selbst erfolgreich war - daher hier nicht
        # abbrechen, sondern anhand der finalen URL weiter unten prüfen.
        print(f"Navigation-Hinweis (evtl. Redirect): {{e}}")
    time.sleep(2)
    print(f"Aktuelle URL nach Login-Check: {{page.url}}")"""
        logged_out_check = " or ".join(f'"{m}" in page.url' for m in urls["logged_out_markers"]) or "False"

    # Python-Skript das CDP verbindet und Session speichert, als Subprocess ausführen
    session_file = str(BASE_DIR / "sessions" / f"{provider_id}_session.json")
    script = f"""
import sys, time
sys.path.insert(0, {str(BASE_DIR)!r})
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    try:
        browser = pw.chromium.connect_over_cdp("http://localhost:{debug_port}")
    except Exception as e:
        print(f"CDP-Verbindung fehlgeschlagen: {{e}}")
        sys.exit(1)
    # Nicht blind die erste Seite nehmen: Im Fenster können tote Tabs liegen
    # (Frame has been detached) oder fremde Seiten (Amazon-Login, newtab).
    # Beides hat Abgleiche stumm ins Leere laufen lassen.
    seiten = [p for c in browser.contexts for p in c.pages if not p.is_closed()]
    print(f"Seiten im Fenster: {{[ (p.url or '')[:60] for p in seiten ]}}")
    page = next((p for p in seiten if "getmoss.com" in (p.url or "")), None)
    if page is None:
        page = seiten[0] if seiten else (
            browser.contexts[0] if browser.contexts else browser.new_context()).new_page()
        print("Keine Moss-Seite im Fenster – öffne sie.")
        page.goto("https://getmoss.com/app/transactions/all",
                  wait_until="domcontentloaded", timeout=30_000)
        time.sleep(3)
    {nav_snippet}
    if {logged_out_check}:
        print("Noch nicht eingeloggt!")
        browser.close()
        sys.exit(1)
    import pathlib
    pathlib.Path({str(BASE_DIR / 'sessions')!r}).mkdir(exist_ok=True)
    ctx.storage_state(path={session_file!r})
    print(f"Session gespeichert.")
    browser.close()
    sys.exit(0)
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
        tmp.write(script)
        tmp_path = tmp.name

    try:
        proc = subprocess.Popen(
            [sys.executable, tmp_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(BASE_DIR),
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line.strip():
                _log(line)
        proc.wait()
        ok = proc.returncode == 0
    except Exception as e:
        _log(f"Fehler: {e}")
        ok = False
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Browser aufräumen
    try:
        info["proc"].terminate()
    except Exception:
        pass
    shutil.rmtree(info.get("tmp_dir", ""), ignore_errors=True)
    _session_browsers.pop(provider_id, None)

    if ok:
        _log("✅ Session gespeichert – automatischer Sync ist jetzt aktiv.")
    else:
        _log("❌ Noch nicht eingeloggt? Bitte zuerst einloggen, dann nochmal 'Session speichern' klicken.")

    run_status["running"] = False


@app.route("/api/moss_reconcile", methods=["POST"])
def api_moss_reconcile():
    """Gleicht offene Moss-Belege im gerade eingeloggten Login-Browser ab und
    verschickt gefundene Rechnungen per Mail. Braucht ein offenes Login-Fenster
    (🔑 Session einrichten), da Moss-Sessions nicht headless wiederverwendbar sind."""
    if run_status["running"]:
        return jsonify({"ok": False, "error": "Läuft bereits"})
    if "moss" not in _session_browsers:
        return jsonify({"ok": False, "error": "Kein Login-Browser offen. Bitte zuerst 🔑 klicken."})

    providers = load_providers()
    provider = next((p for p in providers if p["id"] == "moss"), None)
    if not provider:
        return jsonify({"ok": False, "error": "Moss-Anbieter nicht gefunden"})

    thread = threading.Thread(target=_moss_reconcile_sync, args=(provider,))
    thread.daemon = True
    thread.start()
    return jsonify({"ok": True})


def _moss_reconcile_sync(provider: dict):
    """Verbindet per CDP zum offenen Moss-Login-Browser, prüft den Login-Status
    und führt darin main.sync_moss_open_invoices() aus (live, kein Session-Replay)."""
    import tempfile

    provider_id = provider["id"]
    run_status["running"] = True
    run_status["log"] = []

    def _log(msg):
        run_status["log"].append(msg)
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")

    _log(f"=== Moss-Abgleich ({datetime.now().isoformat()}) ===")

    info = _session_browsers.get(provider_id)
    if not info:
        _log("❌ Kein Login-Browser offen. Bitte zuerst 🔑 klicken.")
        run_status["running"] = False
        return

    debug_port = info["debug_port"]
    urls = _provider_login_urls(provider)
    signin_url = urls["signin_url"]
    logged_out_check = f"page.url.split('?')[0].split('#')[0].rstrip('/') == {signin_url!r}.rstrip('/')"

    script = f"""
import sys, time
sys.path.insert(0, {str(BASE_DIR)!r})
from playwright.sync_api import sync_playwright
import main as billbridge_main
with sync_playwright() as pw:
    try:
        browser = pw.chromium.connect_over_cdp("http://localhost:{debug_port}")
    except Exception as e:
        print(f"CDP-Verbindung fehlgeschlagen: {{e}}")
        sys.exit(1)
    # Nicht blind die erste Seite nehmen: Im Fenster können tote Tabs liegen
    # (Frame has been detached) oder fremde Seiten (Amazon-Login, newtab).
    # Beides hat Abgleiche stumm ins Leere laufen lassen.
    seiten = [p for c in browser.contexts for p in c.pages if not p.is_closed()]
    print(f"Seiten im Fenster: {{[ (p.url or '')[:60] for p in seiten ]}}")
    page = next((p for p in seiten if "getmoss.com" in (p.url or "")), None)
    if page is None:
        page = seiten[0] if seiten else (
            browser.contexts[0] if browser.contexts else browser.new_context()).new_page()
        print("Keine Moss-Seite im Fenster – öffne sie.")
        page.goto("https://getmoss.com/app/transactions/all",
                  wait_until="domcontentloaded", timeout=30_000)
        time.sleep(3)
    print(f"Aktuelle Seite: {{page.url}}")
    if {logged_out_check}:
        print("Noch nicht eingeloggt!")
        browser.close()
        sys.exit(1)
    try:
        billbridge_main.sync_moss_open_invoices(page)
    except Exception as e:
        print(f"Abgleich-Fehler: {{e}}")
        browser.close()
        sys.exit(1)
    browser.close()
    sys.exit(0)
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
        tmp.write(script)
        tmp_path = tmp.name

    try:
        proc = subprocess.Popen(
            [sys.executable, tmp_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(BASE_DIR),
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line.strip():
                _log(line)
        proc.wait()
        ok = proc.returncode == 0
    except Exception as e:
        _log(f"Fehler: {e}")
        ok = False
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Login-Fenster bewusst offen lassen: nur solange es steht, kann der
    # 15-Minuten-Listener freigegebene Buchungstexte nachtragen. Aufgeräumt
    # wird es beim nächsten 🔑 Session einrichten (siehe _setup_session_sync)
    # oder wenn die CDP-Verbindung wegbricht (_moss_apply_buchungstexte).
    if ok:
        _log("✅ Moss-Abgleich abgeschlossen.")
    else:
        _log("❌ Moss-Abgleich fehlgeschlagen. Bitte erneut einloggen und versuchen.")
    _log("ℹ️ Login-Fenster bleibt offen – der Listener trägt damit alle 15 Min "
         "freigegebene Buchungstexte nach. Fenster einfach schließen, wenn du fertig bist.")

    run_status["running"] = False


@app.route("/api/run_provider/<provider_id>", methods=["POST"])
def api_run_provider(provider_id):
    if run_status["running"]:
        return jsonify({"ok": False, "error": "Läuft bereits"})
    providers = load_providers()
    provider = next((p for p in providers if p["id"] == provider_id), None)
    if not provider:
        return jsonify({"ok": False, "error": "Anbieter nicht gefunden"})
    headless = (request.json or {}).get("headless", True)
    thread = threading.Thread(target=_run_provider_sync, args=(provider, headless))
    thread.daemon = True
    thread.start()
    return jsonify({"ok": True})


def _run_provider_sync(provider: dict, headless: bool = True):
    import importlib.util, io
    run_status["running"] = True
    run_status["log"] = []

    def _log(msg):
        run_status["log"].append(msg)
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")

    # stdout umleiten damit print() aus main.py ins Log kommt
    class _Tee(io.TextIOBase):
        def __init__(self, orig):
            self._orig = orig
        def write(self, s):
            self._orig.write(s)
            for line in s.splitlines():
                if line.strip():
                    _log(line)
            return len(s)
        def flush(self):
            self._orig.flush()

    import sys
    old_stdout = sys.stdout
    sys.stdout = _Tee(old_stdout)

    _log(f"=== Provider-Run: {provider['name']} ({datetime.now().isoformat()}) ===")
    try:
        spec = importlib.util.spec_from_file_location("main", str(BASE_DIR / "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        files = mod.download_provider_invoices(provider, headless=headless)
        _mark_provider_run(provider["id"])
        _log(f"{len(files)} Rechnung(en) heruntergeladen.")
        if files:
            mod.post_to_slack(files)
        run_status["last_run"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        run_status["last_count"] = len(get_invoices())
        run_status["last_new_count"] = len(files)
    except Exception as e:
        import traceback
        _log(f"Fehler: {e}")
        _log(traceback.format_exc())
    finally:
        sys.stdout = old_stdout
        run_status["running"] = False


@app.route("/api/status")
def api_status():
    return jsonify({
        "running": run_status["running"],
        "log": run_status["log"][-50:],
        "last_run": run_status["last_run"],
        "last_count": run_status["last_count"],
        "last_new_count": run_status["last_new_count"],
    })


@app.route("/api/logs")
def api_logs():
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text().splitlines()[-200:]
        return jsonify({"lines": lines})
    return jsonify({"lines": []})


# ── Helpers ─────────────────────────────────────────────────────────────────

def _mask(val):
    if not val or len(val) < 10:
        return val
    return val[:6] + "••••••" + val[-4:]


def _set_env(key, value):
    set_key(str(ENV_FILE), key, value)
    os.environ[key] = value


def _run_sync(headless=True):
    run_status["running"] = True
    run_status["log"] = []
    count_before = len(get_invoices())
    args = ["python3", str(BASE_DIR / "main.py")]
    if not headless:
        args.append("--no-headless")

    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(BASE_DIR)
        )
        with LOG_FILE.open("a") as logf:
            logf.write(f"\n=== Run {datetime.now().isoformat()} ===\n")
            for line in proc.stdout:
                line = line.rstrip()
                run_status["log"].append(line)
                logf.write(line + "\n")
        proc.wait()
        run_status["last_run"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        run_status["last_count"] = len(get_invoices())
        run_status["last_new_count"] = run_status["last_count"] - count_before
    except Exception as e:
        run_status["log"].append(f"Fehler: {e}")
    finally:
        run_status["running"] = False


# ── Moss Listener ───────────────────────────────────────────────────────────

MOSS_LAST_TS_FILE = BASE_DIR / "moss_last_ts.txt"
moss_listener_status = {"active": False, "last_check": None, "last_trigger": None}

MOSS_TRIGGER_KEYWORDS = [
    "offenen posten", "offene posten", "fehlende belege", "fehlen", "belege",
    "zuordnen", "nicht zuordnen", "open items", "missing receipts",
]


def _moss_load_last_ts() -> str:
    if MOSS_LAST_TS_FILE.exists():
        return MOSS_LAST_TS_FILE.read_text().strip()
    return "0"


def _moss_save_last_ts(ts: str):
    MOSS_LAST_TS_FILE.write_text(ts)


PROVIDER_LAST_RUN_FILE = BASE_DIR / "provider_last_run.json"
MOSS_SYNC_COOLDOWN_MIN = 60


def _provider_ran_today(provider_id: str) -> bool:
    try:
        data = json.loads(PROVIDER_LAST_RUN_FILE.read_text())
        return data.get(provider_id) == datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return False


def _mark_provider_run(provider_id: str):
    try:
        data = json.loads(PROVIDER_LAST_RUN_FILE.read_text()) if PROVIDER_LAST_RUN_FILE.exists() else {}
    except Exception:
        data = {}
    data[provider_id] = datetime.now().strftime("%Y-%m-%d")
    PROVIDER_LAST_RUN_FILE.write_text(json.dumps(data, indent=2))


def _moss_check_and_sync():
    """Prüft Moss-DM auf neue Nachrichten und startet Sync wenn nötig."""
    import time as _time
    bot_token = os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_USER_TOKEN")
    user_token = os.getenv("SLACK_USER_TOKEN")
    moss_user_id = os.getenv("MOSS_BOT_USER_ID")

    if not (user_token and moss_user_id):
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        user_client = WebClient(token=user_token)
        dm = user_client.conversations_open(users=moss_user_id)
        channel_id = dm["channel"]["id"]

        last_ts = _moss_load_last_ts()
        resp = user_client.conversations_history(channel=channel_id, oldest=last_ts, limit=20)
        messages = resp.get("messages", [])

        if not messages:
            return

        # Neueste Timestamp merken (erste Nachricht ist die neueste)
        newest_ts = messages[0]["ts"]

        triggered = False
        for msg in messages:
            # Nur echte Nachrichten der Moss-App auswerten – eigene Uploads,
            # eigene Antworten und sonstige Events würden sonst eine
            # Endlosschleife auslösen (Sync -> Antwort -> neuer Trigger).
            if msg.get("user") != moss_user_id:
                continue
            text = msg.get("text", "").lower()
            if any(kw in text for kw in MOSS_TRIGGER_KEYWORDS):
                triggered = True
                moss_listener_status["last_trigger"] = datetime.now().strftime("%d.%m.%Y %H:%M")
                with LOG_FILE.open("a") as f:
                    f.write(f"\n[Moss-Listener] Trigger erkannt: {msg['text'][:120]}\n")
                break

        _moss_save_last_ts(newest_ts)

        if not triggered or run_status["running"]:
            return

        # Abkühlzeit: höchstens ein automatischer Sync pro Stunde
        last_sync = moss_listener_status.get("last_sync_epoch", 0)
        if _time.time() - last_sync < MOSS_SYNC_COOLDOWN_MIN * 60:
            with LOG_FILE.open("a") as f:
                f.write(f"[Moss-Listener] Trigger ignoriert (Abkühlzeit {MOSS_SYNC_COOLDOWN_MIN} Min).\n")
            return
        moss_listener_status["last_sync_epoch"] = _time.time()

        # Sync starten
        with LOG_FILE.open("a") as f:
            f.write(f"[Moss-Listener] Starte automatischen Sync ({datetime.now().isoformat()})\n")

        # Alle Provider synchronisieren
        providers = load_providers()
        all_files: list[Path] = []

        # Amazon
        try:
            proc = subprocess.run(
                [sys.executable, str(BASE_DIR / "main.py")],
                capture_output=True, text=True, cwd=str(BASE_DIR), timeout=300
            )
            with LOG_FILE.open("a") as f:
                f.write(proc.stdout[-2000:] if proc.stdout else "")
        except Exception as e:
            with LOG_FILE.open("a") as f:
                f.write(f"Amazon-Fehler: {e}\n")

        # Custom Provider (Galaxus etc.) – höchstens einmal pro Tag,
        # da z.B. Galaxus ein sichtbares Browser-Fenster öffnet.
        import importlib.util
        for provider in providers:
            if not provider.get("custom"):
                continue
            if _provider_ran_today(provider["id"]):
                with LOG_FILE.open("a") as f:
                    f.write(f"[Moss-Listener] {provider['id']} heute schon gelaufen, überspringe.\n")
                continue
            try:
                spec = importlib.util.spec_from_file_location("main", str(BASE_DIR / "main.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                files = mod.download_provider_invoices(provider, headless=True)
                _mark_provider_run(provider["id"])
                all_files.extend(files)
                if files:
                    mod.post_to_slack(files)
            except Exception as e:
                with LOG_FILE.open("a") as f:
                    f.write(f"Provider {provider['id']} Fehler: {e}\n")

        # Gmail
        try:
            spec = importlib.util.spec_from_file_location("main", str(BASE_DIR / "main.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            gmail_files = mod.download_gmail_invoices()
            all_files.extend(gmail_files)
            if gmail_files:
                mod.post_to_slack(gmail_files)
        except Exception as e:
            with LOG_FILE.open("a") as f:
                f.write(f"Gmail-Fehler: {e}\n")

        # Antwort an Moss
        if all_files:
            names = "\n".join(f"• {f.name}" for f in all_files)
            reply = f"✅ BillBridge hat {len(all_files)} neue Rechnung(en) gefunden und hochgeladen:\n{names}"
        else:
            reply = (
                "ℹ️ BillBridge hat alle konfigurierten Quellen geprüft "
                "(Amazon, Galaxus, Gmail) – keine neuen Rechnungen gefunden.\n"
                "Für getjoan.com und andere Anbieter ist noch kein automatischer Download möglich."
            )

        try:
            user_client.chat_postMessage(channel=channel_id, text=reply)
        except SlackApiError as e:
            with LOG_FILE.open("a") as f:
                f.write(f"Moss-Antwort fehlgeschlagen: {e}\n")

    except Exception as e:
        with LOG_FILE.open("a") as f:
            f.write(f"[Moss-Listener] Fehler: {e}\n")


MOSS_PROPOSALS_FILE = BASE_DIR / "moss_proposals.json"


def _open_proposal_count() -> int:
    """Wie viele Buchungstext-Vorschläge warten auf eine Thread-Antwort."""
    try:
        store = json.loads(MOSS_PROPOSALS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    return sum(1 for r in store.values() if r.get("status") == "open")


def _moss_apply_buchungstexte():
    """Trägt im Slack-Thread freigegebene Buchungstexte in Moss ein.

    Braucht ein offenes, eingeloggtes Moss-Login-Fenster (🔑), weil
    Moss-Sessions nicht headless wiederverwendbar sind. Ist keins offen, wird
    der Versuch vertagt statt als Fehler behandelt – der nächste Zyklus oder
    der nächste manuelle Abgleich holt es nach.
    """
    import tempfile

    open_count = _open_proposal_count()
    moss_listener_status["open_proposals"] = open_count
    if open_count == 0 or run_status["running"]:
        return

    if "moss" not in _session_browsers:
        # Nur bei Zustandswechsel loggen, sonst läuft das Log alle 15 Min voll.
        if not moss_listener_status.get("apply_deferred"):
            moss_listener_status["apply_deferred"] = True
            with LOG_FILE.open("a") as f:
                f.write(f"[Moss-Listener] {open_count} Buchungstext-Vorschlag(e) offen, "
                        f"aber kein Login-Browser offen – vertagt (🔑 Session einrichten).\n")
        return
    moss_listener_status["apply_deferred"] = False

    info = _session_browsers["moss"]
    script = f"""
import sys, time
sys.path.insert(0, {str(BASE_DIR)!r})
from playwright.sync_api import sync_playwright
import main as billbridge_main
with sync_playwright() as pw:
    try:
        browser = pw.chromium.connect_over_cdp("http://localhost:{info['debug_port']}")
    except Exception as e:
        print(f"CDP-Verbindung fehlgeschlagen: {{e}}")
        sys.exit(1)
    # Nicht blind die erste Seite nehmen: Im Fenster können tote Tabs liegen
    # (Frame has been detached) oder fremde Seiten (Amazon-Login, newtab).
    # Beides hat Abgleiche stumm ins Leere laufen lassen.
    seiten = [p for c in browser.contexts for p in c.pages if not p.is_closed()]
    print(f"Seiten im Fenster: {{[ (p.url or '')[:60] for p in seiten ]}}")
    page = next((p for p in seiten if "getmoss.com" in (p.url or "")), None)
    if page is None:
        page = seiten[0] if seiten else (
            browser.contexts[0] if browser.contexts else browser.new_context()).new_page()
        print("Keine Moss-Seite im Fenster – öffne sie.")
        page.goto("https://getmoss.com/app/transactions/all",
                  wait_until="domcontentloaded", timeout=30_000)
        time.sleep(3)
    if "getmoss.com" not in page.url or "signin" in page.url:
        print(f"Moss nicht eingeloggt (Seite: {{page.url}}) - vertagt.")
        browser.close()
        sys.exit(1)
    try:
        billbridge_main.apply_buchungstexte_from_slack(page)
    except Exception as e:
        print(f"Buchungstext-Uebernahme fehlgeschlagen: {{e}}")
        browser.close()
        sys.exit(1)
    # Nur die CDP-Verbindung trennen - das Login-Fenster bleibt offen.
    browser.close()
    sys.exit(0)
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
        tmp.write(script)
        tmp_path = tmp.name
    try:
        proc = subprocess.run([sys.executable, tmp_path], capture_output=True,
                              text=True, cwd=str(BASE_DIR), timeout=600)
        out = (proc.stdout or "") + (proc.stderr or "")
        with LOG_FILE.open("a") as f:
            f.write(f"[Moss-Listener] Buchungstext-Übernahme ({datetime.now().isoformat()}):\n")
            f.write(out)
        moss_listener_status["last_apply"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        # Fenster von Hand geschlossen: Registrierung ist tot, sonst würde der
        # Listener bis in die Ewigkeit gegen einen leeren Debug-Port laufen.
        if "CDP-Verbindung fehlgeschlagen" in out:
            import shutil as _shutil
            stale = _session_browsers.pop("moss", None)
            if stale:
                try:
                    stale["proc"].terminate()
                except Exception:
                    pass
                _shutil.rmtree(stale.get("tmp_dir", ""), ignore_errors=True)
            with LOG_FILE.open("a") as f:
                f.write("[Moss-Listener] Login-Fenster nicht mehr erreichbar – "
                        "Registrierung verworfen, warte auf neues 🔑 Session einrichten.\n")
    except Exception as e:
        with LOG_FILE.open("a") as f:
            f.write(f"[Moss-Listener] Buchungstext-Übernahme Fehler: {e}\n")
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    moss_listener_status["open_proposals"] = _open_proposal_count()


def _moss_listener_loop():
    """Hintergrund-Thread: alle 15 Minuten Moss-DM prüfen."""
    import time as _time
    moss_listener_status["active"] = True
    _time.sleep(30)  # Kurz warten bis Flask vollständig gestartet ist
    while True:
        moss_listener_status["last_check"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        _moss_check_and_sync()
        # Unabhängig vom Moss-DM-Trigger: freigegebene Buchungstexte eintragen.
        try:
            _moss_apply_buchungstexte()
        except Exception as e:
            with LOG_FILE.open("a") as f:
                f.write(f"[Moss-Listener] Apply-Fehler: {e}\n")
        _time.sleep(900)  # 15 Minuten


@app.route("/api/moss_listener")
def api_moss_listener():
    return jsonify(moss_listener_status)


if __name__ == "__main__":
    # Moss-Listener als Daemon-Thread starten
    listener_thread = threading.Thread(target=_moss_listener_loop, daemon=True)
    listener_thread.start()

    print("BillBridge läuft auf http://localhost:5757")
    app.run(host="127.0.0.1", port=5757, debug=False)
