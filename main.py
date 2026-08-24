#!/usr/bin/env python3
"""
BillBridge – Amazon-Rechnungen herunterladen und in Slack posten.
"""

import os
import re
import html
import sys
import time
import json
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


def _amazon_do_login(page, PwTimeout) -> bool:
    """Führt den Amazon-Login-Flow durch. Gibt True zurück wenn erfolgreich."""
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

    page.goto(f"https://www.{AMAZON_DOMAIN}/gp/sign-in.html")
    page.wait_for_load_state("domcontentloaded")
    time.sleep(2)

    if _is_on_login_page():
        try:
            page.wait_for_selector("input[name='email'], #ap_email", timeout=10_000)
            print(f"  Gebe E-Mail ein: {AMAZON_EMAIL}")
            page.fill("input[name='email'], #ap_email", AMAZON_EMAIL)
            for btn in ["#continue", "input[type='submit']", "button[type='submit']"]:
                btn_el = page.query_selector(btn)
                if btn_el:
                    btn_el.click()
                    break
            time.sleep(3)
        except PwTimeout:
            print("  E-Mail-Feld nicht gefunden, überspringe...")

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

    try:
        close_btn = page.wait_for_selector("button:has-text('Close'), button:has-text('Schließen')", timeout=5_000)
        if close_btn:
            print("  Schließe Passkey-Dialog...")
            close_btn.click()
            time.sleep(1)
    except PwTimeout:
        pass

    if not _is_logged_in():
        print("  Warte auf Abschluss des Logins (2FA oder Bestätigung, max. 120s)...")
        try:
            page.wait_for_url(_is_logged_in, timeout=120_000)
        except PwTimeout:
            print("Login-Timeout.")
            return False

    return True


def download_invoices(headless: bool = True) -> list[Path]:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    SESSIONS_DIR.mkdir(exist_ok=True)
    downloaded: list[Path] = []
    amazon_session = SESSIONS_DIR / "amazon_session.json"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)

        # Gespeicherte Session laden falls vorhanden
        session_loaded = False
        if amazon_session.exists():
            print("Amazon: Lade gespeicherte Session...")
            context = browser.new_context(
                accept_downloads=True,
                storage_state=str(amazon_session),
            )
            page = context.new_page()
            page.goto(f"https://www.{AMAZON_DOMAIN}/gp/css/order-history", wait_until="domcontentloaded")
            time.sleep(3)
            if "ap/signin" not in page.url and "sign-in" not in page.url:
                print("Amazon: Session gültig, Login übersprungen.")
                session_loaded = True
            else:
                print("Amazon: Session abgelaufen, melde neu an...")
                context.close()

        if not session_loaded:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            print(f"Melde an bei Amazon ({AMAZON_DOMAIN})...")
            ok = _amazon_do_login(page, PwTimeout)
            if not ok:
                browser.close()
                return []
            # Session für zukünftige Runs speichern
            context.storage_state(path=str(amazon_session))
            print(f"Amazon: Session gespeichert ({amazon_session})")

        print(f"Login erfolgreich. ({page.url})")

        # Bestellübersicht: alle Bestellungen (der "Paid By You"-Filter blendet
        # Bestellungen aus, die mit einer Firmenkarte statt persönlich bezahlt
        # wurden, und hat dadurch mehrere Rechnungen verschluckt). Immer explizit
        # anfahren, damit der Hash-Filter unabhängig vom Login-Pfad greift.
        orders_url = f"https://www.{AMAZON_DOMAIN}/-/en/gp/css/order-history?ref_=abn_yadd_ad_your_orders#orders/pagination/1/"
        print(f"Öffne Bestellübersicht: {orders_url}")
        page.goto(orders_url)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)

        already_seen = load_seen_order_ids()
        print(f"Bereits verarbeitete Bestellungen: {len(already_seen)}")

        invoice_urls, all_meta = _collect_invoice_pdf_links(page, already_seen)
        _rename_existing_invoices(all_meta)
        print(f"{len(invoice_urls)} neue Rechnung(en) gefunden.")

        for idx, invoice in enumerate(invoice_urls, 1):
            order_id = invoice["order_id"]
            print(f"  [{idx}/{len(invoice_urls)}] Lade Rechnung(en) für {order_id}...")
            got_any = False
            for part, pdf_url in enumerate(invoice["pdf_urls"]):
                pdf_path = _download_pdf(page, context, pdf_url, order_id, invoice["date"], invoice["price"], part=part)
                if pdf_path:
                    downloaded.append(pdf_path)
                    got_any = True
            if got_any:
                save_seen_order_id(order_id)
            else:
                print(f"    Fehlgeschlagen.")

        # Session nach JEDEM erfolgreichen Lauf zurückschreiben: Amazon rotiert
        # Cookies laufend – ohne Refresh veraltet die gespeicherte Session nach
        # wenigen Tagen und der nächste headless Login scheitert an 2FA.
        context.storage_state(path=str(amazon_session))
        print("Amazon: Session aktualisiert.")

        browser.close()

    return downloaded


_ORDER_META_JS = """
el => {
    const card = el.closest('.a-box-group') || el.closest('#orderCard');
    if (!card) return {date: '', total: ''};
    let date = '', total = '';
    card.querySelectorAll('.a-column').forEach(col => {
        const label = col.querySelector('.a-color-secondary');
        const value = col.querySelector('.a-size-base');
        if (!label || !value) return;
        const l = label.textContent.trim();
        if (/Order placed|Bestellung aufgegeben/i.test(l)) date = value.textContent.trim();
        if (/^Total$|Gesamtsumme/i.test(l)) total = value.textContent.trim();
    });
    return {date, total};
}
"""


def _order_meta_to_tokens(raw_date: str, raw_total: str) -> tuple[str, str]:
    parsed = _parse_amazon_date(raw_date) if raw_date else None
    date_token = parsed.strftime("%Y-%m-%d") if parsed else "unbekannt-datum"
    return date_token, _price_to_filename_token(raw_total)


def _rename_existing_invoices(all_meta: dict) -> None:
    """Benennt bereits heruntergeladene Amazon-PDFs im alten Format
    (Amazon_Rechnung_{order_id}.pdf) auf das neue Format mit Datum/Preis um."""
    for pdf_path in DOWNLOAD_DIR.glob("Amazon_Rechnung_*.pdf"):
        order_id = _order_id_from_filename(pdf_path.name)
        if not order_id or order_id not in all_meta:
            continue
        date_token, price_token = all_meta[order_id]
        new_path = DOWNLOAD_DIR / f"Amazon_Rechnung_{date_token}_{order_id}_{price_token}.pdf"
        if new_path == pdf_path or new_path.exists():
            continue
        pdf_path.rename(new_path)
        print(f"  Umbenannt: {pdf_path.name} -> {new_path.name}")


def _collect_invoice_pdf_links(page, already_seen: set) -> tuple[list[dict], dict]:
    """
    Findet alle Invoice-Popover-URLs auf der Bestellübersicht, ruft jeden
    Popover-Endpoint auf und extrahiert PDF-Link, Bestelldatum und Kaufpreis.

    Gibt (neue_rechnungen, alle_metadaten) zurück. alle_metadaten enthält
    Datum/Preis für JEDE gesehene Order-ID (auch bereits verarbeitete),
    damit bereits heruntergeladene Dateien nachbenannt werden können.
    """
    import json
    from playwright.sync_api import TimeoutError as PwTimeout

    base = f"https://www.{AMAZON_DOMAIN}"
    results = []
    all_meta = {}
    seen_order_ids = set()

    while True:
        time.sleep(2)

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

            if order_id in seen_order_ids:
                continue
            seen_order_ids.add(order_id)

            try:
                meta = el.evaluate(_ORDER_META_JS)
            except Exception:
                meta = {"date": "", "total": ""}
            date_token, price_token = _order_meta_to_tokens(meta.get("date", ""), meta.get("total", ""))
            all_meta[order_id] = (date_token, price_token)

            if order_id in already_seen:
                print(f"  Überspringe {order_id} (bereits verarbeitet).")
                continue

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

            # ALLE PDF-Links im Popover suchen: bei Teillieferungen stellt
            # Amazon eine Rechnung PRO SENDUNG aus (jede entspricht einer
            # eigenen Kartenabbuchung) – nur die erste zu nehmen hat Belege
            # verschluckt.
            # 'generated_invoices' muss mit rein: Amazon liefert die erzeugte
            # Rechnung als signierte S3-URL (…/generated_invoices_v2/<uuid>.pdf),
            # die weder '/invoice' noch 'invoice.pdf' enthält und deshalb früher
            # als "kein PDF-Link" durchgefallen ist.
            pdf_urls = []
            for u in re.findall(r'href=["\']([^"\']*(?:invoice\.pdf|/invoice|generated_invoices)[^"\']*)["\']', pop_html):
                # href-Werte sind HTML-escaped ('&amp;'). Bei den signierten
                # S3-URLs zerlegt das die X-Amz-Parameter und macht die
                # Signatur ungültig – daher vor dem Aufruf zurückwandeln.
                u = html.unescape(u)
                full = base + u if u.startswith("/") else u
                if full not in pdf_urls:
                    pdf_urls.append(full)
            if not pdf_urls:
                # Alle Links ausgeben zur Diagnose
                all_hrefs = re.findall(r'href=["\']([^"\']+)["\']', pop_html)
                print(f"    Kein PDF-Link gefunden. Links im Popover: {all_hrefs}")
                continue

            print(f"  {len(pdf_urls)} PDF(s) gefunden für {order_id} ({date_token}, {price_token})")
            results.append({"order_id": order_id, "pdf_urls": pdf_urls, "date": date_token, "price": price_token})

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

    return results, all_meta


def _parse_amount_str(raw: str) -> Optional[float]:
    """Parst Beträge in deutschem ('3.234,80') wie englischem Format ('63.50')."""
    raw = raw.strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") == 1 and len(raw.split(".")[1]) == 3:
        raw = raw.replace(".", "")  # '1.234' = Tausendertrennung
    try:
        return float(raw)
    except ValueError:
        return None


_INVOICE_AMOUNT_PATTERNS = [
    r"(?:Zahlbetrag|Rechnungssumme)\s*([\d.,]+)\s*€",                     # Amazon
    r"Total aller Lieferungen und Leistungen\s+EUR\s+([\d.,]+)",          # Galaxus
    r"Final Price:\s*([\d.,]+)\s*€",                                      # Airalo
    r"Total Paid[^\d]*([\d.,]+)\s*€",                                     # Airalo (Kopfzeile)
]


def extract_invoice_amount(pdf_path: Path) -> Optional[float]:
    """Liest den Zahlbetrag aus einer Rechnungs-PDF (Amazon, Galaxus, Airalo …)."""
    import pdfplumber

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        for pattern in _INVOICE_AMOUNT_PATTERNS:
            m = re.search(pattern, text)
            if m:
                return _parse_amount_str(m.group(1))
    except Exception as e:
        print(f"    Betrag nicht lesbar ({pdf_path.name}): {e}")
    return None


def extract_invoice_date(pdf_path: Path) -> Optional[str]:
    """Liest das Rechnungsdatum (als YYYY-MM-DD) aus einer PDF, falls erkennbar."""
    import pdfplumber

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        m = re.search(r"(?:Rechnungsdatum|Bestell- / Rechnungsdatum)\s*(\d{2})\.(\d{2})\.(\d{4})", text)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        m = re.search(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})\b", text)
        if m:
            months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                      "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
            month = months.get(m.group(2))
            if month:
                return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
    except Exception:
        pass
    return None


def _download_pdf(page, context, pdf_url: str, order_id: str, date_token: str, price_token: str, part: int = 0) -> Optional[Path]:
    """Lädt eine direkte PDF-URL herunter. Der Betrag im Dateinamen kommt aus
    der PDF selbst (= tatsächliche Abbuchung pro Sendung); die Bestellsumme
    aus der Übersicht dient nur als Fallback."""
    try:
        # Direkt per HTTP mit Session-Cookies herunterladen
        response = context.request.get(pdf_url, timeout=30_000)
        if not response.ok:
            print(f"    HTTP-Fehler: {response.status}")
            return None
        tmp = DOWNLOAD_DIR / f".tmp_{order_id}_{part}.pdf"
        tmp.write_bytes(response.body())

        pdf_amount = extract_invoice_amount(tmp)
        token = f"{pdf_amount:.2f}EUR" if pdf_amount is not None else price_token
        suffix = f"-{part + 1}" if part else ""
        dest = DOWNLOAD_DIR / f"Amazon_Rechnung_{date_token}_{order_id}{suffix}_{token}.pdf"
        if dest.exists():
            tmp.unlink(missing_ok=True)
            print(f"    Existiert bereits: {dest.name}")
            return None
        tmp.rename(dest)
        print(f"    -> Gespeichert: {dest}")
        return dest
    except Exception as e:
        print(f"    Fehler beim Speichern: {e}")
        return None


def _parse_amazon_date(text: str) -> Optional[datetime]:
    months = {
        "Januar": 1, "Februar": 2, "März": 3, "April": 4,
        "Mai": 5, "Juni": 6, "Juli": 7, "August": 8,
        "September": 9, "Oktober": 10, "November": 11, "Dezember": 12,
        "January": 1, "February": 2, "March": 3,
        "May": 5, "June": 6, "July": 7,
        "October": 10, "November": 11, "December": 12,
    }
    for month_name, month_num in months.items():
        if month_name in text:
            parts = text.split()
            try:
                day = int(parts[0].replace(".", ""))
                year = int(parts[-1])
                return datetime(year, month_num, day)
            except (ValueError, IndexError):
                pass
    return None


_CURRENCY_CODES = {"€": "EUR", "$": "USD", "£": "GBP", "CHF": "CHF"}


def _price_to_filename_token(price_text: str) -> str:
    price_text = price_text.strip()
    if not price_text:
        return "unbekannt"
    currency = next((code for sym, code in _CURRENCY_CODES.items() if sym in price_text), "")
    raw_amount = re.sub(r"[^\d.,]", "", price_text).replace(",", ".")
    if not raw_amount:
        return "unbekannt"
    try:
        amount = f"{float(raw_amount):.2f}"
    except ValueError:
        amount = raw_amount
    return f"{amount}{currency}"


def _order_id_from_filename(filename: str) -> Optional[str]:
    m = re.match(r"Amazon_Rechnung_(\d{3}-\d+-\d+)\.pdf$", filename)
    return m.group(1) if m else None


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
                    # Datum + Betrag aus der PDF in den Dateinamen aufnehmen,
                    # damit der Moss-Abgleich per Betrag matchen kann.
                    amount = extract_invoice_amount(dest)
                    inv_date = extract_invoice_date(dest)
                    if amount is not None:
                        date_part = f"{inv_date}_" if inv_date else ""
                        new_dest = DOWNLOAD_DIR / f"Galaxus_Rechnung_{date_part}{order_id}_{amount:.2f}EUR.pdf"
                        if not new_dest.exists():
                            dest.rename(new_dest)
                            dest = new_dest
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


GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
GMAIL_LABEL = os.getenv("GMAIL_LABEL", "Orders")


def download_gmail_invoices() -> list[Path]:
    """PDF-Anhänge aus Gmail-Label herunterladen (IMAP + App-Passwort)."""
    import imaplib
    import email as _email
    import email.header
    import hashlib

    if not GMAIL_EMAIL or not GMAIL_APP_PASSWORD:
        print("Gmail: GMAIL_EMAIL und GMAIL_APP_PASSWORD nicht konfiguriert.")
        return []

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    downloaded: list[Path] = []
    already_seen = load_seen_order_ids()

    print(f"Gmail: Verbinde mit imap.gmail.com als {GMAIL_EMAIL}...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
    except Exception as e:
        print(f"Gmail: Login fehlgeschlagen: {e}")
        return []

    # Label auswählen (Gmail-Labels erscheinen als IMAP-Ordner)
    status, _ = mail.select(f'"{GMAIL_LABEL}"')
    if status != "OK":
        # Ohne Anführungszeichen versuchen
        status, _ = mail.select(GMAIL_LABEL)
    if status != "OK":
        print(f"Gmail: Label '{GMAIL_LABEL}' nicht gefunden.")
        mail.logout()
        return []

    _, msg_ids_raw = mail.search(None, "ALL")
    msg_ids = msg_ids_raw[0].split() if msg_ids_raw[0] else []
    print(f"Gmail: {len(msg_ids)} E-Mail(s) im Label '{GMAIL_LABEL}' gefunden.")

    for msg_id in msg_ids:
        _, msg_data = mail.fetch(msg_id, "(RFC822)")
        raw = msg_data[0][1]
        msg = _email.message_from_bytes(raw)

        # Deduplication per Message-ID
        message_id = msg.get("Message-ID", "").strip()
        if not message_id:
            message_id = msg.get("Subject", "") + msg.get("Date", "")
        dedup_key = f"email_{hashlib.md5(message_id.encode()).hexdigest()[:16]}"
        if dedup_key in already_seen:
            continue

        # Absender-Name für Dateinamen
        from_raw = msg.get("From", "Unknown")
        from_name = _email.header.decode_header(from_raw)[0][0]
        if isinstance(from_name, bytes):
            from_name = from_name.decode(errors="replace")
        from_name = re.sub(r'[<>"\']', '', from_name.split("@")[0]).strip()
        from_name = re.sub(r'\W+', '_', from_name)[:30]

        found_pdf = False
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get("Content-Disposition") is None:
                continue

            orig_filename = part.get_filename() or ""
            decoded = _email.header.decode_header(orig_filename)[0][0]
            if isinstance(decoded, bytes):
                decoded = decoded.decode(errors="replace")
            if not decoded.lower().endswith(".pdf"):
                continue

            # Keine AGB, Widerrufsformulare o.ä. – nur echte Rechnungen
            skip_keywords = ["agb", "widerrufs", "terms", "conditions", "datenschutz",
                             "privacy", "cancellation", "return_policy"]
            if any(kw in decoded.lower() for kw in skip_keywords):
                print(f"  Überspringe (kein Rechnungs-PDF): {decoded}")
                continue

            payload = part.get_payload(decode=True)
            if not payload or payload[:4] != b"%PDF":
                continue

            # Dateiname: Mail_Absender_Originalname[_Betrag].pdf – der Betrag
            # aus der PDF ermöglicht dem Moss-Abgleich das Matching.
            safe_orig = re.sub(r'\W+', '_', decoded.rsplit(".", 1)[0])[:40]
            dest = DOWNLOAD_DIR / f"Mail_{from_name}_{safe_orig}.pdf"
            counter = 1
            while dest.exists():
                dest = DOWNLOAD_DIR / f"Mail_{from_name}_{safe_orig}_{counter}.pdf"
                counter += 1

            dest.write_bytes(payload)
            amount = extract_invoice_amount(dest)
            if amount is not None:
                new_dest = dest.with_name(f"{dest.stem}_{amount:.2f}EUR.pdf")
                if not new_dest.exists():
                    dest.rename(new_dest)
                    dest = new_dest
            print(f"  -> {dest.name} ({len(payload) // 1024} KB)")
            downloaded.append(dest)
            found_pdf = True

        if found_pdf:
            save_seen_order_id(dedup_key)

    mail.logout()
    print(f"Gmail: {len(downloaded)} neue Rechnung(en) heruntergeladen.")
    return downloaded


# ── Rechnungs-PDFs: Produktbeschreibungen extrahieren ────────────────────────

_NON_PRODUCT_ROW_LABELS = {"versandkosten", "aktionsrabatt", "gutschein", "rabatt", "verpackung", "pfand"}


def extract_invoice_line_items(pdf_path: Path) -> list[str]:
    """Extrahiert Produktbeschreibungen aus einer Rechnungs-PDF
    (Amazon-Tabellenlayout, Galaxus- und Airalo-Textlayout)."""
    import pdfplumber

    items = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            # Amazon: Tabellenstruktur mit Beschreibung + Menge
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row or len(row) < 2:
                            continue
                        desc, qty = row[0], row[1]
                        if not desc or not qty or not qty.strip().isdigit():
                            continue
                        if desc.strip().lower() in _NON_PRODUCT_ROW_LABELS:
                            continue
                        desc_clean = re.sub(r"\n?ASIN:\S+", "", desc).replace("\n", " ").strip()
                        desc_clean = re.sub(r"\s+", " ", desc_clean)
                        if desc_clean:
                            items.append(desc_clean)

            if not items:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
                # Galaxus, Eigenverkauf (mit MwSt.-Spalte):
                #   "Produktname 24121911 19.05.2026 1 19,00% 336,13 399,99 ..."
                # Galaxus, Drittanbieter (Spalten "Menge Preis Betrag", KEINE
                # MwSt.-Spalte, Beschreibung mit Fußnotenmarke "1)"):
                #   "Apple TV 4K (3. Gen) ... 1) 22720374 18.08.2026 1 330,07 330,07"
                # Die Prozentspalte ist deshalb optional; danach muss aber ein
                # Betrag stehen, damit nicht jede Textzeile mit Zahlen greift.
                # In der Spalte "Versand" steht entweder ein Datum oder der
                # Status "nicht geliefert" (dessen zweites Wort in die nächste
                # Zeile umbricht) – beides muss die Position passieren lassen.
                galaxus_row = (r"^(.+?)\s+\d{6,}\s+(?:\d{2}\.\d{2}\.\d{4}|nicht)\s+\d+"
                               r"\s+(?:[\d,]+%\s+)?[\d.]*\d,\d{2}")
                for m in re.finditer(galaxus_row, text, re.MULTILINE):
                    desc = re.sub(r"\s+", " ", m.group(1)).strip()
                    # Fußnotenmarke der Drittanbieter-Positionen entfernen.
                    # Leerzeichen davor ist Pflicht und die Zahl kurz, sonst
                    # verstümmelt das Produktangaben wie '... (13.60", 16:10)'.
                    desc = re.sub(r"\s+\d{1,2}\)$", "", desc).strip()
                    if desc:
                        items.append(desc)
                # Airalo: "1 Europa - eSIM - Unbegrenzt - 30 Tage 63.50 € 63.50 €"
                if not items:
                    for m in re.finditer(r"^\d+\s+(.+?)\s+[\d.,]+\s*€\s+[\d.,]+\s*€\s*$", text, re.MULTILINE):
                        items.append(re.sub(r"\s+", " ", m.group(1)).strip())
    except Exception as e:
        print(f"    PDF-Textextraktion fehlgeschlagen ({pdf_path.name}): {e}")
    return items


# ── Moss: Abgleich offener Belege ────────────────────────────────────────────

MOSS_SENT_FILE = Path("moss_sent.txt")

_MOSS_ROW_JS = """
() => {
    const ids = new Set();
    document.querySelectorAll('[row-id]').forEach(r => ids.add(r.getAttribute('row-id')));
    return Array.from(ids).map(id => {
        const els = Array.from(document.querySelectorAll('[row-id="' + id + '"]'));
        const data = {rowId: id};
        els.forEach(el => {
            el.querySelectorAll('[col-id]').forEach(c => {
                data[c.getAttribute('col-id')] = c.textContent.trim();
            });
        });
        return data;
    });
}
"""

_MOSS_BUCHUNGSTEXT_PLACEHOLDER = "Gib zusätzliche Informationen zu dieser Ausgabe an"

_GERMAN_MONTHS_SHORT = {
    # Vollständige Namen und die von Moss verwendeten Abkürzungen (z.B. "Aug.")
    "Januar": 1, "Jan": 1,
    "Februar": 2, "Feb": 2,
    "März": 3, "Mär": 3,
    "April": 4, "Apr": 4,
    "Mai": 5,
    "Juni": 6, "Jun": 6,
    "Juli": 7, "Jul": 7,
    "August": 8, "Aug": 8,
    "September": 9, "Sept": 9, "Sep": 9,
    "Oktober": 10, "Okt": 10,
    "November": 11, "Nov": 11,
    "Dezember": 12, "Dez": 12,
}


def _parse_moss_date(text: str) -> Optional[datetime]:
    m = re.match(r"(\d{1,2})\.?\s+(\w+)\.?\s+(\d{4})", text.strip())
    if not m:
        return None
    day, month_name, year = m.groups()
    month = _GERMAN_MONTHS_SHORT.get(month_name.rstrip("."))
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day))
    except ValueError:
        return None


def _parse_moss_amount(text: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d,.]", "", text).replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_moss_open_rows(page) -> list[dict]:
    """Extrahiert alle Zeilen der aktuell geladenen 'Fehlende Belege'-Ansicht."""
    raw_rows = page.evaluate(_MOSS_ROW_JS)
    rows = []
    for raw in raw_rows:
        merchant_raw = raw.get("merchantSortName", "").strip()
        # Moss hängt diesen Hinweis an die Händlerspalte genau der Zeilen, denen
        # Pflichtangaben (u.a. der Buchungstext) fehlen. Früher wurde er nur
        # abgeschnitten – als Flag erspart er den Klick auf die Filterkachel.
        needs_fields = merchant_raw.endswith("Fehlende Pflichtangaben")
        merchant = re.sub(r"Fehlende Pflichtangaben$", "", merchant_raw).strip()
        amount = _parse_moss_amount(raw.get("homeAmount", ""))
        date = _parse_moss_date(raw.get("expenseMetadata.userTransactionTime", ""))
        receipt_status = raw.get("expenseMetadata.receiptStatus", "")
        if not merchant or amount is None:
            continue
        rows.append({
            "row_id": raw.get("rowId"),
            "merchant": merchant,
            "amount": amount,
            "date": date,
            "receipt_status": receipt_status,
            "needs_fields": needs_fields,
        })
    return rows


# Wie weit Rechnungsdatum und Moss-Buchungsdatum auseinanderliegen dürfen.
# Ohne diese Grenze genügte Betragsgleichheit: eine 23,99-€-Rechnung vom 17.08.
# wurde einer eigenständigen 23,99-€-Buchung vom 24.08. zugeordnet (7 Tage).
# 3 Tage, weil 9 von 10 nachgemessenen korrekten Zuordnungen 0–1 Tag
# auseinanderlagen. Preis dieser Enge: Rechnungen, die Amazon erst beim Versand
# ausstellt, können herausfallen (beobachtet: 15 Tage) – die stehen dann mit
# Abstand im Log und sind von Hand zu pflegen.
MOSS_MATCH_WINDOW_DAYS = 3


def _invoice_file_date(path: Path) -> Optional[datetime]:
    """Liest das Datum aus dem Dateinamen (…_YYYY-MM-DD_…)."""
    m = re.search(r"_(\d{4}-\d{2}-\d{2})_", path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None


def _find_local_invoice_for_amount(amount: float, tx_date: Optional[datetime]) -> Optional[Path]:
    """Sucht eine bereits heruntergeladene Rechnung mit passendem Betrag
    (±2 Cent Toleranz für Rundungsdifferenzen zwischen Rechnung und
    Kartenabbuchung) UND einem Datum innerhalb von MOSS_MATCH_WINDOW_DAYS um
    das Buchungsdatum. Bei mehreren Treffern gewinnt das nächste Datum.
    Ohne bekanntes Buchungsdatum ist das Fenster nicht prüfbar – dann
    entscheidet weiterhin allein der Betrag."""
    candidates = []
    for p in DOWNLOAD_DIR.glob("*.pdf"):
        m = re.search(r"_([\d.]+)EUR\.pdf$", p.name)
        if not m:
            continue
        try:
            file_amount = float(m.group(1))
        except ValueError:
            continue
        if abs(file_amount - amount) <= 0.02:
            candidates.append(p)
    if not candidates:
        return None
    if not tx_date:
        return candidates[0]

    in_window, rejected = [], []
    for p in candidates:
        file_date = _invoice_file_date(p)
        if file_date is None:
            # Ohne Datum im Namen nicht prüfbar – nicht stillschweigend nehmen.
            rejected.append((p, None))
            continue
        distance = abs((file_date - tx_date).days)
        if distance <= MOSS_MATCH_WINDOW_DAYS:
            in_window.append((p, distance))
        else:
            rejected.append((p, distance))

    if not in_window:
        for p, distance in rejected:
            abstand = "kein Datum im Dateinamen" if distance is None else f"{distance} Tage Abstand"
            print(f"    Betrag passt, Datum nicht: {p.name} ({abstand}, "
                  f"Fenster ±{MOSS_MATCH_WINDOW_DAYS} Tage) – nicht zugeordnet.")
        return None

    return min(in_window, key=lambda t: t[1])[0]


def _load_moss_sent() -> set:
    if MOSS_SENT_FILE.exists():
        return set(MOSS_SENT_FILE.read_text().splitlines())
    return set()


def _save_moss_sent(filename: str):
    with MOSS_SENT_FILE.open("a") as f:
        f.write(filename + "\n")


# ── Buchungstext-Freigabe: Ablage der offenen Vorschläge ─────────────────────

MOSS_PROPOSALS_FILE = Path("moss_proposals.json")


def _proposal_key(merchant: str, amount: float, date_str: str) -> str:
    """Stabile Identität einer Moss-Transaktion über Läufe hinweg.
    Bewusst NICHT die row-id: die kommt aus dem ag-Grid-DOM und gilt nur für
    die gerade geladene Ansicht, nicht für den nächsten Durchgang."""
    return f"{merchant.strip().lower()}|{amount:.2f}|{date_str}"


def _load_moss_proposals() -> dict:
    """Offene/erledigte Vorschläge, Slack-Message-ts -> Datensatz."""
    if MOSS_PROPOSALS_FILE.exists():
        try:
            return json.loads(MOSS_PROPOSALS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"Moss: {MOSS_PROPOSALS_FILE.name} unlesbar ({e}), starte mit leerem Stand.")
    return {}


def _save_moss_proposals(store: dict) -> None:
    MOSS_PROPOSALS_FILE.write_text(json.dumps(store, indent=2, ensure_ascii=False))


def _process_moss_transaction(page, row_id: str, pdf_path: Path, buchungstext: str) -> bool:
    """Öffnet die Transaktionszeile, lädt die Rechnung als Beleg hoch und
    trägt den Buchungstext ein (beides speichert automatisch)."""
    try:
        row = page.locator(f'[row-id="{row_id}"]').first
        row.click(timeout=10_000)

        file_input = page.locator('[data-file-upload-root="true"] input[type="file"]')
        file_input.wait_for(state="attached", timeout=10_000)
        file_input.set_input_files(str(pdf_path.resolve()))
        time.sleep(4)

        if buchungstext:
            textarea = page.locator(f'textarea[placeholder="{_MOSS_BUCHUNGSTEXT_PLACEHOLDER}"]')
            textarea.wait_for(state="visible", timeout=10_000)
            textarea.click()
            textarea.fill(buchungstext[:500])
            page.keyboard.press("Tab")
            time.sleep(2)

        close_btn = page.get_by_role("button", name="Schließen")
        if close_btn.count():
            close_btn.first.click()
            time.sleep(1)
        return True
    except Exception as e:
        print(f"    Fehler beim Bearbeiten der Transaktion: {e}")
        return False


def _fill_buchungstext_only(page, row_id: str, buchungstext: str) -> str:
    """Öffnet die Transaktionszeile und trägt nur den Buchungstext ein –
    aber ausschließlich, wenn das Feld noch leer ist (kein Überschreiben).
    Gibt "filled", "skip" (war schon gefüllt) oder "error" zurück."""
    try:
        row = page.locator(f'[row-id="{row_id}"]').first
        row.click(timeout=10_000)
        textarea = page.locator(f'textarea[placeholder="{_MOSS_BUCHUNGSTEXT_PLACEHOLDER}"]')
        textarea.wait_for(state="visible", timeout=10_000)
        if textarea.input_value().strip():
            result = "skip"
        else:
            textarea.click()
            textarea.fill(buchungstext[:500])
            page.keyboard.press("Tab")
            time.sleep(2)
            result = "filled"
        close_btn = page.get_by_role("button", name="Schließen")
        if close_btn.count():
            close_btn.first.click()
            time.sleep(1)
        return result
    except Exception as e:
        print(f"    Buchungstext-Fehler: {e}")
        return "error"


def _fill_missing_buchungstexte(page) -> list[dict]:
    """
    Zweite Abgleich-Phase: Transaktionen, deren Beleg schon vorhanden ist
    (z.B. weil Moss ihn selbst aus dem Dokumenteneingang/Slack-DM zugeordnet
    hat), denen aber der Buchungstext fehlt ('fehlende Informationen').
    Matcht per Betrag gegen lokale PDFs. Buchungstexte werden hier NIE
    automatisch eingetragen – für jeden Treffer wird nur ein Vorschlag
    gesammelt (Freigabe via Slack-Thread, siehe _post_buchungstext_proposals);
    eingetragen wird erst in apply_buchungstexte_from_slack.
    """
    print("Moss: Suche Transaktionen mit fehlenden Pflichtangaben...")
    page.goto("https://getmoss.com/app/transactions/all", wait_until="domcontentloaded", timeout=30_000)
    time.sleep(4)

    # Kein Klick auf die Kachel 'fehlende Informationen' mehr: der Selektor
    # darauf lief in einen Timeout und riss Phase 2 komplett mit. Die Zeilen
    # sind am Hinweis in der Händlerspalte erkennbar, also direkt filterbar.
    all_rows = _extract_moss_open_rows(page)
    rows = [r for r in all_rows if r["needs_fields"]]
    print(f"Moss: {len(rows)} von {len(all_rows)} Transaktion(en) mit fehlenden Pflichtangaben.")
    if all_rows and not rows:
        # Nicht stumm mit 0 Treffern rausgehen – dann wäre unklar, ob wirklich
        # nichts offen ist oder Moss die Beschriftung geändert hat.
        print("  Hinweis: keine Zeile trägt 'Fehlende Pflichtangaben'. Gelesene "
              f"Händlerwerte: {sorted({r['merchant'] for r in all_rows})[:10]}")

    proposals = []
    for row in rows:
        if not row.get("row_id"):
            continue
        match = _find_local_invoice_for_amount(row["amount"], row["date"])
        date_str = row["date"].strftime("%d.%m.%Y") if row["date"] else "?"
        if not match:
            print(f"  Kein lokaler Beleg für {row['merchant']} ({row['amount']:.2f} €, {date_str}) – Buchungstext bleibt offen.")
            continue
        line_items = extract_invoice_line_items(match)
        if not line_items:
            print(f"  Keine Produktzeilen extrahierbar aus {match.name}.")
            continue
        buchungstext = "; ".join(line_items)
        print(f"  Vorschlag (wartet auf Freigabe): {row['merchant']} ({row['amount']:.2f} €): {buchungstext[:60]}...")
        proposals.append({"merchant": row["merchant"], "amount": row["amount"],
                          "date": date_str, "text": buchungstext})

    print(f"Moss: {len(proposals)} Buchungstext-Vorschlag(e) gesammelt.")
    return proposals


def _slack_client_for_history():
    """Client zum Lesen von Thread-Antworten. Bevorzugt den Bot-Token (der Bot
    hat den Vorschlag selbst gepostet), fällt auf den User-Token zurück, falls
    dem Bot der History-Scope fehlt."""
    from slack_sdk import WebClient
    if SLACK_BOT_TOKEN:
        yield WebClient(token=SLACK_BOT_TOKEN)
    if SLACK_USER_TOKEN:
        yield WebClient(token=SLACK_USER_TOKEN)


def _post_buchungstext_proposals(proposals: list[dict]) -> None:
    """Postet jeden Buchungstext-Vorschlag als eigene Kanal-Nachricht.

    Bewusst eine Nachricht pro Transaktion statt einer Sammelliste:
    Slack-Threads sind zweistufig, Antworten hängen immer am Thread-Start und
    nie an einer einzelnen Nachricht innerhalb des Threads. Nur wenn jeder
    Vorschlag seinen eigenen Thread eröffnet, lässt sich eine Antwort
    eindeutig einer Transaktion zuordnen – siehe apply_buchungstexte_from_slack.
    """
    if not proposals:
        return
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        print("Moss: Kein Slack-Kanal konfiguriert, Vorschläge nur im Log.")
        return
    from slack_sdk import WebClient

    store = _load_moss_proposals()
    open_keys = {r["key"] for r in store.values() if r.get("status") == "open"}
    done_keys = {r["key"] for r in store.values() if r.get("status") == "applied"}

    fresh, seen = [], set()
    for prop in proposals:
        key = _proposal_key(prop["merchant"], prop["amount"], prop["date"])
        # Phase 1 (Beleg hochgeladen) und Phase 2 ('fehlende Informationen')
        # sehen dieselbe Transaktion – deshalb hier entdoppeln.
        if key in seen or key in open_keys or key in done_keys:
            continue
        seen.add(key)
        fresh.append((key, prop))

    skipped = len(proposals) - len(fresh)
    if not fresh:
        print(f"Moss: Keine neuen Vorschläge ({skipped} doppelt, schon offen oder erledigt).")
        return

    client = WebClient(token=SLACK_BOT_TOKEN)
    posted = 0
    for key, prop in fresh:
        text = (
            f"📝 *Buchungstext nötig* – *{prop['merchant']}* "
            f"{prop['amount']:.2f} € ({prop['date']})\n"
            f"Vorschlag: _{prop['text'][:300]}_\n"
            f"→ Antworte _in diesem Thread_ mit dem Buchungstext "
            f"(oder `ok`, um den Vorschlag zu übernehmen)."
        )
        try:
            resp = client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text)
        except Exception as e:
            print(f"Moss: Vorschlag {prop['merchant']} {prop['amount']:.2f} € fehlgeschlagen: {e}")
            continue
        store[resp["ts"]] = {
            "key": key,
            "channel": SLACK_CHANNEL_ID,
            "ts": resp["ts"],
            "merchant": prop["merchant"],
            "amount": prop["amount"],
            "date": prop["date"],
            "suggested": prop["text"],
            "status": "open",
            "posted_at": datetime.now().isoformat(timespec="seconds"),
        }
        posted += 1

    _save_moss_proposals(store)
    tail = f", {skipped} übersprungen (doppelt/offen/erledigt)." if skipped else "."
    print(f"Moss: {posted} Vorschlag/Vorschläge als eigene Threads gepostet{tail}")


_OK_REPLIES = {"ok", "okay", "passt", "ja", "jup", "yes", "übernehmen", "uebernehmen", "+1", "👍", "✅"}


def _clean_reply_text(msg: dict) -> str:
    """Antworttext säubern: Mentions, Aufzählungszeichen und Codeticks raus.
    Mehrzeilige Antworten werden zu einer Zeile zusammengezogen, weil Moss
    ein einzeiliges Buchungstext-Feld erwartet."""
    text = msg.get("text", "")
    text = re.sub(r"<@[^>]+>", "", text)
    text = re.sub(r"<(https?://[^|>]+)(\|[^>]*)?>", r"\1", text)
    text = text.replace("`", "").replace("*", "").replace("_", "")
    lines = [re.sub(r"^\s*[•\-\*\u2022\d]+[.)]?\s*", "", ln).strip()
             for ln in text.splitlines()]
    return "; ".join(ln for ln in lines if ln).strip()


def _read_thread_answer(client, channel: str, ts: str) -> Optional[str]:
    """Letzte menschliche Antwort in einem Vorschlags-Thread."""
    resp = client.conversations_replies(channel=channel, ts=ts, limit=50)
    msgs = resp.get("messages", [])
    answer = None
    for msg in msgs[1:]:  # msgs[0] ist der Vorschlag selbst
        if msg.get("bot_id") or msg.get("subtype") == "bot_message":
            continue
        cleaned = _clean_reply_text(msg)
        if cleaned:
            answer = cleaned  # spätere Antwort korrigiert eine frühere
    return answer


def apply_buchungstexte_from_slack(page) -> dict:
    """Trägt die im Slack-Thread freigegebenen Buchungstexte in Moss ein.

    Gegenstück zu _post_buchungstext_proposals: liest zu jedem offenen
    Vorschlag den zugehörigen Thread, nimmt die letzte menschliche Antwort und
    schreibt sie in das Buchungstext-Feld der Transaktion. Erwartet eine
    eingeloggte Moss-Page. Vorhandene Texte werden nicht überschrieben.
    """
    result = {"applied": 0, "no_answer": 0, "not_found": 0, "failed": 0}
    store = _load_moss_proposals()
    open_items = {ts: r for ts, r in store.items() if r.get("status") == "open"}
    if not open_items:
        print("Moss: Keine offenen Buchungstext-Vorschläge.")
        return result

    client = None
    answers = {}
    last_err = None
    for candidate in _slack_client_for_history():
        try:
            answers = {}
            for ts, rec in open_items.items():
                ans = _read_thread_answer(candidate, rec["channel"], ts)
                if ans:
                    answers[ts] = ans
            client = candidate
            break
        except Exception as e:
            last_err = e
            continue
    if client is None:
        # Slack nennt den fehlenden Scope selbst – den ausgeben statt zu raten.
        # Private Kanäle brauchen 'groups:history', öffentliche 'channels:history'.
        needed = None
        resp = getattr(last_err, "response", None)
        if resp is not None:
            needed = (resp.data or {}).get("needed")
        hint = (f" Dem Token fehlt der Scope '{needed}' – in der Slack-App unter "
                f"'OAuth & Permissions' ergänzen und die App neu installieren."
                if needed else "")
        print(f"Moss: Thread-Antworten nicht lesbar ({last_err}).{hint}")
        result["failed"] = len(open_items)
        return result

    result["no_answer"] = len(open_items) - len(answers)
    if not answers:
        print(f"Moss: {len(open_items)} Vorschlag/Vorschläge offen, noch keine Antwort im Thread.")
        return result

    print(f"Moss: {len(answers)} beantwortete(r) Vorschlag/Vorschläge – trage in Moss ein...")
    page.goto("https://getmoss.com/app/transactions/all",
              wait_until="domcontentloaded", timeout=30_000)
    time.sleep(4)
    rows = {_proposal_key(r["merchant"], r["amount"],
                          r["date"].strftime("%d.%m.%Y") if r["date"] else "?"): r
            for r in _extract_moss_open_rows(page)}

    for ts, answer in answers.items():
        rec = store[ts]
        text = rec["suggested"] if answer.strip().lower() in _OK_REPLIES else answer
        if not text.strip():
            # Kommt vor, wenn der Vorschlag als falsch zurückgezogen wurde und
            # trotzdem mit 'ok' bestätigt wird – niemals leer eintragen.
            print(f"  Kein Text für {rec['merchant']} {rec['amount']:.2f} € "
                  f"(Antwort '{answer.strip()[:20]}' ohne Vorschlag) – bleibt offen.")
            result["failed"] += 1
            continue
        row = rows.get(rec["key"])
        if not row or not row.get("row_id"):
            print(f"  Transaktion nicht in der Liste: {rec['merchant']} "
                  f"{rec['amount']:.2f} € ({rec['date']}) – bleibt offen.")
            result["not_found"] += 1
            continue

        outcome = _fill_buchungstext_only(page, row["row_id"], text)
        if outcome == "error":
            print(f"  Eintragen fehlgeschlagen: {rec['merchant']} {rec['amount']:.2f} €")
            result["failed"] += 1
            continue

        rec["status"] = "applied"
        rec["applied_text"] = text
        rec["applied_at"] = datetime.now().isoformat(timespec="seconds")
        rec["outcome"] = outcome
        result["applied"] += 1
        note = ("✅ Buchungstext in Moss eingetragen: "
                if outcome == "filled" else
                "ℹ️ Feld war in Moss schon gefüllt, unverändert gelassen: ")
        print(f"  {note}{rec['merchant']} {rec['amount']:.2f} € -> {text[:60]}")
        try:
            client.chat_postMessage(channel=rec["channel"], thread_ts=ts,
                                    text=f"{note}_{text[:300]}_")
            client.reactions_add(channel=rec["channel"], timestamp=ts, name="white_check_mark")
        except Exception as e:
            print(f"    Slack-Bestätigung fehlgeschlagen: {e}")

    _save_moss_proposals(store)
    print(f"Moss-Buchungstexte: {result['applied']} eingetragen, "
          f"{result['no_answer']} ohne Antwort, {result['not_found']} nicht gefunden, "
          f"{result['failed']} fehlgeschlagen.")
    return result


def sync_moss_open_invoices(page) -> dict:
    """
    Phase 1: Ruft die Moss-Ansicht 'Fehlende Belege' auf, gleicht offene Posten
    gegen bereits heruntergeladene Rechnungen (Betrag) ab, lädt Treffer direkt
    als Beleg hoch und trägt den Buchungstext (aus den Produktbeschreibungen
    der PDF) ein.
    Phase 2: Trägt Buchungstexte für Transaktionen nach, deren Beleg Moss
    bereits selbst zugeordnet hat (Ansicht 'fehlende Informationen').
    Erwartet eine bereits eingeloggte Playwright-Page (z.B. aus einer per CDP
    verbundenen, sichtbaren Session-Setup-Session).
    """
    # Zuerst die bereits im Slack-Thread freigegebenen Buchungstexte
    # eintragen: hier ist garantiert eine eingeloggte Moss-Page vorhanden,
    # was für den 15-Minuten-Listener nicht immer gilt.
    try:
        applied = apply_buchungstexte_from_slack(page)
    except Exception as e:
        print(f"Moss: Buchungstext-Übernahme fehlgeschlagen: {e}")
        applied = {"applied": 0}

    now = datetime.now()
    date_from = (now - timedelta(days=365)).strftime("%Y-%m-%dT00:00:00.000Z")
    date_to = (now + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59.999Z")
    url = (
        "https://getmoss.com/app/transactions/all"
        f"?receiptAggregatedStatuses[]=RECEIPT_MISSING"
        f"&userTransactionTimeFrom={date_from}"
        f"&userTransactionTimeTo={date_to}"
    )
    print(f"Moss: Öffne offene Belege ({url})...")
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(4)

    rows = _extract_moss_open_rows(page)
    print(f"Moss: {len(rows)} offene(r) Beleg(e) gefunden.")

    already_done = _load_moss_sent()
    done, already, unmatched = [], [], []
    phase1_proposals = []

    for row in rows:
        match = _find_local_invoice_for_amount(row["amount"], row["date"])
        date_str = row["date"].strftime("%d.%m.%Y") if row["date"] else "?"
        if not match:
            print(f"  Kein lokaler Beleg für {row['merchant']} ({row['amount']:.2f} €, {date_str}).")
            unmatched.append(row)
            continue
        if match.name in already_done:
            already.append(row)
            continue
        if not row.get("row_id"):
            unmatched.append(row)
            continue

        line_items = extract_invoice_line_items(match)
        buchungstext = "; ".join(line_items)
        # Beleg hochladen, aber Buchungstext NIE automatisch setzen –
        # stattdessen Vorschlag sammeln und zur Freigabe an Patrick schicken.
        print(f"  Lade {match.name} als Beleg hoch (Buchungstext wartet auf Freigabe).")
        if _process_moss_transaction(page, row["row_id"], match, ""):
            _save_moss_sent(match.name)
            # Auch im laufenden Durchgang sperren, damit dieselbe PDF nicht
            # zwei betragsgleichen Transaktionen zugeordnet wird.
            already_done.add(match.name)
            done.append(row)
            if buchungstext:
                date_str = row["date"].strftime("%d.%m.%Y") if row["date"] else "?"
                phase1_proposals.append({"merchant": row["merchant"], "amount": row["amount"],
                                         "date": date_str, "text": buchungstext})
        else:
            unmatched.append(row)

    phase2_proposals = _fill_missing_buchungstexte(page)
    proposals = phase1_proposals + phase2_proposals
    # Entdoppelt und postet nur noch nicht offene/erledigte Vorschläge.
    _post_buchungstext_proposals(proposals)

    print(f"Moss-Abgleich: {len(done)} hochgeladen, {applied.get('applied', 0)} Buchungstext(e) aus Slack eingetragen, {len(proposals)} Vorschlag/Vorschläge geprüft, {len(already)} bereits erledigt, {len(unmatched)} ohne lokalen Beleg.")
    return {"sent": done, "already_sent": already, "unmatched": unmatched,
            "texts_filled": applied.get("applied", 0), "proposals": proposals}


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
        files += download_gmail_invoices()

    post_to_slack(files)


if __name__ == "__main__":
    main()
