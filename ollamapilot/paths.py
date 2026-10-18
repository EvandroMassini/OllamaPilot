from __future__ import annotations

import os
import sys
from pathlib import Path


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Pasta dos arquivos empacotados. No executável, é a pasta interna do PyInstaller."""
    if _frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def data_root() -> Path:
    """Pasta gravável. No executável, fica ao lado do OllamaPilot.exe."""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = data_root()
PACKAGE = Path(__file__).resolve().parent
STORAGE = Path(os.environ["OLLAMAPILOT_STORAGE"]) if os.environ.get("OLLAMAPILOT_STORAGE") else ROOT / "storage"
KNOWLEDGE = STORAGE / "knowledge"
ARTIFACTS = STORAGE / "artifacts"
LOGS = STORAGE / "logs"
BACKUPS = STORAGE / "backups"
GHIDRA_SCRIPTS = bundle_root() / "ghidra_scripts" if _frozen() else PACKAGE / "ghidra_scripts"
ICON = bundle_root() / "assets" / "ollamapilot.ico" if _frozen() else data_root() / "assets" / "ollamapilot.ico"
SESSION_FILE = STORAGE / "sessao.json"
SETTINGS_FILE = STORAGE / "settings.json"


def ensure_storage() -> None:
    for directory in (STORAGE, KNOWLEDGE, ARTIFACTS, LOGS):
        directory.mkdir(parents=True, exist_ok=True)


def write_text_atomic(path: Path, text: str) -> None:
    """Grava o arquivo inteiro e só então substitui a cópia anterior."""
    write_bytes_atomic(path, text.encode("utf-8"))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def park_invalid(path: Path) -> None:
    """Afasta um JSON ilegível para não ser sobrescrito pela próxima gravação válida."""
    if not path.is_file():
        return
    parked = path.with_name(path.name + ".invalido")
    if parked.exists():
        from datetime import datetime

        parked = path.with_name(f"{path.name}.invalido-{datetime.now():%Y%m%d-%H%M%S-%f}")
    try:
        path.replace(parked)
    except OSError:
        pass
