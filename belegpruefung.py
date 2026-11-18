"""Prüft, ob eine PDF wirklich eine Rechnung ist.

Finance nimmt als Beleg nur Rechnungen an – keine Lieferscheine,
Bestellbestätigungen, Preislisten oder Vertragsunterlagen. Das sichere
Merkmal: Nur eine Rechnung führt eine Rechnungsnummer. Bewusst ohne
Playwright und ohne Slack, damit die Regeln ohne Netz testbar bleiben.
"""
import hashlib
import re
from pathlib import Path

# Feld, das nur auf Rechnungen steht. Es reicht die Beschriftung: In
# Tabellen-Layouts (Galaxus, Nuki) steht der Wert in einer anderen Zeile.
_NUMMERNFELD = re.compile(
    r"(?i)\b(rechnungs\s*-?\s*(?:nummer|nr)\b|rechnung\s+nr\b|rechnungsnr\b"
    r"|invoice\s*(?:number|no\b|nr\b|#|id\b)|faktura\s*-?\s*(?:nummer|nr)\b"
    r"|belegnummer|numer\s+faktury|num[eé]ro\s+de\s+facture|factuurnummer)"
)
# Belegnummer allein tragen auch Lieferscheine – sie zählt nur, wenn sich das
# Dokument selbst Rechnung nennt.
_NENNT_SICH_RECHNUNG = re.compile(r"(?i)\b(rechnung|invoice|faktura|facture|factuur)\b")

# Wie sich das Dokument im Kopf oder im Dateinamen selbst nennt.
_KEINE_RECHNUNG = (
    ("Lieferschein", r"lieferschein|delivery note|packing slip"),
    ("Bestellbestätigung", r"bestellbest[aä]tigung|order confirmation|bestell[uü]bersicht"),
    ("Auftragsbestätigung", r"auftragsbest[aä]tigung"),
    ("Versandbestätigung", r"versandbest[aä]tigung|shipping confirmation"),
    ("Angebot", r"angebot|quotation"),
    ("Vertragsunterlage", r"leistungsbeschreibung|vertragszusammenfassung|legitimationsnachweis"
                          r"|preisliste|preisinformationen"),
    ("Anleitung", r"installation instructions|installationsanleitung"),
)
_KOPFZEILEN = 12


def pruefe_text(text: str, dateiname: str = "") -> tuple:
    """Gibt (ist_rechnung, grund) zurück; grund ist bei einer Rechnung leer."""
    text = text or ""
    if not text.strip():
        return False, "kein lesbarer Text"
    kopf = "\n".join(text.splitlines()[:_KOPFZEILEN])
    name = re.sub(r"[_\W]+", " ", dateiname or "")
    # Unsere eigenen Dateinamen heißen immer "…_Rechnung_…" – das sagt nichts.
    name = re.sub(r"(?i)^(amazon|galaxus) rechnung ", "", name)
    for art, muster in _KEINE_RECHNUNG:
        if re.search(rf"(?i)\b(?:{muster})\b", kopf) or re.search(rf"(?i)\b(?:{muster})", name):
            return False, art
    feld = _NUMMERNFELD.search(text)
    if not feld:
        return False, "keine Rechnungsnummer"
    if feld.group(1).lower() == "belegnummer" and not _NENNT_SICH_RECHNUNG.search(text):
        return False, "keine Rechnungsnummer"
    return True, ""


def pruefe_pdf(pfad: Path) -> tuple:
    """Wie pruefe_text, liest die PDF selbst. Unlesbare Dateien sind keine Rechnung."""
    try:
        import pdfplumber
        with pdfplumber.open(str(pfad)) as pdf:
            text = "\n".join(seite.extract_text() or "" for seite in pdf.pages[:5])
    except Exception:
        return False, "PDF nicht lesbar"
    return pruefe_text(text, Path(pfad).name)


def inhalt_kennung(pfad: Path) -> str:
    """Fingerabdruck des Dateiinhalts: dieselbe Rechnung unter anderem Namen bleibt dieselbe."""
    try:
        return hashlib.sha256(Path(pfad).read_bytes()).hexdigest()
    except OSError:
        return ""
