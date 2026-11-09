"""Tests für die Zuordnung Rechnung → Moss-Transaktion.

Diese Logik hat zweimal falsche Belege zugeordnet: einmal über ein zu enges
Datumsfenster, einmal über eine Betragstoleranz, die ähnliche Rechnungen
durchgehen ließ (Rückmeldung aus der Buchhaltung, Oktober 2026). Beide Regeln
sind hier festgeschrieben.
"""
from datetime import datetime

import main


def _rechnung(tmp_path, datum: str, betrag: str, order: str = "302-1234567-1234567"):
    """Legt eine leere PDF mit dem Namensschema von BillBridge an."""
    pfad = tmp_path / f"Amazon_Rechnung_{datum}_{order}_{betrag}EUR.pdf"
    pfad.write_bytes(b"%PDF-1.4\n")
    return pfad


def test_gleicher_betrag_im_fenster_wird_zugeordnet(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    rechnung = _rechnung(tmp_path, "2026-10-29", "184.55")

    treffer = main._find_local_invoice_for_amount(184.55, datetime(2026, 11, 4))

    assert treffer == rechnung


def test_ein_cent_daneben_wird_nicht_zugeordnet(tmp_path, monkeypatch):
    """Der Fall aus der Buchhaltung: 0 Cent Toleranz heißt wirklich 0."""
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    _rechnung(tmp_path, "2026-10-29", "184.56")

    assert main._find_local_invoice_for_amount(184.55, datetime(2026, 11, 4)) is None


def test_krumme_betraege_werden_in_cent_verglichen(tmp_path, monkeypatch):
    """0.1 + 0.2 != 0.3 – der Vergleich muss in ganzen Cent laufen."""
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    rechnung = _rechnung(tmp_path, "2026-11-04", "342.02")

    assert main._find_local_invoice_for_amount(342.02, datetime(2026, 11, 4)) == rechnung


def test_betrag_passt_datum_nicht(tmp_path, monkeypatch):
    """Moss bucht auf den Abrechnungstag – außerhalb des Fensters ist es
    trotzdem eine fremde Rechnung."""
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    _rechnung(tmp_path, "2026-08-01", "99.00")

    assert main._find_local_invoice_for_amount(99.00, datetime(2026, 11, 4)) is None


def test_fenstergrenze_ist_eingeschlossen(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main, "MOSS_MATCH_WINDOW_DAYS", 14)
    gerade_noch = _rechnung(tmp_path, "2026-10-21", "50.00")

    assert main._find_local_invoice_for_amount(50.00, datetime(2026, 11, 4)) == gerade_noch


def test_naechstes_datum_gewinnt(tmp_path, monkeypatch):
    """Zwei betragsgleiche Rechnungen im Fenster: die näher am Buchungstag."""
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    fern = _rechnung(tmp_path, "2026-10-25", "65.99", order="302-1111111-1111111")
    nah = _rechnung(tmp_path, "2026-11-03", "65.99", order="302-2222222-2222222")

    treffer = main._find_local_invoice_for_amount(65.99, datetime(2026, 11, 4))

    assert treffer == nah
    assert treffer != fern


def test_ohne_datum_im_dateinamen_nicht_stillschweigend_nehmen(tmp_path, monkeypatch):
    """Alte Dateien ohne Datum sind nicht prüfbar – lieber nichts zuordnen."""
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    (tmp_path / "Amazon_Rechnung_302-9999999-9999999_77.00EUR.pdf").write_bytes(b"%PDF-1.4\n")

    assert main._find_local_invoice_for_amount(77.00, datetime(2026, 11, 4)) is None


def test_ohne_buchungsdatum_entscheidet_der_betrag(tmp_path, monkeypatch):
    """Konnte Moss kein Datum liefern, bleibt nur der Betrag."""
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    rechnung = _rechnung(tmp_path, "2026-01-02", "12.97")

    assert main._find_local_invoice_for_amount(12.97, None) == rechnung


def test_leerer_ordner_ergibt_keinen_treffer(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)

    assert main._find_local_invoice_for_amount(10.00, datetime(2026, 11, 4)) is None
