from __future__ import annotations

import os
import re
from pathlib import Path

_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)", re.IGNORECASE)
_SECRET_NAMES = {
    ".env",
    ".htpasswd",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
    "web.config",
}
_SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".kdbx")
_SECRET_PARTS = (
    "password",
    "senha",
    "secret",
    "credential",
    "cookie",
    "login data",
    "ntds.dit",
)


def resolve_path(value: object) -> Path:
    raw = str(value or "").strip().strip('"')
    if not raw or "\x00" in raw:
        raise ValueError("Informe um caminho absoluto, como E:\\pasta\\arquivo.txt.")
    raw = raw.replace("/", "\\")
    if not re.match(r"^[A-Za-z]:\\", raw) and not raw.startswith("\\\\"):
        raise ValueError("Use um caminho absoluto. O OllamaPilot não tem pasta padrão; o caminho vem da conversa.")
    path = Path(os.path.abspath(raw))
    if _DEVICE.match(path.name):
        raise ValueError("Esse nome é reservado pelo Windows.")
    cursor = path
    while True:
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("O caminho passa por um atalho simbólico e foi recusado.")
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    return path


def is_secret(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    if ".ssh" in parts or ".gnupg" in parts or "credential" in parts:
        return True
    name = path.name.lower()
    if name in _SECRET_NAMES or name.startswith(".env"):
        return True
    if name.endswith(_SECRET_SUFFIXES):
        return True
    return any(part in name for part in _SECRET_PARTS)


def risk_note(path: Path) -> str:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    folders = [
        windir,
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ]
    text = str(path)
    for folder in folders:
        if folder and text.lower().startswith(folder.lower()):
            return f"Este caminho fica em uma pasta protegida do Windows: {folder}."
    return ""


def clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True
