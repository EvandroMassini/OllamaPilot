from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ollamapilot.paths import LOGS, ensure_storage
from ollamapilot.privacy import redact as _scrub
from ollamapilot.secrets import known_values

ERROR_LOG_LIMIT = 1_000_000
TURN_LOG_LIMIT = 2_000_000


def log_error(kind: str, message: str, **details: object) -> Path | None:
    """Acrescenta um erro em storage/logs/erros.log. Uma falha ao gravar não interrompe o turno."""
    try:
        ensure_storage()
        LOGS.mkdir(parents=True, exist_ok=True)
        path = LOGS / "erros.log"
        _rotate(path, ERROR_LOG_LIMIT)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"{stamp}  {kind}", _detail(message) or "(sem mensagem)"]
        for key, value in details.items():
            if _secret_key(key):
                lines.append(f"  {key}: [omitido]")
            else:
                lines.append(f"  {key}: {_detail(value)}")
        lines.append("")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path
    except Exception:
        return None


def log_turn(record: dict) -> Path | None:
    """Acrescenta a trilha de um turno em storage/logs/turnos.jsonl, sem conteúdo da conversa."""
    try:
        ensure_storage()
        LOGS.mkdir(parents=True, exist_ok=True)
        path = LOGS / "turnos.jsonl"
        _rotate(path, TURN_LOG_LIMIT)
        clean = {key: _clean_value(key, value) for key, value in record.items()}
        clean.setdefault("quando", datetime.now().isoformat(timespec="seconds"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(clean, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception:
        return None


def redact(text: str) -> str:
    """Troca chaves, tokens, senhas e os valores do cofre por [omitido]."""
    try:
        secrets = known_values()
    except Exception:
        secrets = []
    return _scrub(text, secrets)


def _rotate(path: Path, limit: int) -> None:
    try:
        if path.is_file() and path.stat().st_size > limit:
            older = path.with_name(path.stem + ".1" + path.suffix)
            older.unlink(missing_ok=True)
            path.replace(older)
    except OSError:
        pass


def _clean_value(key: str, value: object) -> object:
    if _secret_key(key):
        return "[omitido]"
    if isinstance(value, dict):
        return {str(k): _clean_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_value(key, item) for item in value[:200]]
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= 300 else text[:300] + "…"
    return value


def _secret_key(key: str) -> bool:
    folded = key.lower()
    return any(part in folded for part in ("key", "senha", "password", "token", "authorization", "secret"))


def _detail(value: object) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    lowered = text.lower()
    if "api_key" in lowered or "cloud_api_key" in lowered:
        return "[omitido]"
    text = redact(text)
    if len(text) > 2000:
        return text[:2000] + "…"
    return text
