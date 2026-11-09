"""Tests für die Störungsmeldungen nach Slack.

Wichtigste Eigenschaft: Eine Dauerstörung darf den Kanal nicht zumüllen, aber
auch nicht nach der ersten Nachricht für immer verstummen. Und keine
Benachrichtigung darf je einen Lauf abbrechen.
"""
import json
from datetime import datetime, timedelta

import pytest

import main


class FakeClient:
    """Ersatz für slack_sdk.WebClient – sammelt die gesendeten Nachrichten."""
    gesendet = []

    def __init__(self, token=None):
        self.token = token

    def chat_postMessage(self, channel, text):
        FakeClient.gesendet.append({"channel": channel, "text": text})
        return {"ok": True}


@pytest.fixture
def slack(tmp_path, monkeypatch):
    import slack_sdk
    FakeClient.gesendet = []
    monkeypatch.setattr(slack_sdk, "WebClient", FakeClient)
    monkeypatch.setattr(main, "ALERT_STATE_FILE", tmp_path / "alerts_sent.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-TEST")
    monkeypatch.delenv("SLACK_ALERT_CHANNEL_ID", raising=False)
    return FakeClient


def _state(monkeypatch=None):
    return json.loads(main.ALERT_STATE_FILE.read_text())


def test_erste_stoerung_wird_gemeldet(slack):
    assert main.notify_failure("Amazon-Lauf fehlgeschlagen", details="Login-Timeout.",
                               hinweis="Session neu einrichten", kennung="run_amazon")

    assert len(slack.gesendet) == 1
    nachricht = slack.gesendet[0]
    assert nachricht["channel"] == "C-TEST"
    assert "Amazon-Lauf fehlgeschlagen" in nachricht["text"]
    assert "Login-Timeout." in nachricht["text"]
    assert "Session neu einrichten" in nachricht["text"]


def test_eigener_alarmkanal_hat_vorrang(slack, monkeypatch):
    monkeypatch.setenv("SLACK_ALERT_CHANNEL_ID", "C-ALARM")

    main.notify_failure("Störung", kennung="k")

    assert slack.gesendet[0]["channel"] == "C-ALARM"


def test_gleiche_stoerung_im_cooldown_wird_nicht_wiederholt(slack):
    main.notify_failure("Störung", kennung="k")
    assert main.notify_failure("Störung", kennung="k") is False

    assert len(slack.gesendet) == 1
    assert _state()["k"]["unterdrueckt"] == 1


def test_nach_ablauf_des_cooldowns_wird_wieder_gemeldet(slack):
    main.notify_failure("Störung", kennung="k")
    main.notify_failure("Störung", kennung="k")  # unterdrückt

    # Uhr vorstellen, statt im Test zu warten.
    state = _state()
    state["k"]["zuletzt"] = (datetime.now() - timedelta(hours=99)).isoformat(timespec="seconds")
    main.ALERT_STATE_FILE.write_text(json.dumps(state))

    assert main.notify_failure("Störung", kennung="k")
    assert len(slack.gesendet) == 2
    # Die zweite Nachricht sagt, wie oft es zwischendurch auftrat.
    assert "1×" in slack.gesendet[1]["text"]
    assert _state()["k"]["unterdrueckt"] == 0


def test_eigener_cooldown_je_meldung(slack):
    main.notify_failure("Störung", kennung="k", cooldown_hours=0)

    assert main.notify_failure("Störung", kennung="k", cooldown_hours=0)
    assert len(slack.gesendet) == 2


def test_verschiedene_stoerungen_blockieren_sich_nicht(slack):
    main.notify_failure("Amazon", kennung="run_amazon")
    main.notify_failure("Moss", kennung="moss_abgleich")

    assert len(slack.gesendet) == 2


def test_ohne_token_keine_meldung_und_kein_fehler(slack, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    assert main.notify_failure("Störung", kennung="k") is False
    assert slack.gesendet == []


def test_slack_fehler_bricht_nichts_ab(slack, monkeypatch):
    import slack_sdk

    class KaputterClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, channel, text):
            raise RuntimeError("Slack nicht erreichbar")

    monkeypatch.setattr(slack_sdk, "WebClient", KaputterClient)

    assert main.notify_failure("Störung", kennung="k") is False


def test_entwarnung_nur_nach_gemeldeter_stoerung(slack):
    # Ohne vorherige Störung bleibt es still.
    assert main.notify_recovery("run_amazon", "Amazon-Lauf") is False
    assert slack.gesendet == []

    main.notify_failure("Amazon-Lauf fehlgeschlagen", kennung="run_amazon")
    assert main.notify_recovery("run_amazon", "Amazon-Lauf")

    assert "läuft wieder" in slack.gesendet[-1]["text"]
    # Speicher geleert: die nächste Störung meldet sofort statt im Cooldown.
    assert "run_amazon" not in _state()


def test_entwarnung_macht_den_weg_fuer_neue_meldung_frei(slack):
    main.notify_failure("Störung", kennung="k")
    main.notify_recovery("k", "Lauf")

    assert main.notify_failure("Störung", kennung="k")
    assert len(slack.gesendet) == 3  # Störung, Entwarnung, Störung


def test_kaputter_speicher_wird_ignoriert(slack):
    main.ALERT_STATE_FILE.write_text("{kein json")

    assert main.notify_failure("Störung", kennung="k")
