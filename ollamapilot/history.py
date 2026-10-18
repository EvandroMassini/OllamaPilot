"""Conversas anteriores, uma por arquivo em storage/conversas."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path

from ollamapilot import paths
from ollamapilot.privacy import for_disk

_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def folder() -> Path:
    return paths.STORAGE / "conversas"


def new_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def _path(conversation_id: str) -> Path:
    if not valid_id(conversation_id):
        raise ValueError(f"Identificador de conversa inválido: {conversation_id!r}")
    return folder() / f"{conversation_id}.json"


def title_for(display: list[dict]) -> str:
    for item in display:
        if item.get("role") != "user":
            continue
        text = str(item.get("content") or "").split("\n\nAnexos:")[0]
        line = " ".join(text.split())
        if line:
            return line if len(line) <= 70 else line[:69] + "…"
    return "Conversa sem título"


def save(conversation_id: str, payload: dict) -> bool:
    """Grava a conversa. Conversa sem mensagens não entra no histórico."""
    display = payload.get("display") or []
    if not display:
        return False
    path = _path(conversation_id)
    now = datetime.now().isoformat(timespec="seconds")
    created = now
    manual = False
    pinned = False
    kept_title = ""
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if isinstance(previous, dict):
            if isinstance(previous.get("criada"), str):
                created = previous["criada"]
            manual = bool(previous.get("titulo_manual"))
            pinned = bool(previous.get("fixada"))
            kept_title = str(previous.get("titulo") or "")
    record = for_disk(dict(payload))
    record.update(
        {
            "id": conversation_id,
            "titulo": kept_title if manual and kept_title else title_for(record.get("display") or display),
            "titulo_manual": manual,
            "fixada": pinned,
            "criada": created,
            "atualizada": now,
            "mensagens": sum(1 for item in display if item.get("role") == "user"),
        }
    )
    paths.write_text_atomic(path, json.dumps(record, ensure_ascii=False, indent=2, default=str))
    return True


def load(conversation_id: str) -> dict:
    payload = json.loads(_path(conversation_id).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("O arquivo da conversa não tem o formato esperado.")
    return payload


def list_conversations() -> list[dict]:
    """Resumo de cada conversa, da mais recente para a mais antiga. Arquivo ilegível aparece para poder ser excluído."""
    rows = []
    if not folder().is_dir():
        return rows
    for path in folder().glob("*.json"):
        conversation_id = path.stem
        if not valid_id(conversation_id):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            row = {
                "id": conversation_id,
                "titulo": str(payload.get("titulo") or "Conversa sem título"),
                "criada": str(payload.get("criada") or ""),
                "atualizada": str(payload.get("atualizada") or ""),
                "mensagens": int(payload.get("mensagens") or 0),
                "fixada": bool(payload.get("fixada")),
                "legivel": True,
            }
        except (OSError, ValueError, TypeError):
            stamp = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
            row = {
                "id": conversation_id,
                "titulo": "Conversa ilegível",
                "criada": stamp,
                "atualizada": stamp,
                "mensagens": 0,
                "fixada": False,
                "legivel": False,
            }
        row["bytes"] = path.stat().st_size
        rows.append(row)
    # Fixar não mexe em atualizada: as conversas soltas continuam da mais recente para a mais antiga.
    pinned = sorted((row for row in rows if row.get("fixada")), key=lambda item: item["titulo"].casefold())
    rest = sorted((row for row in rows if not row.get("fixada")), key=lambda item: item["atualizada"], reverse=True)
    return pinned + rest


def rename(conversation_id: str, title: str) -> str:
    """Troca o título mostrado na lista. O próximo salvamento do turno não desfaz essa troca."""
    text = " ".join(str(title or "").split())
    if not text:
        raise ValueError("O título não pode ficar vazio.")
    if len(text) > 80:
        text = text[:79] + "…"
    _patch(conversation_id, {"titulo": text, "titulo_manual": True})
    return text


def set_pinned(conversation_id: str, pinned: bool) -> None:
    """Conversa fixada fica no topo da lista, em ordem alfabética com as outras fixadas."""
    _patch(conversation_id, {"fixada": bool(pinned)})


def _patch(conversation_id: str, fields: dict) -> None:
    path = _path(conversation_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("O arquivo da conversa não tem o formato esperado.")
    payload.update(fields)
    paths.write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def delete(conversation_ids: list[str]) -> int:
    removed = 0
    for conversation_id in conversation_ids:
        try:
            _path(conversation_id).unlink()
            removed += 1
        except (OSError, ValueError):
            continue
    return removed


def in_period(rows: list[dict], start: datetime | None, end: datetime | None) -> list[str]:
    """Ids das conversas cuja última atualização cai em [start, end). None deixa o lado aberto."""
    chosen = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row["atualizada"])
        except ValueError:
            continue
        if start is not None and stamp < start:
            continue
        if end is not None and stamp >= end:
            continue
        chosen.append(row["id"])
    return chosen
