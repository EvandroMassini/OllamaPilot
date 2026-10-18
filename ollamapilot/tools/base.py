from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Callable


class ToolContext:
    """O que uma ferramenta pode usar sem o agent.py conhecer a classe."""

    def __init__(self, settings, client, cancel: Callable[[], bool] | None, agent=None) -> None:
        self.settings = settings
        self.client = client
        self.cancel = cancel or (lambda: False)
        self.agent = agent


_PROVIDERS: dict[str, type] = {}


def tool(cls):
    """Registra a ferramenta. Um arquivo novo entra no agente sem editar o laço."""
    if not getattr(cls, "name", ""):
        raise TypeError(f"{cls.__name__} não declarou name.")
    previous = _PROVIDERS.get(cls.name)
    if previous is not None and previous is not cls:
        raise ValueError(f"Ferramenta duplicada: {cls.name}")
    _PROVIDERS[cls.name] = cls
    return cls


class Tool:
    name = ""
    description = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    sensitive = False
    grant_key: str | None = None
    scope_label = ""
    prompt_hint = ""
    mutates = False
    order = 500
    group = "arquivos"
    # Repetir a mesma chamada é um passo novo (ler a saída de novo, rodar os testes de novo).
    repeatable = False

    def __init__(self, ctx: ToolContext | None = None) -> None:
        self.ctx = ctx

    @classmethod
    def available(cls, ctx: ToolContext) -> bool:
        return True

    @classmethod
    def create(cls, ctx: ToolContext):
        signature = inspect.signature(cls.__init__)
        kwargs = {}
        if "settings" in signature.parameters:
            kwargs["settings"] = ctx.settings
        if "client" in signature.parameters:
            kwargs["client"] = ctx.client
        if "cancel" in signature.parameters:
            kwargs["cancel"] = ctx.cancel
        if "ctx" in signature.parameters:
            kwargs["ctx"] = ctx
        instance = cls(**kwargs)
        instance.ctx = ctx
        return instance

    def touched_paths(self, arguments: dict) -> list[Path]:
        raw = arguments.get("caminho")
        if not raw:
            return []
        try:
            from ollamapilot.tools.paths import resolve_path

            return [resolve_path(raw)]
        except Exception:
            return []

    def needs_approval(self, arguments: dict) -> bool:
        return self.sensitive

    def preview(self, arguments: dict) -> dict:
        return {
            "title": self.name,
            "summary": self.name,
            "detail": "",
        }

    def run(self, arguments: dict) -> dict:
        raise NotImplementedError


def schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def text_prop(description: str) -> dict:
    return {"type": "string", "description": description}


def int_prop(description: str) -> dict:
    return {"type": "integer", "description": description}


def bool_prop(description: str) -> dict:
    return {"type": "boolean", "description": description}


def validate_arguments(parameters: dict, arguments: dict) -> tuple[dict, list[str]]:
    """Confere obrigatórios e tipos do schema. Converte só o que é inequívoco, como "5" em 5."""
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(properties, dict):
        return dict(arguments), []
    cleaned = dict(arguments)
    errors: list[str] = []
    for name in parameters.get("required") or []:
        value = cleaned.get(name)
        if value is None or (isinstance(value, str) and not value.strip() and name != "conteudo"):
            errors.append(f"falta o argumento obrigatório '{name}'")
    for name, spec in properties.items():
        if name not in cleaned or cleaned[name] is None or not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        value = cleaned[name]
        converted, ok = _convert(expected, value)
        if ok:
            cleaned[name] = converted
        else:
            errors.append(f"'{name}' deve ser {_TYPE_NAMES.get(expected, expected)}, veio {_type_name(value)}")
    return cleaned, errors


_TYPE_NAMES = {
    "string": "texto",
    "integer": "número inteiro",
    "number": "número",
    "boolean": "verdadeiro ou falso",
    "array": "lista",
    "object": "objeto",
}


def _type_name(value: object) -> str:
    if isinstance(value, bool):
        return "verdadeiro/falso"
    if isinstance(value, int):
        return "número inteiro"
    if isinstance(value, float):
        return "número"
    if isinstance(value, str):
        return "texto"
    if isinstance(value, list):
        return "lista"
    if isinstance(value, dict):
        return "objeto"
    return type(value).__name__


def _convert(expected: object, value: object) -> tuple[object, bool]:
    if expected == "string":
        if isinstance(value, str):
            return value, True
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value), True
        return value, False
    if expected == "integer":
        if isinstance(value, bool):
            return value, False
        if isinstance(value, int):
            return value, True
        if isinstance(value, float) and value.is_integer():
            return int(value), True
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip()), True
        return value, False
    if expected == "number":
        if isinstance(value, bool):
            return value, False
        if isinstance(value, (int, float)):
            return value, True
        try:
            return float(str(value).strip()), True
        except ValueError:
            return value, False
    if expected == "boolean":
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "sim", "não", "nao"}:
            return value.strip().lower() in {"true", "sim"}, True
        return value, False
    if expected == "array":
        if isinstance(value, list):
            return value, True
        if isinstance(value, str):
            # As ferramentas aceitam a lista em JSON ou um item solto em texto.
            return value, True
        if isinstance(value, dict):
            return [value], True
        return value, False
    if expected == "object":
        if isinstance(value, dict):
            return value, True
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return value, False
            return (parsed, True) if isinstance(parsed, dict) else (value, False)
        return value, False
    return value, True
