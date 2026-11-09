"""Tests für das Protokoll und seine Kürzung.

Das Protokoll wuchs unbegrenzt (2,8 MB nach wenigen Wochen). Gekürzt wird
bewusst in der Datei selbst statt durch Umbenennen: launchd hält stdout und
stderr des Dienstes mit O_APPEND auf genau dieser Datei offen. Nach einem
Umbenennen schriebe launchd in die umbenannte Datei weiter – deshalb prüft
test_kuerzen_behaelt_dieselbe_datei genau das.
"""
import os

import pytest

import app


@pytest.fixture
def log(tmp_path, monkeypatch):
    pfad = tmp_path / "billbridge.log"
    monkeypatch.setattr(app, "LOG_FILE", pfad)
    monkeypatch.setattr(app, "LOG_MAX_BYTES", 4096)
    monkeypatch.setattr(app, "LOG_KEEP_BYTES", 1024)
    return pfad


def fuellen(pfad, bytes_, zeilenlaenge=60):
    """Füllt das Protokoll mit erkennbar nummerierten Zeilen."""
    with pfad.open("a") as f:
        nr = 0
        while pfad.stat().st_size < bytes_:
            f.write(f"Zeile {nr:06d} " + "x" * zeilenlaenge + "\n")
            f.flush()
            nr += 1
    return nr


def test_eintrag_bekommt_genau_einen_zeilenumbruch(log):
    app.schreibe_log("erste Zeile")
    app.schreibe_log("zweite Zeile\n")   # Aufrufer bringt schon einen mit

    assert log.read_text() == "erste Zeile\nzweite Zeile\n"


def test_mehrzeiliger_block_bleibt_mehrzeilig(log):
    """Die Ausgabe eines Unterprozesses geht als ein Block ins Protokoll."""
    app.schreibe_log("Kopf:\nZeile A\nZeile B")

    assert log.read_text() == "Kopf:\nZeile A\nZeile B\n"


def test_kleines_protokoll_wird_nicht_gekuerzt(log):
    app.schreibe_log("harmlos")

    assert log.read_text() == "harmlos\n"
    assert "gekürzt" not in log.read_text()


def test_grosses_protokoll_wird_gekuerzt(log):
    fuellen(log, app.LOG_MAX_BYTES + 2000)

    app.schreibe_log("Eintrag nach dem Überlauf")

    inhalt = log.read_text()
    assert log.stat().st_size <= app.LOG_KEEP_BYTES + 200
    assert inhalt.startswith("=== Protokoll gekürzt ")
    # Das Ende bleibt erhalten, der Anfang fällt weg.
    assert "Eintrag nach dem Überlauf" in inhalt
    assert "Zeile 000000" not in inhalt


def test_gekuerztes_protokoll_beginnt_an_einer_zeilengrenze(log):
    """Sonst startet das Protokoll mitten in einem Wort."""
    fuellen(log, app.LOG_MAX_BYTES + 2000)

    app.schreibe_log("neu")

    for zeile in log.read_text().splitlines()[1:]:
        assert zeile.startswith(("Zeile ", "neu")), zeile


def test_kuerzen_behaelt_dieselbe_datei(log):
    """Kernpunkt: launchd schreibt mit O_APPEND in genau diese Datei. Würde
    gekürzt, indem umbenannt und neu angelegt wird, schriebe launchd in die
    alte Datei weiter und das Protokoll liefe auseinander."""
    fuellen(log, app.LOG_MAX_BYTES + 2000)
    inode_vorher = log.stat().st_ino

    app.schreibe_log("nach dem Kürzen")

    assert log.stat().st_ino == inode_vorher
    # Und auch keine Sicherungskopien daneben, die niemand aufräumt.
    assert [p.name for p in log.parent.iterdir()] == [log.name]


def test_anhaengen_nach_dem_kuerzen_funktioniert_weiter(log):
    fuellen(log, app.LOG_MAX_BYTES + 2000)
    app.schreibe_log("direkt nach dem Kürzen")

    app.schreibe_log("und noch einer")

    assert log.read_text().endswith("direkt nach dem Kürzen\nund noch einer\n")


def test_nicht_schreibbares_protokoll_wirft_nicht(tmp_path, monkeypatch):
    """Ein kaputtes Protokoll darf keinen Lauf abbrechen."""
    monkeypatch.setattr(app, "LOG_FILE", tmp_path / "gibtsnicht" / "billbridge.log")

    app.schreibe_log("egal")  # darf nicht werfen


def test_leerer_eintrag_erzeugt_keine_leerzeile(log):
    """Ein Prozess, der nichts ausgegeben hat, soll nichts hinterlassen."""
    app.schreibe_log("echter Eintrag")
    app.schreibe_log("")
    app.schreibe_log("   \n  ")

    assert log.read_text() == "echter Eintrag\n"


# ── Flask-Zugriffsprotokoll ──────────────────────────────────────────────────

class FakeRecord:
    def __init__(self, text):
        self._text = text

    def getMessage(self):
        return self._text


def test_erfolgreiche_abfragen_werden_gefiltert():
    """Die Oberfläche fragt /api/status im Sekundentakt ab."""
    f = app._NurAuffaelligeZugriffe()

    assert not f.filter(FakeRecord('127.0.0.1 - - [09/Nov/2026 10:26:00] "GET /api/status HTTP/1.1" 200 -'))
    assert not f.filter(FakeRecord('127.0.0.1 - - [09/Nov/2026 10:26:00] "GET / HTTP/1.1" 304 -'))


def test_fehler_bleiben_im_protokoll():
    f = app._NurAuffaelligeZugriffe()

    assert f.filter(FakeRecord('127.0.0.1 - - [09/Nov/2026 10:26:00] "GET /weg HTTP/1.1" 404 -'))
    assert f.filter(FakeRecord('127.0.0.1 - - [09/Nov/2026 10:26:00] "POST /api/run HTTP/1.1" 500 -'))


def test_andere_werkzeug_meldungen_bleiben():
    """Startmeldungen und Tracebacks sind kein Zugriffsprotokoll."""
    f = app._NurAuffaelligeZugriffe()

    assert f.filter(FakeRecord(" * Running on http://127.0.0.1:5757"))
