"""Print backend selection and detection."""
from __future__ import annotations

from app.services.printing.base import PrinterInfo, PrintResult
from app.services.printing.linux import LinuxPrintBackend


def get_backend():
    return LinuxPrintBackend()   # Linux servers only (CUPS / lp)


def detect_printers() -> list[PrinterInfo]:
    return get_backend().list_printers()


def default_printer() -> str | None:
    return get_backend().default_printer()


__all__ = [
    "PrinterInfo",
    "PrintResult",
    "get_backend",
    "detect_printers",
    "default_printer",
]
