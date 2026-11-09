"""Tests für den Lebenszyklus der Buchungstext-Vorschläge.

Vorschläge blieben für immer "open". Beantwortete, deren Transaktion Moss
nicht mehr anzeigt, wurden alle 15 Minuten erneut versucht – und zählten in
jeder Meldung als "offen" mit, obwohl nichts mehr zu tun war. Die Fristen hier
sind bewusst lang: Eine Transaktion kann vorübergehend aus der Ansicht fallen
(Filter, virtualisierte Tabelle, laufender Export), und ein vorschnell
abgelegter Vorschlag verliert eine Antwort, die jemand geschrieben hat.
"""
from datetime import datetime, timedelta

import main


JETZT = datetime(2026, 11, 9, 12, 0, 0)


def vorschlag(**felder):
    rec = {"channel": "C-TEST", "merchant": "Amazon", "amount": 42.39,
           "date": "08.09.2026", "suggested": "Office Berlin", "status": "open",
           "posted_at": JETZT.isoformat(timespec="seconds")}
    rec.update(felder)
    return rec


def vor_tagen(tage, stunden=0):
    return (JETZT - timedelta(days=tage, hours=stunden)).isoformat(timespec="seconds")


# ── Fristen ──────────────────────────────────────────────────────────────────

def test_frisch_vermisste_transaktion_bleibt_offen():
    rec = vorschlag(nicht_gefunden_seit=vor_tagen(1))

    assert main._proposal_give_up_reason("1", rec, beantwortet=True, jetzt=JETZT) is None


def test_lange_vermisste_transaktion_wird_abgelegt():
    rec = vorschlag(nicht_gefunden_seit=vor_tagen(main.MOSS_PROPOSAL_MISSING_DAYS))

    assert main._proposal_give_up_reason("1", rec, beantwortet=True,
                                         jetzt=JETZT) == "nicht_auffindbar"


def test_einen_tag_vor_der_frist_noch_nicht():
    rec = vorschlag(nicht_gefunden_seit=vor_tagen(main.MOSS_PROPOSAL_MISSING_DAYS - 1,
                                                  stunden=23))

    assert main._proposal_give_up_reason("1", rec, beantwortet=True, jetzt=JETZT) is None


def test_unbeantworteter_vorschlag_verfaellt_nach_frist():
    rec = vorschlag(posted_at=vor_tagen(main.MOSS_PROPOSAL_NO_ANSWER_DAYS))

    assert main._proposal_give_up_reason("1", rec, beantwortet=False,
                                         jetzt=JETZT) == "ohne_antwort"


def test_unbeantworteter_vorschlag_vor_der_frist_bleibt():
    rec = vorschlag(posted_at=vor_tagen(main.MOSS_PROPOSAL_NO_ANSWER_DAYS - 1))

    assert main._proposal_give_up_reason("1", rec, beantwortet=False, jetzt=JETZT) is None


def test_beantworteter_vorschlag_verfaellt_nicht_durch_alter():
    """Wer geantwortet hat, soll seine Antwort nicht wegen Zeitablaufs
    verlieren – nur eine endgültig verschwundene Transaktion beendet ihn."""
    rec = vorschlag(posted_at=vor_tagen(365))

    assert main._proposal_give_up_reason("1", rec, beantwortet=True, jetzt=JETZT) is None


def test_vermisstfrist_schlaegt_antwortfrist():
    rec = vorschlag(posted_at=vor_tagen(365),
                    nicht_gefunden_seit=vor_tagen(main.MOSS_PROPOSAL_MISSING_DAYS))

    assert main._proposal_give_up_reason("1", rec, beantwortet=False,
                                         jetzt=JETZT) == "nicht_auffindbar"


# ── Vermisst-Zähler ──────────────────────────────────────────────────────────

def test_erste_vermisstmeldung_setzt_zeitstempel():
    rec = vorschlag()

    main._proposal_missing_seen(rec, JETZT)

    assert rec["nicht_gefunden_seit"] == JETZT.isoformat(timespec="seconds")
    assert rec["nicht_gefunden_versuche"] == 1


def test_weitere_vermisstmeldungen_behalten_den_ersten_zeitstempel():
    """Die Frist läuft ab dem ersten Fehlversuch, nicht ab dem letzten."""
    rec = vorschlag()

    main._proposal_missing_seen(rec, JETZT)
    main._proposal_missing_seen(rec, JETZT + timedelta(hours=5))

    assert rec["nicht_gefunden_seit"] == JETZT.isoformat(timespec="seconds")
    assert rec["nicht_gefunden_versuche"] == 2


def test_wiedergefundene_transaktion_setzt_die_frist_zurueck():
    """Ein Aussetzer darf nicht über Wochen nachwirken."""
    rec = vorschlag(nicht_gefunden_seit=vor_tagen(13), nicht_gefunden_versuche=200)

    main._proposal_missing_cleared(rec)

    assert "nicht_gefunden_seit" not in rec
    assert main._proposal_give_up_reason("1", rec, beantwortet=True, jetzt=JETZT) is None


# ── Zeitstempel lesen ────────────────────────────────────────────────────────

def test_posted_at_hat_vorrang_vor_slack_zeitstempel():
    rec = vorschlag(posted_at="2026-10-01T09:00:00")

    assert main._proposal_posted_at("1794218418.349439", rec) == datetime(2026, 10, 1, 9, 0)


def test_ohne_posted_at_dient_der_slack_zeitstempel_als_ersatz():
    """Einträge aus der Zeit vor posted_at sollen trotzdem altern können."""
    rec = vorschlag()
    del rec["posted_at"]
    ts = str(datetime(2026, 9, 22, 8, 30).timestamp())

    assert main._proposal_posted_at(ts, rec) == datetime(2026, 9, 22, 8, 30)


def test_kaputte_zeitstempel_werfen_nicht():
    assert main._parse_iso("gestern") is None
    assert main._parse_iso(None) is None
    assert main._proposal_posted_at("keine-zahl", {}) is None
    # Ohne lesbare Zeitstempel wird nichts abgelegt – lieber offen lassen.
    assert main._proposal_give_up_reason("keine-zahl", {"status": "open"},
                                         beantwortet=False, jetzt=JETZT) is None


# ── Ablegen ──────────────────────────────────────────────────────────────────

class FakeClient:
    def __init__(self):
        self.nachrichten = []

    def chat_postMessage(self, channel, thread_ts=None, text=None):
        self.nachrichten.append({"channel": channel, "thread_ts": thread_ts, "text": text})
        return {"ok": True}


def test_ablegen_vermerkt_grund_und_meldet_im_thread():
    rec = vorschlag(nicht_gefunden_seit=vor_tagen(20))
    client = FakeClient()

    main._retire_proposal(client, "1794218418.349439", rec, "nicht_auffindbar", JETZT)

    assert rec["status"] == "verfallen"
    assert rec["verfallen_grund"] == "nicht_auffindbar"
    assert rec["verfallen_am"] == JETZT.isoformat(timespec="seconds")
    # Hinweis geht in den Thread, nicht in den Kanal: dort steht die Antwort.
    assert len(client.nachrichten) == 1
    nachricht = client.nachrichten[0]
    assert nachricht["thread_ts"] == "1794218418.349439"
    assert nachricht["channel"] == "C-TEST"
    assert "20 Tagen" in nachricht["text"]
    assert "von Hand" in nachricht["text"]


def test_gesperrte_transaktion_bekommt_eigenen_hinweis():
    rec = vorschlag()
    client = FakeClient()

    main._retire_proposal(client, "1", rec, "gesperrt", JETZT)

    assert rec["verfallen_grund"] == "gesperrt"
    assert "nicht mehr bearbeiten" in client.nachrichten[0]["text"]


def test_ablegen_ohne_slack_client_funktioniert():
    """Der Vorschlag wird auch abgelegt, wenn Slack nicht erreichbar ist –
    sonst bliebe er ewig im Kreis laufen."""
    rec = vorschlag()

    main._retire_proposal(None, "1", rec, "ohne_antwort", JETZT)

    assert rec["status"] == "verfallen"


def test_slack_fehler_beim_hinweis_bricht_nichts_ab():
    class KaputterClient:
        def chat_postMessage(self, **kwargs):
            raise RuntimeError("Slack nicht erreichbar")

    rec = vorschlag()

    main._retire_proposal(KaputterClient(), "1", rec, "ohne_antwort", JETZT)

    assert rec["status"] == "verfallen"
