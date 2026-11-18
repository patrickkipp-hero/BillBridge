"""Rückmeldung aus der Buchhaltung, November 2026:

1. Nur Rechnungen dürfen als Beleg hochgeladen werden – keine Lieferscheine
   oder Bestellbestätigungen. Merkmal: die Rechnungsnummer.
2. Bei gleichen Beträgen wurde dieselbe Rechnung mehrfach hochgeladen.
3. Findet sich nichts Passendes, muss eine Warnung kommen.
"""
from datetime import datetime

import belegpruefung as bp
import main

AMAZON_RECHNUNG = """Rechnung
Zahlbetrag 161,38 €
Verkauft von Amazon EU S.à r.l.
Rechnungsdatum 14.10.2026
Rechnungsnummer DE2ABC123XYZ
Bestellnummer 302-1234567-1234567"""

AMAZON_LIEFERSCHEIN = """Elektronischer Lieferschein
Bestelldatum: 14. Oktober 2026 Lieferadresse:
Auftragsnummer: Patrick Kipp
Bestellnummer: 302-8092502-6646728
Versanddatum: 14. Oktober 2026 Deutschland"""

GALAXUS = """Galaxus Deutschland GmbH www.galaxus.de
Kaufbeleg / Rechnung Auftrag 188615843
Bestell- / Rechnungsdatum 19.05.2026 Hero Software GmbH
Fälligkeitsdatum
Rechnungsnummer
MwSt-Nr./USt-ID DE312684999"""


# ── 1. Nur Rechnungen ────────────────────────────────────────────────────────

def test_rechnung_mit_rechnungsnummer_gilt():
    assert bp.pruefe_text(AMAZON_RECHNUNG, "Amazon_Rechnung_2026-10-14_302-1_161.38EUR.pdf") == (True, "")


def test_rechnungsnummer_als_tabellenkopf_reicht():
    """Galaxus: Beschriftung und Wert stehen in verschiedenen Zeilen."""
    assert bp.pruefe_text(GALAXUS)[0] is True


def test_amazon_lieferschein_ist_keine_rechnung():
    """Stand als Amazon_Rechnung_… im Ordner und wurde in Moss hochgeladen."""
    assert bp.pruefe_text(AMAZON_LIEFERSCHEIN, "Amazon_Rechnung_2026-10-14_302-8092502-6646728_32.28EUR.pdf") \
        == (False, "Lieferschein")


def test_dokument_ohne_rechnungsnummer_ist_keine_rechnung():
    assert bp.pruefe_text("Bestellung 4711\nVielen Dank für Ihren Einkauf\nSumme 12,00 €") \
        == (False, "keine Rechnungsnummer")


def test_bestellbestaetigung_trotz_rechnungsnummer_im_text():
    text = "Bestellbestätigung\nIhre Rechnungsnummer teilen wir Ihnen mit der Rechnung mit."
    assert bp.pruefe_text(text) == (False, "Bestellbestätigung")


def test_dateiname_verraet_den_lieferschein():
    assert bp.pruefe_text("VoIPDistri.com\nRechnungsnummer 30341", "Mail_VoIP_Lieferschein_30341_001.pdf") \
        == (False, "Lieferschein")


def test_belegnummer_zaehlt_nur_auf_einer_rechnung():
    assert bp.pruefe_text("Original RECHNUNG\nBelegnummer Belegdatum\n1078640 23.06.2026")[0] is True
    assert bp.pruefe_text("Warenausgang\nBelegnummer 1078640")[0] is False


def test_leere_oder_unlesbare_pdf_ist_keine_rechnung(tmp_path):
    assert bp.pruefe_text("") == (False, "kein lesbarer Text")
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"kein pdf")
    assert bp.pruefe_pdf(kaputt) == (False, "PDF nicht lesbar")


def test_slack_bekommt_keine_nicht_rechnungen(tmp_path, monkeypatch, capsys):
    """post_to_slack ist die letzte Schranke vor der Moss-App."""
    lieferschein = tmp_path / "Mail_X_Lieferschein_1.pdf"
    lieferschein.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(main, "_ist_rechnung", lambda pfad: (False, "Lieferschein"))

    main.post_to_slack([lieferschein])      # würde beim Versand an der Attrappe scheitern

    ausgabe = capsys.readouterr().out
    assert "keine Rechnung (Lieferschein)" in ausgabe
    assert "Keine Rechnungen zum Hochladen" in ausgabe


def test_abgleich_nimmt_keinen_lieferschein(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    (tmp_path / "Amazon_Rechnung_2026-10-14_302-1_32.28EUR.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(main, "_ist_rechnung", lambda pfad: (False, "Lieferschein"))
    gruende = []

    assert main._find_local_invoice_for_amount(32.28, datetime(2026, 10, 15), gruende=gruende) is None
    assert gruende == ["Dokument im Ordner ist keine Rechnung (Lieferschein)"]


# ── 2. Eine Rechnung, eine Transaktion ───────────────────────────────────────

def _rechnung(ordner, datum, betrag, order, inhalt=b"%PDF-1.4\n"):
    pfad = ordner / f"Amazon_Rechnung_{datum}_{order}_{betrag}EUR.pdf"
    pfad.write_bytes(inhalt)
    return pfad


def test_vergebene_rechnung_wird_nicht_noch_einmal_zugeordnet(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    schon_weg = _rechnung(tmp_path, "2026-10-07", "179.65", "302-1")
    gruende = []

    treffer = main._find_local_invoice_for_amount(179.65, datetime(2026, 10, 8),
                                                  vergeben=lambda p: p == schon_weg, gruende=gruende)

    assert treffer is None
    assert gruende == ["passende Rechnung hängt schon an einer anderen Transaktion"]


def test_zweite_betragsgleiche_transaktion_bekommt_die_andere_rechnung(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    erste = _rechnung(tmp_path, "2026-10-07", "179.65", "302-1", b"%PDF-1.4\nA")
    zweite = _rechnung(tmp_path, "2026-10-01", "179.65", "302-2", b"%PDF-1.4\nB")

    treffer = main._find_local_invoice_for_amount(179.65, datetime(2026, 10, 8),
                                                  vergeben=lambda p: p == erste)

    assert treffer == zweite


def test_gleicher_inhalt_unter_anderem_namen_gilt_als_vergeben(tmp_path, monkeypatch):
    """Mail-Anhänge lagen doppelt im Ordner (…_1.pdf) – dieselbe Rechnung."""
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main, "MOSS_SENT_FILE", tmp_path / "moss_sent.txt")
    monkeypatch.setattr(main, "MOSS_ZUORDNUNG_FILE", tmp_path / "moss_zuordnung.json")
    original = _rechnung(tmp_path, "2026-10-07", "50.00", "302-1", b"%PDF-1.4\ngleich")
    kopie = _rechnung(tmp_path, "2026-10-07", "50.00", "302-1_1", b"%PDF-1.4\ngleich")
    main._save_moss_sent(original.name)             # Altbestand: nur der Dateiname

    namen, kennungen = main._vergebene_belege()

    assert original.name in namen
    assert kennungen[bp.inhalt_kennung(kopie)] is None


def test_zuordnung_merkt_sich_die_transaktion(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main, "MOSS_SENT_FILE", tmp_path / "moss_sent.txt")
    monkeypatch.setattr(main, "MOSS_ZUORDNUNG_FILE", tmp_path / "moss_zuordnung.json")
    rechnung = _rechnung(tmp_path, "2026-10-07", "50.00", "302-1")

    main._merke_zuordnung(rechnung, {"row_id": "abc", "merchant": "Amazon", "amount": 50.0,
                                     "date": datetime(2026, 10, 8)})

    _, kennungen = main._vergebene_belege()
    assert kennungen[bp.inhalt_kennung(rechnung)] == "abc"


# ── 3. Warnung bei Handarbeit ────────────────────────────────────────────────

def _zeile(row_id, tage_alt, grund=""):
    return {"row_id": row_id, "merchant": "Amazon", "amount": 161.38,
            "date": datetime(2026, 11, 17) - __import__("datetime").timedelta(days=tage_alt), "_grund": grund}


def test_nur_neue_und_nicht_zu_frische_transaktionen_werden_gemeldet():
    jetzt = datetime(2026, 11, 17)
    zeilen = [_zeile("alt", 10), _zeile("frisch", 1), _zeile("schon", 10)]

    neu = main._neue_handarbeit(zeilen, {"schon": "2026-11-10T20:30:00"}, jetzt)

    assert [r["row_id"] for r in neu] == ["alt"]


def test_warntext_nennt_transaktion_und_grund():
    text = main._handarbeit_text([_zeile("a", 10, "passende Rechnung hängt schon an einer anderen Transaktion")])

    assert "1 Transaktion braucht dich" in text
    assert "07.11.2026 · Amazon · 161,38 €" in text
    assert "hängt schon an einer anderen Transaktion" in text


def test_warnung_geht_einmal_raus_und_dann_nicht_wieder(tmp_path, monkeypatch):
    gesendet = []

    class Client:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, channel, text):
            gesendet.append((channel, text))

    import slack_sdk
    monkeypatch.setattr(slack_sdk, "WebClient", Client)
    monkeypatch.setattr(main, "SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_ALERT_CHANNEL_ID", "C123")
    monkeypatch.setattr(main, "MOSS_GEMELDET_FILE", tmp_path / "moss_gemeldet.json")
    zeilen = [_zeile("a", 30), _zeile("b", 30)]

    assert main.melde_handarbeit(zeilen) == 2
    assert main.melde_handarbeit(zeilen) == 0

    assert len(gesendet) == 1 and gesendet[0][0] == "C123"
    assert "2 Transaktionen brauchen dich" in gesendet[0][1]
