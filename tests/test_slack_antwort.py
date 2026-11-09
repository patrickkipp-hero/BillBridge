"""Tests für das Säubern der Slack-Thread-Antworten.

Was hier herauskommt, landet wörtlich im Buchungstext-Feld in Moss.
Zwei Fehler sind so entstanden: Slack liefert '&' maskiert zurück ('P&amp;C'),
und die Aufzählungs-Erkennung hat führende Mengenangaben gefressen
('2 Monitore für Berlin' → 'Monitore für Berlin').
"""
import main


def clean(text):
    return main._clean_reply_text({"text": text})


def test_html_entitaeten_werden_aufgeloest():
    """Slack maskiert &, < und > in der API-Antwort."""
    assert clean("P&amp;C Büro Hannover") == "P&C Büro Hannover"
    assert clean("Preis &lt; 100 &amp; Versand &gt; 0") == "Preis < 100 & Versand > 0"


def test_mention_wird_entfernt():
    assert clean("<@U012AB3CD> Office Berlin") == "Office Berlin"


def test_link_behaelt_nur_die_url():
    assert clean("Beleg <https://example.com/x|hier>") == "Beleg https://example.com/x"


def test_markup_wird_entfernt():
    assert clean("*Office* `Berlin`") == "Office Berlin"


def test_mehrere_zeilen_werden_zusammengezogen():
    """Moss hat ein einzeiliges Feld."""
    assert clean("Monitor\nDockingstation") == "Monitor; Dockingstation"


def test_aufzaehlungszeichen_werden_entfernt():
    assert clean("- Monitor\n- Dock") == "Monitor; Dock"
    assert clean("• Monitor\n• Dock") == "Monitor; Dock"
    assert clean("1) Monitor\n2) Dock") == "Monitor; Dock"
    assert clean("1. Monitor\n2. Dock") == "Monitor; Dock"


def test_fuehrende_mengenangabe_bleibt_erhalten():
    """Regression: Ziffern zählen nur mit Punkt oder Klammer als Listenmarke."""
    assert clean("2 Monitore für Berlin") == "2 Monitore für Berlin"
    assert clean("3x Dock für HO") == "3x Dock für HO"
    assert clean("13 Zoll Hülle") == "13 Zoll Hülle"
    assert clean("1.5 m Kabel") == "1.5 m Kabel"


def test_leere_antwort_ergibt_leeren_text():
    assert clean("") == ""
    assert clean("   \n  ") == ""
    assert clean("<@U012AB3CD>") == ""


def test_fehlendes_textfeld_wirft_nicht():
    assert main._clean_reply_text({}) == ""
