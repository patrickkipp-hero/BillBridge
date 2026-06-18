#!/usr/bin/env python3
"""
BillBridge – Amazon-Rechnungen herunterladen und in Slack posten.
"""

import os
import re
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

AMAZON_EMAIL = os.getenv("AMAZON_EMAIL")
AMAZON_PASSWORD = os.getenv("AMAZON_PASSWORD")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN")
MOSS_BOT_USER_ID = os.getenv("MOSS_BOT_USER_ID")
AMAZON_DOMAIN = os.getenv("AMAZON_DOMAIN", "amazon.de")
MONTHS_BACK = int(os.getenv("MONTHS_BACK", "1"))

DOWNLOAD_DIR = Path("invoices")
SEEN_FILE = Path("seen_invoices.txt")


def load_seen_order_ids() -> set:
    if SEEN_FILE.exists():
        return set(SEEN_FILE.read_text().splitlines())
    return set()


def save_seen_order_id(order_id: str):
    with SEEN_FILE.open("a") as f:
        f.write(order_id + "\n")


def check_config():
    missing = []
    for var in ["AMAZON_EMAIL", "AMAZON_PASSWORD", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID"]:
        if not os.getenv(var):
            missing.append(var)
    if missing:
        print(f"Fehlende Umgebungsvariablen: {', '.join(missing)}")
        print("Bitte .env Datei anlegen (siehe .env.example)")
        sys.exit(1)


def download_invoices(headless: bool = True) -> list[Path]:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    downloaded: list[Path] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # Login
        print(f"Melde an bei Amazon ({AMAZON_DOMAIN})...")
        page.goto(f"https://www.{AMAZON_DOMAIN}/gp/sign-in.html")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        def _is_on_login_page():
            return "ap/signin" in page.url or "ap/sign-in" in page.url

        def _is_logged_in():
            u = page.url
            return (
                "ap/signin" not in u
                and "ap/cvf" not in u
                and "ap/mfa" not in u
                and "ax/claim" not in u
            )

        # Schritt 1: E-Mail eingeben
        if _is_on_login_page():
            try:
                # Verschiedene mögliche Selektoren für das E-Mail-Feld
                page.wait_for_selector("input[name='email'], #ap_email", timeout=10_000)
                print(f"  Gebe E-Mail ein: {AMAZON_EMAIL}")
                page.fill("input[name='email'], #ap_email", AMAZON_EMAIL)
                # Continue-Button klicken (verschiedene mögliche Selektoren)
                for btn in ["#continue", "input[type='submit']", "button[type='submit']"]:
                    btn_el = page.query_selector(btn)
                    if btn_el:
                        btn_el.click()
                        break
                time.sleep(3)
            except PwTimeout:
                print("  E-Mail-Feld nicht gefunden, überspringe...")

        # Schritt 2: Passwort eingeben
        try:
            page.wait_for_selector("#ap_password", timeout=12_000)
            print("  Gebe Passwort ein...")
            page.fill("#ap_password", AMAZON_PASSWORD)
            remember = page.query_selector("#rememberMe")
            if remember:
                remember.check()
            page.click("#signInSubmit")
            time.sleep(3)
            page.wait_for_load_state("domcontentloaded")
        except PwTimeout:
            print("  Passwort-Feld nicht gefunden, überspringe...")

        # Schritt 3: Passkey-Dialog automatisch schließen ("No passkeys available")
        try:
            close_btn = page.wait_for_selector("button:has-text('Close'), button:has-text('Schließen')", timeout=5_000)
            if close_btn:
                print("  Schließe Passkey-Dialog...")
                close_btn.click()
                time.sleep(1)
        except PwTimeout:
            pass

        # Schritt 4: Warte bis alle Weiterleitungen abgeschlossen (ax/claim, 2FA, etc.)
        if not _is_logged_in():
            print("  Warte auf Abschluss des Logins (2FA oder Bestätigung falls nötig, max. 60s)...")
            try:
                page.wait_for_url(_is_logged_in, timeout=60_000)
            except PwTimeout:
                print("Login-Timeout.")
                browser.close()
                return []

        print(f"Login erfolgreich. ({page.url})")

        # Bestellübersicht: nur "Paid By You" Filter
        orders_url = f"https://www.{AMAZON_DOMAIN}/-/en/gp/css/order-history?ref_=abn_yadd_ad_your_orders#orderedAndPaidBy/yoYourOrdersPaidByYou/pagination/1/"
        print(f"Öffne Bestellübersicht (Paid By You): {orders_url}")
        page.goto(orders_url)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)

        already_seen = load_seen_order_ids()
        print(f"Bereits verarbeitete Bestellungen: {len(already_seen)}")

        invoice_urls = _collect_invoice_pdf_links(page, already_seen)
        print(f"{len(invoice_urls)} neue Rechnung(en) gefunden.")

        if not invoice_urls:
            print("Keine neuen Rechnungen gefunden.")
            browser.close()
            return []

        for idx, (order_id, pdf_url) in enumerate(invoice_urls, 1):
            print(f"  [{idx}/{len(invoice_urls)}] Lade Rechnung {order_id}...")
            pdf_path = _download_pdf(page, context, pdf_url, order_id)
            if pdf_path:
                downloaded.append(pdf_path)
                save_seen_order_id(order_id)
            else:
                print(f"    Fehlgeschlagen.")

        browser.close()

    return downloaded


def _collect_invoice_pdf_links(page, already_seen: set) -> list[tuple[str, str]]:
    """
    Findet alle Invoice-Popover-URLs auf der Bestellübersicht,
    ruft jeden Popover-Endpoint auf und extrahiert den PDF-Link.
    """
    import json
    from playwright.sync_api import TimeoutError as PwTimeout

    base = f"https://www.{AMAZON_DOMAIN}"
    results = []
    seen_order_ids = set()

    while True:
        time.sleep(2)
        html = page.content()

        # Alle Invoice-Popover-Elemente finden:
        # <span data-a-popover='{"url":"/your-orders/invoice/popover?orderId=XXX&..."}'>
        popover_elements = page.query_selector_all('span[data-a-popover*="invoice/popover"]')
        print(f"  {len(popover_elements)} Invoice-Button(s) auf dieser Seite.")

        for el in popover_elements:
            raw = el.get_attribute("data-a-popover") or ""
            try:
                data = json.loads(raw)
                popover_url = data.get("url", "")
            except Exception:
                # URL direkt per Regex extrahieren
                m = re.search(r'"url"\s*:\s*"([^"]+)"', raw)
                popover_url = m.group(1) if m else ""

            if not popover_url:
                continue

            # Order-ID aus der Popover-URL
            oid_match = re.search(r'orderId=([\d-]+)', popover_url)
            order_id = oid_match.group(1) if oid_match else "unknown"

            if order_id in seen_order_ids or order_id in already_seen:
                print(f"  Überspringe {order_id} (bereits verarbeitet).")
                continue
            seen_order_ids.add(order_id)

            # Popover-Inhalt abrufen
            full_popover_url = base + popover_url if popover_url.startswith("/") else popover_url
            print(f"  Rufe Invoice-Popover auf für {order_id}...")

            pop_page = page.context.new_page()
            try:
                pop_page.goto(full_popover_url, timeout=15_000)
                pop_page.wait_for_load_state("domcontentloaded")
                pop_html = pop_page.content()
            except Exception as e:
                print(f"    Popover-Fehler: {e}")
                pop_page.close()
                continue
            finally:
                pop_page.close()

            # PDF-Link im Popover-Inhalt suchen
            pdf_match = re.search(r'href=["\']([^"\']*(?:invoice\.pdf|/invoice)[^"\']*)["\']', pop_html)
            if not pdf_match:
                # Alle Links ausgeben zur Diagnose
                all_hrefs = re.findall(r'href=["\']([^"\']+)["\']', pop_html)
                print(f"    Kein PDF-Link gefunden. Links im Popover: {all_hrefs}")
                continue

            pdf_url = pdf_match.group(1)
            if pdf_url.startswith("/"):
                pdf_url = base + pdf_url
            print(f"  PDF gefunden: {order_id} -> {pdf_url}")
            results.append((order_id, pdf_url))

        # Nächste Seite?
        next_btn = page.query_selector(".a-pagination .a-last:not(.a-disabled) a")
        if not next_btn:
            break
        print("  Gehe zur nächsten Seite...")
        next_btn.click()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            time.sleep(2)
        except PwTimeout:
            break

    return results


def _download_pdf(page, context, pdf_url: str, order_id: str) -> Optional[Path]:
    """Lädt eine direkte PDF-URL herunter und speichert sie."""
    dest = DOWNLOAD_DIR / f"Amazon_Rechnung_{order_id}.pdf"
    try:
        # Direkt per HTTP mit Session-Cookies herunterladen
        response = context.request.get(pdf_url, timeout=30_000)
        if response.ok:
            dest.write_bytes(response.body())
            print(f"    -> Gespeichert: {dest}")
            return dest
        else:
            print(f"    HTTP-Fehler: {response.status}")
            return None
    except Exception as e:
        print(f"    Fehler beim Speichern: {e}")
        return None


def _parse_amazon_date(text: str) -> Optional[datetime]:
    german_months = {
        "Januar": 1, "Februar": 2, "März": 3, "April": 4,
        "Mai": 5, "Juni": 6, "Juli": 7, "August": 8,
        "September": 9, "Oktober": 10, "November": 11, "Dezember": 12,
    }
    for month_name, month_num in german_months.items():
        if month_name in text:
            parts = text.split()
            try:
                day = int(parts[0].replace(".", ""))
                year = int(parts[-1])
                return datetime(year, month_num, day)
            except (ValueError, IndexError):
                pass
    return None


SESSIONS_DIR = Path("sessions")


def launch_session_browser(provider: dict):
    """
    Öffnet ein neues Chrome-Fenster mit Debug-Port (eigenes Temp-Profil).
    Das bestehende Chrome wird NICHT geschlossen.
    Gibt {"proc", "tmp_dir", "debug_port"} zurück oder None bei Fehler.
    """
    import shutil, tempfile, subprocess as _sp

    chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(chrome_path).exists():
        print("Google Chrome nicht gefunden unter /Applications/.")
        return None

    SESSIONS_DIR.mkdir(exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="billbridge_login_"))
    debug_port = 9223

    print("Öffne neues Chrome-Fenster für Galaxus-Login...")
    proc = _sp.Popen([
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={tmp_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-http2",
        "https://www.galaxus.de/en/sign-in",
    ])
    time.sleep(4)
    print("Browser offen. Bitte bei Galaxus einloggen, dann in BillBridge auf 'Session speichern' klicken.")
    return {"proc": proc, "tmp_dir": str(tmp_dir), "debug_port": debug_port}


def save_session_from_browser(provider: dict, debug_port: int) -> bool:
    """
    Verbindet sich per CDP mit dem Login-Chrome, prüft ob eingeloggt,
    speichert Cookies/Storage als Session-Datei.
    """
    from playwright.sync_api import sync_playwright

    provider_id = provider.get("id", "custom")
    SESSIONS_DIR.mkdir(exist_ok=True)
    session_file = SESSIONS_DIR / f"{provider_id}_session.json"

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{debug_port}")
        except Exception as e:
            print(f"Chrome Debug-Port nicht erreichbar: {e}")
            return False

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("Prüfe Login-Status bei Galaxus...")
        try:
            page.goto("https://www.galaxus.de/en/order", wait_until="domcontentloaded", timeout=20_000)
            time.sleep(2)
        except Exception as e:
            print(f"Navigation fehlgeschlagen: {e}")
            browser.close()
            return False

        if "sign-in" in page.url or "id.galaxus" in page.url or "login" in page.url:
            print("Noch nicht eingeloggt! Bitte zuerst im Chrome-Fenster einloggen.")
            browser.close()
            return False

        ctx.storage_state(path=str(session_file))
        print(f"Session gespeichert: {session_file}")
        browser.close()
    return True


def download_provider_invoices(provider: dict, headless: bool = True) -> list[Path]:
    """Lädt Rechnungen für einen custom Provider herunter (Galaxus, CoolBlue, etc.)."""
    provider_id = provider.get("id", "custom")

    if provider_id == "galaxus":
        return _scrape_galaxus(provider, headless)

    print(f"Automatisierung für '{provider_id}' noch nicht implementiert.")
    return []


PROFILES_DIR = Path("browser_profiles")


def _scrape_galaxus(provider: dict, headless: bool = True) -> list[Path]:
    """
    Galaxus.de Invoice-Scraper.
    Nutzt ein persistentes Chromium-Profil – einmal eingeloggt, dauerhaft gültig.
    Galaxus blockiert headless Chromium, daher läuft der Browser immer sichtbar.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    provider_id = provider.get("id", "galaxus")
    session_file = SESSIONS_DIR / f"{provider_id}_session.json"

    if not session_file.exists():
        print("Galaxus: Keine Session. Bitte '🔑 Session einrichten' in der App klicken.")
        return []

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    downloaded: list[Path] = []
    already_seen = load_seen_order_ids()

    with sync_playwright() as pw:
        # headless=False: Galaxus blockt headless Chromium vollständig
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-http2", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            accept_downloads=True,
            storage_state=str(session_file),
            viewport={"width": 1440, "height": 900},
            locale="de-DE",
        )
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = context.new_page()
        print(f"Galaxus: Nutze Session aus {session_file}.")

        # ── Bestellübersicht ───────────────────────────────────────────────────
        orders_url = "https://www.galaxus.de/en/order"
        print(f"  Öffne Bestellübersicht: {orders_url}")
        page.goto(orders_url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(4)

        # Session-Ablauf erkennen: Weiterleitung auf id.galaxus.eu
        if "id.galaxus" in page.url or "sign-in" in page.url or "login" in page.url:
            print("  Session abgelaufen! Bitte 'Session einrichten' in der App erneut klicken.")
            browser.close()
            return []

        print(f"  Eingeloggt als: {page.url}")

        # Alle PDF / Invoice Links auf der Seite
        pdf_links = _find_galaxus_invoice_links(page, context, already_seen)
        print(f"  {len(pdf_links)} neue Rechnung(en) gefunden.")

        for order_id, pdf_url in pdf_links:
            print(f"  Lade Rechnung {order_id}...")
            dest = DOWNLOAD_DIR / f"Galaxus_Rechnung_{order_id}.pdf"
            try:
                # PDF via Browser-Navigation herunterladen (page.goto triggert Download)
                with page.expect_download(timeout=30_000) as dl_info:
                    try:
                        page.goto(pdf_url, wait_until="commit", timeout=30_000)
                    except Exception as nav_err:
                        # "Download is starting" ist erwartet – Download läuft trotzdem
                        if "Download is starting" not in str(nav_err):
                            raise
                dl = dl_info.value
                dl.save_as(str(dest))
                # Prüfe ob echtes PDF
                if dest.stat().st_size > 1000 and dest.read_bytes()[:4] == b"%PDF":
                    print(f"    -> Gespeichert: {dest} ({dest.stat().st_size // 1024} KB)")
                    downloaded.append(dest)
                    save_seen_order_id(order_id)
                else:
                    print(f"    Kein gültiges PDF, überspringe.")
                    dest.unlink(missing_ok=True)
            except Exception as e:
                print(f"    Download-Fehler: {e}")

        if not pdf_links:
            page.screenshot(path=str(DOWNLOAD_DIR / "galaxus_debug_orders.png"))
            print("  (Debug-Screenshot gespeichert)")

        browser.close()

    return downloaded


def _find_galaxus_invoice_links(page, context, already_seen: set) -> list[tuple[str, str]]:
    """
    Galaxus Invoice-URL: /orderdocuments/pdfproofofpurchase/{orderId}
    Findet alle Order-IDs auf der Bestellübersicht und baut die PDF-URLs direkt.
    """
    base = "https://www.galaxus.de"
    results = []
    seen_ids: set = set()

    # Alle Order-Links auf der Übersichtsseite: /en/order/191502726
    order_links = page.query_selector_all('a[href*="/order/"]')
    order_ids = []
    for el in order_links:
        href = el.get_attribute("href") or ""
        m = re.search(r'/order/(\d+)', href)
        if m and m.group(1) not in order_ids:
            order_ids.append(m.group(1))

    print(f"  {len(order_ids)} Bestellung(en) gefunden: {order_ids}")

    for raw_id in order_ids:
        order_id = f"galaxus_{raw_id}"
        if order_id in seen_ids or order_id in already_seen:
            print(f"  Überspringe {order_id} (bereits verarbeitet).")
            continue
        seen_ids.add(order_id)
        pdf_url = f"{base}/orderdocuments/pdfproofofpurchase/{raw_id}"
        print(f"  Rechnung: {order_id} -> {pdf_url}")
        results.append((order_id, pdf_url))

    return results


def post_to_slack(files: list[Path]) -> None:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    if not files:
        print("Keine Rechnungen zum Hochladen.")
        return

    # Moss-DM: User-Token + DM-Channel per conversations.open öffnen
    moss_channel = None
    if SLACK_USER_TOKEN and MOSS_BOT_USER_ID:
        try:
            user_client = WebClient(token=SLACK_USER_TOKEN)
            dm = user_client.conversations_open(users=MOSS_BOT_USER_ID)
            moss_channel = dm["channel"]["id"]
            print(f"Moss DM-Channel: {moss_channel}")
        except SlackApiError as e:
            print(f"Moss DM konnte nicht geöffnet werden: {e.response['error']}")

    success = 0
    for pdf_path in files:
        print(f"Uploade nach Slack: {pdf_path.name}...")
        targets = []
        if moss_channel:
            targets.append(("Moss", WebClient(token=SLACK_USER_TOKEN), moss_channel))
        if SLACK_CHANNEL_ID:
            targets.append(("Channel", WebClient(token=SLACK_BOT_TOKEN), SLACK_CHANNEL_ID))

        uploaded = False
        for label, client, channel in targets:
            try:
                response = client.files_upload_v2(
                    channel=channel,
                    file=str(pdf_path),
                    filename=pdf_path.name,
                    title=pdf_path.stem.replace("_", " "),
                    initial_comment=f":receipt: Amazon Rechnung: *{pdf_path.stem}*",
                )
                if response["ok"]:
                    print(f"  -> {label}: hochgeladen")
                    uploaded = True
            except SlackApiError as e:
                print(f"  Slack-Fehler ({label}): {e.response['error']}")

        if uploaded:
            success += 1

    print(f"\nFertig: {success}/{len(files)} Rechnungen in Slack gepostet.")


def main():
    parser = argparse.ArgumentParser(
        description="BillBridge – Amazon-Rechnungen nach Slack",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Browser sichtbar anzeigen (hilfreich bei 2FA-Problemen)",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Bereits heruntergeladene PDFs im 'invoices/' Ordner direkt hochladen",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=None,
        help="Wie viele Monate zurück suchen (überschreibt MONTHS_BACK in .env)",
    )
    args = parser.parse_args()

    check_config()

    global MONTHS_BACK
    if args.months:
        MONTHS_BACK = args.months

    if args.skip_download:
        files = sorted(DOWNLOAD_DIR.glob("*.pdf"))
        if not files:
            print("Keine PDFs im 'invoices/' Ordner gefunden.")
            sys.exit(1)
        print(f"{len(files)} lokale PDF(s) gefunden.")
    else:
        files = download_invoices(headless=not args.no_headless)

    post_to_slack(files)


if __name__ == "__main__":
    main()
