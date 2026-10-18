"""Chaves e senhas fora do settings.json.

No Windows, o destino é o Gerenciador de Credenciais. Se ele não estiver
disponível, a cópia fica cifrada com DPAPI num arquivo local.
OLLAMAPILOT_VAULT=memory guarda só na memória: é o modo dos testes.
"""

from __future__ import annotations

import base64
import json
import os
import time

from ollamapilot.paths import STORAGE, write_text_atomic

_PREFIX = "OllamaPilot/"


class SecretStore:
    def get(self, name: str) -> str:
        raise NotImplementedError

    def set(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError

    def known(self) -> list[str]:
        return []


class MemoryStore(SecretStore):
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str:
        return self.values.get(name, "")

    def set(self, name: str, value: str) -> None:
        self.values[name] = value
        _invalidate()

    def delete(self, name: str) -> None:
        self.values.pop(name, None)
        _invalidate()

    def known(self) -> list[str]:
        return [value for value in self.values.values() if len(value) >= 6]


class FileStore(SecretStore):
    """DPAPI. O arquivo sozinho não revela o segredo em outro usuário do Windows."""

    def __init__(self, path=None) -> None:
        self.path = path or (STORAGE / "cofre.dat")

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, payload: dict) -> None:
        write_text_atomic(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def get(self, name: str) -> str:
        raw = self._read().get(name)
        if not isinstance(raw, str) or not raw:
            return ""
        try:
            import win32crypt

            blob = base64.b64decode(raw)
            result = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
            data = result[1] if isinstance(result, tuple) else result
            return bytes(data).decode("utf-8")
        except Exception:
            return ""

    def set(self, name: str, value: str) -> None:
        import win32crypt

        blob = win32crypt.CryptProtectData(value.encode("utf-8"), "OllamaPilot")
        payload = self._read()
        payload[name] = base64.b64encode(bytes(blob)).decode("ascii")
        self._write(payload)
        _invalidate()

    def delete(self, name: str) -> None:
        payload = self._read()
        if name not in payload:
            return
        payload.pop(name, None)
        self._write(payload)
        _invalidate()


class WindowsStore(SecretStore):
    def __init__(self) -> None:
        self._file = FileStore()

    def get(self, name: str) -> str:
        try:
            import win32cred

            cred = win32cred.CredRead(_PREFIX + name, win32cred.CRED_TYPE_GENERIC, 0)
            blob = cred.get("CredentialBlob")
            if isinstance(blob, str):
                return blob
            if isinstance(blob, bytes):
                for encoding in ("utf-16-le", "utf-8"):
                    try:
                        text = blob.decode(encoding)
                    except UnicodeDecodeError:
                        continue
                    if text and "\x00" not in text:
                        return text
                return blob.decode("utf-8", errors="ignore").replace("\x00", "")
        except Exception:
            pass
        return self._file.get(name)

    def set(self, name: str, value: str) -> None:
        try:
            import win32cred

            win32cred.CredWrite(
                {
                    "Type": win32cred.CRED_TYPE_GENERIC,
                    "TargetName": _PREFIX + name,
                    "UserName": "OllamaPilot",
                    "CredentialBlob": value,
                    "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                    "Comment": "Segredo do OllamaPilot",
                },
                0,
            )
            _invalidate()
            return
        except Exception:
            self._file.set(name, value)

    def delete(self, name: str) -> None:
        try:
            import win32cred

            win32cred.CredDelete(_PREFIX + name, win32cred.CRED_TYPE_GENERIC, 0)
        except Exception:
            pass
        self._file.delete(name)


_memory: MemoryStore | None = None
_windows: WindowsStore | None = None


def current() -> SecretStore:
    global _memory, _windows
    if os.environ.get("OLLAMAPILOT_VAULT") == "memory":
        if _memory is None:
            _memory = MemoryStore()
        return _memory
    if _windows is None:
        _windows = WindowsStore()
    return _windows


_CACHE_SECONDS = 30.0
_cache_at = 0.0
_cache_values: list[str] = []


def _invalidate() -> None:
    global _cache_at
    _cache_at = 0.0


def known_values() -> list[str]:
    """Valores do cofre, por no máximo 30 segundos, para não consultar o Windows a cada token."""
    global _cache_at, _cache_values
    now = time.monotonic()
    if _cache_at and now - _cache_at < _CACHE_SECONDS:
        return list(_cache_values)
    store = current()
    values = []
    for name in ("cloud_api_key", "web_api_key", "db_password", "openai_api_key"):
        try:
            value = store.get(name)
        except Exception:
            value = ""
        if isinstance(value, str) and len(value) >= 6:
            values.append(value)
    values.extend(item for item in store.known() if item not in values)
    _cache_values = values
    _cache_at = now
    return list(values)
