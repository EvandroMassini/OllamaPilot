"""Frases curtas que o usuário pediu para guardar, entre conversas."""

from __future__ import annotations

import json
import re
from datetime import datetime

from ollamapilot import paths

MAX_ITEMS = 40
MAX_CHARS = 240
_SECRET = re.compile(
    r"(?i)(\b(senha|password|token|api[_ -]?key|secret|chave)\b\s*([:=]| é | eh )\s*\S+|"
    r"\b(sk-|ghp_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9])"
)


def file_path():
    return paths.STORAGE / "memoria.json"


def looks_like_secret(text: str) -> bool:
    return bool(_SECRET.search(text or ""))


def load() -> list[dict]:
    path = file_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("texto") or "").strip()
        if not text:
            continue
        items.append({"texto": text[:MAX_CHARS], "data": str(item.get("data") or "")})
    return items[:MAX_ITEMS]


def save(items: list[dict]) -> None:
    paths.ensure_storage()
    paths.write_text_atomic(file_path(), json.dumps(items[:MAX_ITEMS], ensure_ascii=False, indent=2))


def remember(text: str) -> dict:
    phrase = " ".join(str(text or "").split())
    if not phrase:
        raise ValueError("Informe a frase para guardar.")
    if len(phrase) > MAX_CHARS:
        raise ValueError(f"A frase passa de {MAX_CHARS} caracteres.")
    if looks_like_secret(phrase):
        raise ValueError("Não guardo senha, chave ou token. A frase não foi gravada.")
    items = load()
    items.append({"texto": phrase, "data": datetime.now().isoformat(timespec="seconds")})
    if len(items) > MAX_ITEMS:
        items = items[-MAX_ITEMS:]
    save(items)
    return {"guardado": phrase, "total": len(items)}


def forget(text: str) -> dict:
    needle = " ".join(str(text or "").split()).lower()
    if not needle:
        raise ValueError("Informe o texto da memória que deve sair.")
    items = load()
    kept = [item for item in items if needle not in item["texto"].lower()]
    removed = len(items) - len(kept)
    if not removed:
        return {"apagadas": 0, "mensagem": "Nenhuma memória continha esse texto."}
    save(kept)
    return {"apagadas": removed, "total": len(kept)}


def prompt_line() -> str:
    items = load()
    if not items:
        return ""
    phrases = "; ".join(item["texto"] for item in items)
    return " Fatos que o usuário pediu para guardar: " + phrases + "."
