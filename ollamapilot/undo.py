from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ollamapilot import paths
from ollamapilot.tools.paths import is_secret

MAX_ENTRIES = 100
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
# Turno em andamento; o agente define no início e limpa no fim. Só um turno roda por vez.
CURRENT_TURN = ""


def _folder() -> Path:
    return paths.BACKUPS


def _index() -> Path:
    return _folder() / "indice.json"


def _load() -> list[dict]:
    try:
        data = json.loads(_index().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _store(entries: list[dict]) -> None:
    paths.write_text_atomic(_index(), json.dumps(entries, ensure_ascii=False, indent=1))


def _key(path: Path) -> str:
    return str(path).lower()


def remember(path: Path, action: str, plan_id: str | None = None) -> dict | None:
    """Guarda o estado do arquivo antes de uma alteração. Devolve None se não houver como desfazer."""
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    entry: dict = {"caminho": str(path), "acao": action, "quando": time.time()}
    if plan_id:
        entry["plano"] = plan_id
    if CURRENT_TURN:
        entry["turno"] = CURRENT_TURN
    if path.is_file():
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
        if is_secret(path):
            sealed = _protect(data)
            if sealed is None:
                return None
            data = sealed
            entry["cifrado"] = True
        name = f"{int(time.time() * 1000)}-{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:12]}.bin"
        paths.write_bytes_atomic(folder / name, data)
        entry.update({"existia": True, "copia": name, "bytes": size})
    elif path.exists():
        return None
    else:
        entry["existia"] = False
    entries = _load()
    entries.append(entry)
    _store(_prune(entries))
    return entry


def latest(path: Path) -> dict | None:
    wanted = _key(path)
    for entry in reversed(_load()):
        if _key(Path(str(entry.get("caminho") or ""))) == wanted:
            return entry
    return None


def restore(path: Path) -> dict:
    entry = latest(path)
    if entry is None:
        raise ValueError("Não há alteração do OllamaPilot guardada para esse arquivo.")
    if entry.get("existia"):
        copy = _folder() / str(entry.get("copia") or "")
        if not copy.is_file():
            raise ValueError("A cópia anterior desse arquivo não está mais disponível.")
        paths.write_bytes_atomic(path, _read_copy(copy, bool(entry.get("cifrado"))))
        result = {"caminho": str(path), "restaurado": True, "bytes": copy.stat().st_size}
    else:
        if path.is_file():
            path.unlink()
        result = {"caminho": str(path), "removido": True, "motivo": "o arquivo não existia antes da alteração"}
    entries = _load()
    entries = [item for item in entries if item is not entry and item != entry]
    _store(entries)
    if entry.get("copia"):
        (_folder() / str(entry["copia"])).unlink(missing_ok=True)
    result["alteracao_desfeita"] = entry.get("acao")
    return result


def restore_plan(plan_id: str) -> dict:
    """Restaura, na ordem inversa, as cópias marcadas com o mesmo plano."""
    wanted = str(plan_id or "").strip()
    if not wanted:
        raise ValueError("Informe o plano a desfazer.")
    result = _restore_marked("plano", wanted)
    if result is None:
        raise ValueError("Não há alterações guardadas para esse plano.")
    return {"plano": wanted, "restaurados": result}


def restore_turn(turn_id: str) -> list[dict]:
    """Volta os arquivos alterados num turno da conversa. Lista vazia se o turno não mexeu em arquivo."""
    wanted = str(turn_id or "").strip()
    if not wanted:
        return []
    return _restore_marked("turno", wanted) or []


def turn_changes(turn_id: str) -> int:
    wanted = str(turn_id or "").strip()
    return sum(1 for item in _load() if wanted and item.get("turno") == wanted)


def _restore_marked(field: str, wanted: str) -> list[dict] | None:
    entries = [item for item in _load() if item.get(field) == wanted]
    if not entries:
        return None
    restored = []
    for entry in reversed(entries):
        path = Path(str(entry.get("caminho") or ""))
        if entry.get("existia"):
            copy = _folder() / str(entry.get("copia") or "")
            if not copy.is_file():
                restored.append({"caminho": str(path), "erro": "a cópia não está mais disponível"})
                continue
            paths.write_bytes_atomic(path, _read_copy(copy, bool(entry.get("cifrado"))))
            restored.append({"caminho": str(path), "restaurado": True})
        elif path.is_file():
            path.unlink()
            restored.append({"caminho": str(path), "removido": True})
        else:
            restored.append({"caminho": str(path), "restaurado": True})
    remaining = [item for item in _load() if item.get(field) != wanted]
    for entry in entries:
        if entry.get("copia"):
            (_folder() / str(entry["copia"])).unlink(missing_ok=True)
    _store(remaining)
    return restored


def _protect(data: bytes) -> bytes | None:
    try:
        import win32crypt

        return bytes(win32crypt.CryptProtectData(data, "OllamaPilot"))
    except Exception:
        return None


def _reveal(data: bytes) -> bytes | None:
    try:
        import win32crypt

        result = win32crypt.CryptUnprotectData(data, None, None, None, 0)
        blob = result[1] if isinstance(result, tuple) else result
        return bytes(blob)
    except Exception:
        return None


def _read_copy(copy: Path, sealed: bool) -> bytes:
    data = copy.read_bytes()
    if not sealed:
        return data
    plain = _reveal(data)
    if plain is None:
        raise ValueError("A cópia cifrada não pôde ser aberta neste usuário do Windows.")
    return plain


def describe(entry: dict) -> str:
    moment = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(float(entry.get("quando") or 0)))
    if entry.get("existia"):
        return f"volta à versão anterior a '{entry.get('acao')}' de {moment} ({entry.get('bytes')} bytes)"
    return f"apaga o arquivo, que não existia antes de '{entry.get('acao')}' de {moment}"


def _prune(entries: list[dict]) -> list[dict]:
    total = 0
    kept: list[dict] = []
    for entry in reversed(entries):
        size = int(entry.get("bytes") or 0)
        if len(kept) >= MAX_ENTRIES or total + size > MAX_TOTAL_BYTES:
            if entry.get("copia"):
                (_folder() / str(entry["copia"])).unlink(missing_ok=True)
            continue
        total += size
        kept.append(entry)
    kept.reverse()
    return kept
