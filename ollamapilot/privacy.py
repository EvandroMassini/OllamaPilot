"""O que pode ir para o disco. A conversa em memória continua completa."""

from __future__ import annotations

import json
import re

from ollamapilot.tools.paths import is_secret

_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{20,}"),
    re.compile(r"\$argon2(?:id|i|d)\$\S+"),
    re.compile(r"\$6\$[A-Za-z0-9./]{8,}\$[A-Za-z0-9./]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk_live_[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)([A-Za-z0-9_]*(?:api[_-]?key|apikey|token|senha|password|passwd|pwd|secret|authorization)[A-Za-z0-9_]*)"
        r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
    ),
    re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+(@)"),
    re.compile(r"(?i)((?:password|pwd|accountkey)\s*=\s*)[^;\s]+"),
)


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Troca chaves, tokens, senhas e os valores conhecidos do cofre por [omitido]."""
    # O número de grupos decide o que sobra: três grupos preservam o nome do campo,
    # dois preservam o prefixo e o sufixo, e um grupo some inteiro.
    for pattern in _PATTERNS:
        groups = pattern.groups
        if groups >= 3:
            text = pattern.sub(lambda match: match.group(1) + match.group(2) + "[omitido]", text)
        elif groups == 2:
            text = pattern.sub(lambda match: match.group(1) + "[omitido]" + match.group(2), text)
        elif groups == 1:
            text = pattern.sub(lambda match: match.group(1) + "[omitido]", text)
        else:
            text = pattern.sub("[omitido]", text)
    for secret in secrets or []:
        if isinstance(secret, str) and len(secret) >= 6 and secret in text:
            text = text.replace(secret, "[omitido]")
    return text


def scrub(text: str, secrets: list[str] | None = None) -> str:
    return redact(_mask_embedded(text), secrets)


def redact_known(value):
    """Tira só os valores do cofre. O restante da conversa, inclusive um arquivo autorizado, permanece."""
    from ollamapilot.secrets import known_values

    secrets = [item for item in known_values() if item]
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, "[omitido]")
        return value
    if isinstance(value, list):
        return [redact_known(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_known(item) for key, item in value.items()}
    return value


def for_disk(payload: dict, secrets: list[str] | None = None) -> dict:
    """Cópia da sessão ou da conversa sem segredos. O objeto original não muda."""
    try:
        cloned = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        cloned = dict(payload)
    if not isinstance(cloned, dict):
        return {}
    bag = list(secrets or [])
    for key in ("transcript", "display"):
        items = cloned.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ("content", "thinking"):
                if isinstance(item.get(field), str):
                    item[field] = scrub(item[field], bag)
    return cloned


def _mask_embedded(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        masked = _mask_payload(data)
        if masked is data:
            return text
        return json.dumps(masked, ensure_ascii=False)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return text
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return text
    masked = _mask_payload(data)
    if masked is data:
        return text
    return text[:start] + json.dumps(masked, ensure_ascii=False) + text[end + 1 :]


def _mask_payload(data):
    if isinstance(data, list):
        changed = False
        items = []
        for item in data:
            masked = _mask_payload(item)
            changed = changed or masked is not item
            items.append(masked)
        return items if changed else data
    if not isinstance(data, dict):
        return data
    path = str(data.get("caminho") or data.get("arquivo") or "")
    secret = False
    if path:
        try:
            from pathlib import Path

            secret = is_secret(Path(path))
        except (OSError, ValueError):
            secret = False
    if not secret:
        return data
    masked = dict(data)
    for field in ("conteudo", "texto", "trecho", "stdout", "stderr"):
        value = masked.get(field)
        if isinstance(value, str) and value and not value.startswith("[conteúdo omitido"):
            masked[field] = f"[conteúdo omitido: {PathName(path)}, {len(value)} caracteres]"
    return masked


def PathName(path: str) -> str:
    name = path.replace("/", "\\").rstrip("\\")
    return name.split("\\")[-1] or path
