"""Gemeinsame Test-Vorbereitung.

main.py liegt im Projektwurzelverzeichnis und wird hier importierbar gemacht,
damit die Tests aus jedem Verzeichnis laufen.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
