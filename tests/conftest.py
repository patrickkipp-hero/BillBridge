"""Gemeinsame Test-Vorbereitung.

main.py liegt im Projektwurzelverzeichnis und wird hier importierbar gemacht,
damit die Tests aus jedem Verzeichnis laufen.
"""
import sys
import types
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


# slack_sdk brauchen die Tests nur als Attrappe – gesendet wird nie. Auf
# diesem Mac hat nicht jeder Interpreter das Paket (das python3 auf dem PATH
# ist ein anderes als das mit den Abhängigkeiten), und die Tests sollen auf
# jedem laufen. Deshalb notfalls ein Platzhalter-Modul, dessen WebClient beim
# tatsächlichen Versand auffliegt statt still nichts zu tun.
try:
    import slack_sdk  # noqa: F401
except ModuleNotFoundError:
    platzhalter = types.ModuleType("slack_sdk")

    class WebClient:
        def __init__(self, token=None):
            self.token = token

        def __getattr__(self, name):
            raise AssertionError(
                f"slack_sdk ist nicht installiert – der Test hätte WebClient "
                f"ersetzen müssen, bevor er '{name}' aufruft."
            )

    platzhalter.WebClient = WebClient
    sys.modules["slack_sdk"] = platzhalter
