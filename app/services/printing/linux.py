"""Linux/CUPS print backend.

Uses the `lp` command-line tool when available and degrades gracefully to
writing the job to a spool file when no printer/CUPS is configured. This keeps
billing functional in a headless dev environment.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.config import LOG_DIR
from app.services.printing.base import PrinterInfo, PrintResult, classify_printer


class LinuxPrintBackend:
    name = "cups"

    def _lp(self) -> str | None:
        return shutil.which("lp")

    def list_printers(self) -> list[PrinterInfo]:
        printers: list[PrinterInfo] = []
        default_name = self.default_printer()
        lpstat = shutil.which("lpstat")
        if lpstat:
            try:
                out = subprocess.run(
                    [lpstat, "-a"], capture_output=True, text=True, timeout=10
                ).stdout
                for line in out.splitlines():
                    if not line.strip():
                        continue
                    name = line.split()[0]
                    printers.append(
                        PrinterInfo(
                            name=name,
                            is_default=(name == default_name),
                            description=line.strip(),
                            kind=classify_printer(name),
                        )
                    )
            except (subprocess.SubprocessError, OSError):
                pass
        return printers

    def default_printer(self) -> str | None:
        lpstat = shutil.which("lpstat")
        if not lpstat:
            return None
        try:
            out = subprocess.run(
                [lpstat, "-d"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
            if ":" in out:
                return out.split(":", 1)[1].strip() or None
        except (subprocess.SubprocessError, OSError):
            return None
        return None

    def _fallback(self, text: str, printer: str | None, reason: str) -> PrintResult:
        spool = Path(LOG_DIR) / "print_spool.txt"
        with spool.open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== PRINT JOB ({reason}) printer={printer or 'default'} =====\n")
            fh.write(text)
            fh.write("\n")
        return PrintResult(
            ok=True,
            backend=self.name,
            printer=printer or "",
            message=f"No CUPS printer available ({reason}); wrote job to {spool}",
            output_path=str(spool),
        )

    def print_text(self, text: str, printer: str | None = None) -> PrintResult:
        lp = self._lp()
        if not lp:
            return self._fallback(text, printer, "lp not installed")
        cmd = [lp]
        if printer:
            cmd += ["-d", printer]
        try:
            proc = subprocess.run(
                cmd, input=text, capture_output=True, text=True, timeout=30
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return self._fallback(text, printer, f"lp failed: {exc}")
        if proc.returncode != 0:
            return self._fallback(text, printer, proc.stderr.strip() or "lp error")
        return PrintResult(ok=True, backend=self.name, printer=printer or "", message=proc.stdout.strip())

    def print_pdf(self, pdf_path: str, printer: str | None = None) -> PrintResult:
        lp = self._lp()
        if not lp:
            return self._fallback(f"[PDF at {pdf_path}]", printer, "lp not installed")
        cmd = [lp]
        if printer:
            cmd += ["-d", printer]
        cmd.append(pdf_path)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            return self._fallback(f"[PDF at {pdf_path}]", printer, f"lp failed: {exc}")
        if proc.returncode != 0:
            return self._fallback(f"[PDF at {pdf_path}]", printer, proc.stderr.strip() or "lp error")
        return PrintResult(ok=True, backend=self.name, printer=printer or "", message=proc.stdout.strip())
