"""Moss liefert Transaktionen seitenweise (gemessen: 50 von 324 pro Seite).
Diese Tests sichern die Blätterschleife ab – ohne Browser."""
import main


def _zeile(rid, betrag=1.0):
    return {"row_id": rid, "merchant": "Amazon", "amount": betrag}


def test_zusammenfuehren_behaelt_reihenfolge_und_entfernt_dubletten():
    seiten = [[_zeile("a"), _zeile("b")], [_zeile("b"), _zeile("c")]]
    assert [r["row_id"] for r in main._zeilen_zusammenfuehren(seiten)] == ["a", "b", "c"]


def test_zusammenfuehren_ueberspringt_zeilen_ohne_id():
    assert main._zeilen_zusammenfuehren([[{"row_id": None}, _zeile("a")]]) == [_zeile("a")]


def test_zusammenfuehren_leer():
    assert main._zeilen_zusammenfuehren([]) == []


def _praepariere(monkeypatch, seiten):
    """Ersetzt alle Browserzugriffe: jede 'Seite' ist eine Zeilenliste."""
    stand = {"seite": 0, "geblaettert": 0}
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "_moss_seitengroesse_setzen", lambda page, g=100: True)
    monkeypatch.setattr(main, "_extract_moss_open_rows", lambda page: seiten[stand["seite"]])

    def blaettern(page, richtung="Next"):
        if stand["seite"] + 1 >= len(seiten):
            return False
        stand["seite"] += 1
        stand["geblaettert"] += 1
        return True

    monkeypatch.setattr(main, "_moss_blaettern", blaettern)
    return stand


def test_alle_zeilen_liest_jede_seite(monkeypatch):
    seiten = [[_zeile("a"), _zeile("b")], [_zeile("c")], [_zeile("d")]]
    stand = _praepariere(monkeypatch, seiten)
    zeilen = main._moss_alle_zeilen(object())
    assert [r["row_id"] for r in zeilen] == ["a", "b", "c", "d"]
    assert stand["geblaettert"] == 2


def test_alle_zeilen_einzelne_seite(monkeypatch):
    _praepariere(monkeypatch, [[_zeile("a")]])
    assert len(main._moss_alle_zeilen(object())) == 1


def test_alle_zeilen_bricht_bei_endlosem_blaettern_ab(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "_moss_seitengroesse_setzen", lambda page, g=100: True)
    monkeypatch.setattr(main, "_extract_moss_open_rows", lambda page: [_zeile("a")])
    aufrufe = []
    monkeypatch.setattr(main, "_moss_blaettern", lambda page, r="Next": aufrufe.append(1) or True)
    zeilen = main._moss_alle_zeilen(object())
    assert len(zeilen) == 1
    assert len(aufrufe) == main.MOSS_MAX_PAGES
