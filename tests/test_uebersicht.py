"""Übersicht „Das braucht dich" und Laufansicht: reine Funktionen in uebersicht.py."""
import uebersicht as u

MOSS_LOG = [
    "=== Moss-Abgleich gestartet ===",
    "Moss-Buchungstexte: 2 eingetragen",
    "Moss: 3 Vorschlag/Vorschläge offen",
    "Moss: Öffne offene Belege",
    "Moss: 324 Transaktion(en) über 7 Tabellenseite(n)",
    "Moss: 4 Transaktion(en) ohne Beleg",
    "Lade Beleg hoch für Amazon (161,38 €, 14.10.2026)",
    "Kein lokaler Beleg für Galaxus Deu (2.986,00 €, 18.09.2026)",
    "Suche Transaktionen mit fehlenden Pflichtangaben",
    "Moss: 5 von 324 Transaktionen ohne Pflichtangaben",
    "✅ Moss-Abgleich abgeschlossen",
]


def zustaende(f):
    return [s["zustand"] for s in f["schritte"]]


def test_fmt_eur():
    assert u.fmt_eur(2986) == "2.986,00 €"
    assert u.fmt_eur("64.34") == "64,34 €"
    assert u.fmt_eur(None) == ""
    assert u.fmt_eur("abc") == ""


def test_lauf_ohne_moss_ist_sonst():
    f = u.lauf_fortschritt(["Starte Amazon", "", "2 neue Rechnungen"], True)
    assert f["art"] == "sonst"
    assert f["schritte"] == []
    assert f["ereignisse"] == ["2 neue Rechnungen", "Starte Amazon"]   # neueste zuerst


def test_lauf_leeres_protokoll():
    assert u.lauf_fortschritt(None, False)["art"] == "sonst"


def test_moss_erster_schritt_aktiv():
    f = u.lauf_fortschritt(MOSS_LOG[:3], True)
    assert f["art"] == "moss"
    assert f["schritt_nr"] == 1 and f["schritt_anzahl"] == 4
    assert zustaende(f) == ["aktiv", "wartet", "wartet", "wartet"]
    assert "2 eingetragen" in f["schritte"][0]["ergebnis"]


def test_moss_belege_mit_fortschritt_und_aktueller_zeile():
    f = u.lauf_fortschritt(MOSS_LOG[:8], True)
    assert zustaende(f) == ["fertig", "fertig", "aktiv", "wartet"]
    belege = f["schritte"][2]
    assert belege["fortschritt"] == {"ist": 2, "von": 4}
    assert belege["aktuell"] == {"haendler": "Galaxus Deu", "betrag": "2.986,00 €", "datum": "18.09.2026"}
    zaehler = {z["name"]: z["wert"] for z in f["zaehler"]}
    assert zaehler["Belege hochgeladen"] == 1
    assert zaehler["Ohne passende Rechnung"] == 1


def test_moss_abgeschlossen_alles_fertig():
    f = u.lauf_fortschritt(MOSS_LOG, True)
    assert zustaende(f) == ["fertig"] * 4
    assert "fortschritt" not in f["schritte"][2]
    assert "5 von 324" in f["schritte"][3]["ergebnis"]


def test_moss_nicht_mehr_laufend_gilt_als_fertig():
    assert zustaende(u.lauf_fortschritt(MOSS_LOG[:5], False)) == ["fertig", "fertig", "wartet", "wartet"]


def test_thread_link():
    assert u.thread_link("https://x.slack.com/", "C1", "17.5") == "https://x.slack.com/archives/C1/p175"
    assert u.thread_link("", "C1", "17.5") == "https://slack.com/app_redirect?channel=C1"
    assert u.thread_link("https://x.slack.com", "", "17.5") == ""


VORSCHLAEGE = {
    "1.1": {"status": "open", "merchant": "Amazon", "amount": 10, "date": "01.09.2026", "channel": "C1"},
    "2.2": {"status": "open", "merchant": "Galaxus", "amount": 20, "date": "18.09.2026", "channel": "C1"},
    "3.3": {"status": "verfallen", "verfallen_grund": "gesperrt", "merchant": "Amazon",
            "amount": 5, "date": "02.09.2026", "suggested": "USB-Kabel"},
    "4.4": {"status": "verfallen", "verfallen_grund": "gesperrt", "merchant": "Amazon",
            "amount": 6, "date": "03.09.2026", "von_hand_erledigt_am": "2026-11-09T22:00:00"},
    "5.5": {"status": "verfallen", "verfallen_grund": "alt", "merchant": "Amazon", "amount": 7},
    "6.6": {"status": "applied", "outcome": "filled", "merchant": "Amazon", "amount": 8,
            "applied_at": "2026-11-08T20:30:00"},
}


def test_uebersicht_sortiert_und_filtert():
    d = u.baue_uebersicht(VORSCHLAEGE, None, "https://x.slack.com")
    assert [z["haendler"] for z in d["offen"]] == ["Galaxus", "Amazon"]      # neuestes Datum zuerst
    assert d["offen"][0]["link"] == "https://x.slack.com/archives/C1/p22"
    assert [(z["ts"], z["text"]) for z in d["gesperrt"]] == [("3.3", "USB-Kabel")]   # von Hand erledigte fehlen
    assert d["anzahl"] == 3
    assert d["stand"]["texte_eingetragen"] == 1
    assert d["erledigt"][0]["wann"] == "2026-11-08T20:30:00"


def test_uebersicht_ohne_stand_erfindet_nichts():
    d = u.baue_uebersicht({}, None)
    assert d["anzahl"] == 0 and d["ohne_beleg"] == [] and d["hinweise"] == []
    assert d["stand"] == {"zeit": None, "gesamt": None, "balken": [], "texte_eingetragen": 0}


def test_uebersicht_mit_stand():
    stand = {"zeit": "2026-11-09T20:31:00", "gesamt": 324, "ohne_beleg_anzahl": 4, "pflicht_offen": 24,
             "ohne_produktzeilen": 1,
             "ohne_beleg": [{"merchant": "Hetzner", "amount": 12.5, "date": "05.10.2026", "grund": "keine Rechnung im Ordner"}]}
    d = u.baue_uebersicht({}, stand)
    assert d["stand"]["balken"] == [{"name": "Belege angehängt", "ist": 320, "von": 324},
                                    {"name": "Pflichtangaben vollständig", "ist": 300, "von": 324}]
    assert d["ohne_beleg"] == [{"haendler": "Hetzner", "datum": "05.10.2026", "betrag": "12,50 €",
                                "hinweis": "keine Rechnung im Ordner"}]
    assert d["anzahl"] == 1
    assert len(d["hinweise"]) == 1 and "1 Rechnung " in d["hinweise"][0]
