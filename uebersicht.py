"""Daten für die Übersicht „Das braucht dich" und die Laufansicht.

Bewusst ohne Flask und ohne Playwright: reine Funktionen über dem Protokoll
eines Laufs, den Buchungstext-Vorschlägen und dem Stand des letzten
Moss-Abgleichs. So bleiben sie ohne Browser und ohne Netz testbar.
"""
import re
from datetime import datetime

# Reihenfolge wie in main.sync_moss_open_invoices. Jeder Schritt beginnt mit
# der ersten Protokollzeile, die seinen Marker enthält; Schritt 1 mit dem Start.
MOSS_SCHRITTE = (
    ("texte", "Buchungstexte eintragen", None),
    ("liste", "Moss-Liste lesen", "Moss: Öffne offene Belege"),
    ("belege", "Belege hochladen", ("Transaktion(en) ohne Beleg", "Spalte 'Beleg' nicht lesbar")),
    ("pflicht", "Pflichtangaben prüfen", "Suche Transaktionen mit fehlenden Pflichtangaben"),
)
MOSS_ENDE = ("Moss-Abgleich:", "✅ Moss-Abgleich", "❌")
MOSS_KENNUNG = ("=== Moss-Abgleich", "Moss: Öffne offene Belege")

_ZEILE_BETRAG = re.compile(r"für (?P<haendler>.+?) \((?P<betrag>[\d.,]+) €, (?P<datum>[\d.?]+)\)")


def _enthaelt(zeile: str, marker) -> bool:
    if marker is None:
        return False
    if isinstance(marker, str):
        marker = (marker,)
    return any(m in zeile for m in marker)


def _zahl(muster: str, zeilen) -> "int | None":
    for z in zeilen:
        m = re.search(muster, z)
        if m:
            return int(m.group(1))
    return None


def fmt_eur(betrag) -> str:
    try:
        wert = float(betrag)
    except (TypeError, ValueError):
        return ""
    return f"{wert:,.2f}".replace(",", "~").replace(".", ",").replace("~", ".") + " €"


def lauf_fortschritt(log: list, running: bool) -> dict:
    """Leitet aus den Protokollzeilen ab, wo der Lauf steht."""
    log = [z for z in (log or []) if z and z.strip()]
    ereignisse = [z.strip() for z in log if not z.startswith("===")][-6:][::-1]
    if not any(_enthaelt(z, MOSS_KENNUNG) for z in log):
        return {"art": "sonst", "titel": "Rechnungen werden geholt",
                "schritte": [], "zaehler": [], "ereignisse": ereignisse}

    # Startindex je Schritt; ein Schritt ohne Marker im Protokoll hat noch nicht begonnen.
    starts = [0]
    for _, _, marker in MOSS_SCHRITTE[1:]:
        idx = next((i for i, z in enumerate(log) if _enthaelt(z, marker)), None)
        starts.append(idx)
    ende = next((i for i, z in enumerate(log) if _enthaelt(z, MOSS_ENDE)), None)
    fertig = ende is not None or not running

    abschnitte = []
    for i, start in enumerate(starts):
        if start is None:
            abschnitte.append([])
            continue
        spaetere = [s for s in starts[i + 1:] if s is not None]
        bis = spaetere[0] if spaetere else (ende if ende is not None else len(log))
        abschnitte.append(log[start:bis])

    aktiv = max(i for i, s in enumerate(starts) if s is not None)
    texte, liste, belege, pflicht = abschnitte

    eingetragen = _zahl(r"Moss-Buchungstexte: (\d+) eingetragen", texte)
    offen = _zahl(r"Moss: (\d+) Vorschlag/Vorschläge offen", texte)
    gesamt_fehlend = _zahl(r"Moss: (\d+) Transaktion\(en\) über", liste)
    seiten = _zahl(r"über (\d+) Tabellenseite", liste)
    ohne_beleg = _zahl(r"Moss: (\d+) Transaktion\(en\) ohne Beleg", belege)
    hochgeladen = sum(1 for z in belege if z.strip().startswith("Lade "))
    gesperrt = sum(1 for z in belege if "In Moss gesperrt" in z)
    ohne_rechnung = sum(1 for z in belege if "Kein lokaler Beleg für" in z)
    pflicht_m = next((re.search(r"Moss: (\d+) von (\d+) Transaktion", z) for z in pflicht
                      if re.search(r"Moss: (\d+) von (\d+) Transaktion", z)), None)
    vorschlaege = _zahl(r"Moss: (\d+) Buchungstext-Vorschlag", pflicht)

    ergebnisse = [
        ("Keine offenen Vorschläge." if any("Keine offenen Buchungstext" in z for z in texte)
         else " · ".join(t for t in (
             f"{eingetragen} eingetragen" if eingetragen is not None else "",
             f"{offen} warten noch auf deine Antwort in Slack" if offen is not None else "",
         ) if t) or "Liest deine Antworten aus den Slack-Threads."),
        (f"{gesamt_fehlend} Transaktionen über {seiten} Tabellenseite(n) erfasst."
         if gesamt_fehlend is not None and seiten is not None
         else "Blättert durch die Transaktionsliste in Moss."),
        (f"{ohne_beleg} Transaktionen ohne Beleg: {hochgeladen - gesperrt} hochgeladen, "
         f"{ohne_rechnung} ohne passende Rechnung" + (f", {gesperrt} in Moss gesperrt." if gesperrt else ".")
         if ohne_beleg is not None else "Sucht zu jeder Transaktion die passende Rechnung im Ordner."),
        (f"{pflicht_m.group(1)} von {pflicht_m.group(2)} Transaktionen mit fehlenden Pflichtangaben"
         + (f" · {vorschlaege} Vorschlag{'' if vorschlaege == 1 else 'e'} gesammelt." if vorschlaege is not None else ".")
         if pflicht_m else "Prüft, wo Buchungstext oder andere Pflichtangaben fehlen."),
    ]

    schritte = []
    for i, (kennung, titel, _) in enumerate(MOSS_SCHRITTE):
        if starts[i] is None:
            zustand = "wartet"
        elif i < aktiv or fertig:
            zustand = "fertig"
        else:
            zustand = "aktiv"
        schritt = {"kennung": kennung, "titel": titel, "zustand": zustand,
                   "ergebnis": ergebnisse[i] if zustand != "wartet" else "Startet, sobald der Schritt davor durch ist."}
        if kennung == "belege" and zustand == "aktiv" and ohne_beleg:
            bearbeitet = min(ohne_beleg, hochgeladen + ohne_rechnung)
            schritt["fortschritt"] = {"ist": bearbeitet, "von": ohne_beleg}
            letzte = next((_ZEILE_BETRAG.search(z) for z in reversed(belege) if _ZEILE_BETRAG.search(z)), None)
            if letzte:
                schritt["aktuell"] = {"haendler": letzte.group("haendler"),
                                      "betrag": letzte.group("betrag") + " €",
                                      "datum": letzte.group("datum")}
        schritte.append(schritt)

    zaehler = [
        ("Buchungstexte eingetragen", eingetragen),
        ("Transaktionen ohne Beleg", ohne_beleg),
        ("Belege hochgeladen", (hochgeladen - gesperrt) if ohne_beleg is not None else None),
        ("Ohne passende Rechnung", ohne_rechnung if ohne_beleg is not None else None),
        ("In Moss gesperrt", gesperrt if ohne_beleg is not None else None),
        ("Transaktionen geprüft", int(pflicht_m.group(2)) if pflicht_m else None),
    ]
    return {
        "art": "moss", "titel": "Abgleich mit Moss läuft",
        "schritt_nr": aktiv + 1, "schritt_anzahl": len(MOSS_SCHRITTE),
        "schritte": schritte,
        "zaehler": [{"name": n, "wert": w} for n, w in zaehler if w is not None],
        "ereignisse": ereignisse,
    }


def _zeile(rec: dict) -> dict:
    return {"haendler": rec.get("merchant") or "", "datum": rec.get("date") or "",
            "betrag": fmt_eur(rec.get("amount"))}


def _datum_sortierbar(text: str) -> str:
    try:
        return datetime.strptime(text, "%d.%m.%Y").strftime("%Y%m%d")
    except (TypeError, ValueError):
        return ""


def thread_link(slack_url: str, channel: str, ts: str) -> str:
    """Dauerlink auf die Slack-Nachricht; ohne bekannte Workspace-Adresse der Kanal."""
    if not (channel and ts):
        return ""
    if slack_url:
        return f"{slack_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
    return f"https://slack.com/app_redirect?channel={channel}"


def baue_uebersicht(vorschlaege: dict, stand: "dict | None", slack_url: str = "") -> dict:
    """Was Patrick gerade selbst tun muss, plus der Stand in Moss."""
    vorschlaege = vorschlaege or {}
    offen, gesperrt, erledigt = [], [], []
    eingetragen = 0
    for ts, rec in vorschlaege.items():
        status = rec.get("status")
        if status == "open":
            offen.append({**_zeile(rec), "link": thread_link(slack_url, rec.get("channel"), rec.get("ts") or ts)})
        elif (status == "verfallen" and rec.get("verfallen_grund") == "gesperrt"
              and not rec.get("von_hand_erledigt_am")):
            gesperrt.append({**_zeile(rec), "ts": rec.get("ts") or ts, "text": rec.get("suggested") or ""})
        elif status == "applied" and rec.get("outcome") == "filled":
            eingetragen += 1
            erledigt.append({"text": f"Buchungstext eingetragen: {rec.get('merchant', '')}, {fmt_eur(rec.get('amount'))}",
                             "wann": rec.get("applied_at") or ""})
    for liste in (offen, gesperrt):
        liste.sort(key=lambda r: _datum_sortierbar(r["datum"]), reverse=True)
    erledigt.sort(key=lambda r: r["wann"], reverse=True)

    stand = stand or {}
    ohne_beleg = [{**_zeile(r), "hinweis": r.get("grund") or ""} for r in stand.get("ohne_beleg") or []]
    ohne_beleg.sort(key=lambda r: _datum_sortierbar(r["datum"]), reverse=True)

    gesamt = stand.get("gesamt")
    balken = []
    if gesamt:
        fehlend = stand.get("ohne_beleg_anzahl")
        if fehlend is not None:
            balken.append({"name": "Belege angehängt", "ist": max(0, gesamt - fehlend), "von": gesamt})
        pflicht = stand.get("pflicht_offen")
        if pflicht is not None:
            balken.append({"name": "Pflichtangaben vollständig", "ist": max(0, gesamt - pflicht), "von": gesamt})

    hinweise = []
    if stand.get("ohne_produktzeilen"):
        n = stand["ohne_produktzeilen"]
        hinweise.append(f"Aus {n} Rechnung{'en' if n != 1 else ''} ließen sich keine Produktzeilen lesen – "
                        "dafür gibt es keinen Buchungstext-Vorschlag.")

    return {
        "anzahl": len(offen) + len(gesperrt) + len(ohne_beleg),
        "offen": offen, "gesperrt": gesperrt, "ohne_beleg": ohne_beleg,
        "stand": {"zeit": stand.get("zeit"), "gesamt": gesamt, "balken": balken,
                  "texte_eingetragen": eingetragen},
        "hinweise": hinweise,
        "erledigt": erledigt[:4],
    }
