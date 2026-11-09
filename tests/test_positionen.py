"""Tests für die Produktpositionen, aus denen der Buchungstext entsteht.

Die Erkennung ist zweimal gebrochen:
* Der Fußnoten-Entferner der Galaxus-Drittanbieter-Positionen hat
  Produktangaben wie '(13.60", 16:10)' verstümmelt.
* Drittanbieter-Rechnungen ohne MwSt.-Spalte lieferten gar keine Positionen
  (Fall Galaxus 342,02 €).
Die Zeilen unten stammen aus echten Rechnungen dieser beiden Fälle.
"""
import main


# ── Amazon: Tabellenlayout ───────────────────────────────────────────────────

def test_amazon_zeile_ohne_asin_und_umbrueche():
    table = [
        ["Beschreibung", "Menge", "Preis", "MwSt."],
        ["18 Blöcke Haftnotizen 76 mm x 76 mm, Klebezettel zum Beschriften | Leicht\n"
         "zu Beschreiben, Wiederaufklebbar, Super Sticky Notes für Schule, Zuhause,\n"
         "Büro\nASIN:B0DQLBR13G", "1", "7,97 €", "19.0%"],
    ]

    assert main._line_items_from_table(table) == [
        "18 Blöcke Haftnotizen 76 mm x 76 mm, Klebezettel zum Beschriften | Leicht "
        "zu Beschreiben, Wiederaufklebbar, Super Sticky Notes für Schule, Zuhause, Büro"
    ]


def test_amazon_summenzeile_ohne_menge_wird_ignoriert():
    """Die Summenzeile hat keine Menge – sonst stünde '19.0%' im Buchungstext."""
    assert main._line_items_from_table([["", "19.0%", "45,74 €", "8,69 €"]]) == []


def test_amazon_versandkosten_sind_kein_produkt():
    table = [["Versandkosten", "1", "4,99 €"], ["Tastatur", "1", "29,99 €"]]

    assert main._line_items_from_table(table) == ["Tastatur"]


def test_amazon_leere_tabelle():
    assert main._line_items_from_table([]) == []
    assert main._line_items_from_table(None) == []


# ── Galaxus: Textlayout ──────────────────────────────────────────────────────

def test_galaxus_eigenverkauf_mit_mwst_spalte():
    text = ('Lenovo ThinkVision T27hv-30 (2560 x 1440 Pixel, 27") '
            '24121911 19.05.2026 1 19,00% 336,13 399,99 336,13 399,99')

    assert main._line_items_from_text(text) == [
        'Lenovo ThinkVision T27hv-30 (2560 x 1440 Pixel, 27")'
    ]


def test_galaxus_drittanbieter_ohne_mwst_spalte():
    """Fall 342,02 € – zwei Positionen, Spalten ohne MwSt., mit Fußnotenmarke."""
    text = ("Apple TV 4K (3. Gen) + Ethernet (128 GB, tvOS 16) 1) 22720374 18.08.2026 1 330,07 330,07\n"
            "Primewire HDMI – HDMI (1 m) 2) 37557262 18.08.2026 1 11,95 11,95")

    assert main._line_items_from_text(text) == [
        "Apple TV 4K (3. Gen) + Ethernet (128 GB, tvOS 16)",
        "Primewire HDMI – HDMI (1 m)",
    ]


def test_galaxus_klammer_mit_seitenverhaeltnis_bleibt_heil():
    """Regression: '16:10)' darf der Fußnoten-Entferner nicht abschneiden."""
    text = ('Hyper Privacy Filter for MacBook Air 13.6" (13.60", 16:10) '
            '47529858 05.06.2026 1 19,00% 48,98 58,29 48,98 58,29')

    assert main._line_items_from_text(text) == [
        'Hyper Privacy Filter for MacBook Air 13.6" (13.60", 16:10)'
    ]


def test_galaxus_nicht_gelieferte_position_zaehlt_mit():
    """In der Spalte 'Versand' steht statt eines Datums 'nicht geliefert',
    dessen zweites Wort umbricht."""
    text = ("Apple Power Adapter (70 W) 36094011 nicht 1 19,00% 45,21 53,80 45,21 53,80\n"
            "geliefert")

    assert main._line_items_from_text(text) == ["Apple Power Adapter (70 W)"]


def test_fliesstext_erzeugt_keine_positionen():
    """Adressblock und Zahlungshinweise enthalten auch Zahlen."""
    text = ("Rechnung 188615843 vom 19.05.2026\n"
            "Zahlbar innerhalb 30 Tagen ohne Abzug\n"
            "Musterstraße 12, 30159 Hannover\n")

    assert main._line_items_from_text(text) == []


# ── Airalo: Textlayout ───────────────────────────────────────────────────────

def test_airalo_zeile():
    text = "1 Europa - eSIM - Unbegrenzt - 30 Tage 63.50 € 63.50 €"

    assert main._line_items_from_text(text) == ["Europa - eSIM - Unbegrenzt - 30 Tage"]


def test_airalo_greift_nur_wenn_galaxus_nichts_fand():
    """Beide Muster am selben Text: Galaxus gewinnt, Airalo bleibt stumm."""
    text = ('Lenovo ThinkVision T27hv-30 (2560 x 1440 Pixel, 27") '
            '24121911 19.05.2026 1 19,00% 336,13 399,99 336,13 399,99\n'
            '1 Europa - eSIM - Unbegrenzt - 30 Tage 63.50 € 63.50 €')

    assert main._line_items_from_text(text) == [
        'Lenovo ThinkVision T27hv-30 (2560 x 1440 Pixel, 27")'
    ]
