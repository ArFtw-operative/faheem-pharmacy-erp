"""Printer abstraction layer.

The application talks only to `PrintBackend`. Platform-specific details
(CUPS/lp on Linux) stay behind this interface so the
same billing code runs in dev and production. Printer names are never
hardcoded: they are discovered at runtime.
"""
from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class PrinterInfo:
    name: str
    is_default: bool = False
    description: str = ""
    kind: str = "printer"  # printer | a4 | thermal | unknown


@dataclass
class PrintResult:
    ok: bool
    backend: str
    printer: str = ""
    message: str = ""
    output_path: str = ""
    raw: dict = field(default_factory=dict)


@runtime_checkable
class PrintBackend(Protocol):
    name: str

    def list_printers(self) -> list[PrinterInfo]: ...

    def default_printer(self) -> str | None: ...

    def print_text(self, text: str, printer: str | None = None) -> PrintResult: ...

    def print_pdf(self, pdf_path: str, printer: str | None = None) -> PrintResult: ...


def classify_printer(name: str) -> str:
    lowered = name.lower()
    if any(k in lowered for k in ("a4", "laser", "officejet", "deskjet")):
        return "a4"
    if any(k in lowered for k in ("thermal", "pos", "receipt", "tm-", "rp")):
        return "thermal"
    return "printer"


def current_platform() -> str:
    return platform.system().lower()
