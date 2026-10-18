"""Aplica um trecho exato, ou um diff unificado, sem regravar o arquivo inteiro."""

from __future__ import annotations

import difflib
import re

from ollamapilot.config import Settings
from ollamapilot.paths import write_text_atomic
from ollamapilot.tools.base import Tool, schema, text_prop, tool
from ollamapilot.tools.filesystem import _diff, _keep_undo, decode_text
from ollamapilot.tools.paths import resolve_path

_MARKED = re.compile(
    r"<<<<<<< BUSCAR\r?\n(?P<search>.*?)\r?\n=======\r?\n(?P<replace>.*?)\r?\n>>>>>>> SUBSTITUIR",
    re.DOTALL,
)


@tool
class ApplyPatch(Tool):
    name = "aplicar_patch"
    order = 70
    mutates = True
    scope_label = "gravar, editar, criar pasta e desfazer arquivos"
    prompt_hint = (
        "Para alterar um arquivo existente, chame aplicar_patch com blocos buscar/substituir. "
        "Não regrave o arquivo inteiro com reticências ou com o miolo omitido."
    )
    description = (
        "Altera um arquivo de texto UTF-8 por trechos. Prefira blocos, cada um com buscar (trecho atual, único) "
        "e substituir. Também aceita patch no formato BUSCAR/SUBSTITUIR ou diff unificado. "
        "Se um trecho falhar, nada é gravado. Pede autorização, mostra o diff e guarda a versão anterior. "
        "Retorno: {caminho, bytes, blocos}."
    )
    sensitive = True
    grant_key = "arquivos"
    parameters = schema(
        {
            "caminho": text_prop("Caminho absoluto do arquivo existente."),
            "blocos": {
                "type": "array",
                "description": "Lista de objetos {buscar, substituir}. O buscar precisa aparecer uma vez.",
            },
            "patch": text_prop("Opcional, no lugar de blocos. Marcadores BUSCAR/SUBSTITUIR ou diff unificado."),
        },
        ["caminho"],
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, arguments: dict) -> dict:
        path, original, updated, count = self._prepare(arguments)
        return {
            "title": "Aplicar patch",
            "summary": f"{path} ({count} blocos)",
            "detail": f"{path}\n\n{_diff(original, updated, path)}",
        }

    def run(self, arguments: dict) -> dict:
        path, _original, updated, count = self._prepare(arguments)
        if len(updated.encode("utf-8")) > self.settings.max_write_bytes:
            raise ValueError("O arquivo editado excede o limite.")
        backup = _keep_undo(self, path, "aplicar_patch")
        write_text_atomic(path, updated)
        return {"caminho": str(path), "bytes": path.stat().st_size, "blocos": count, "pode_desfazer": backup is not None}

    def _prepare(self, arguments: dict):
        path = resolve_path(arguments.get("caminho"))
        if not path.is_file():
            raise ValueError("O arquivo não existe. Para criar, use gravar_arquivo.")
        data = path.read_bytes()
        if len(data) > self.settings.max_write_bytes:
            raise ValueError("O arquivo é grande demais para editar por aqui.")
        if b"\x00" in data:
            raise ValueError("O arquivo é binário e não pode ser editado.")
        original, _encoding = decode_text(data)
        blocks = _blocks(arguments)
        if not blocks:
            raise ValueError("Informe blocos ou um patch.")
        updated = original
        for search, replace in blocks:
            updated = _replace_unique(updated, search, replace)
        if updated == original:
            raise ValueError("O patch não muda o arquivo.")
        return path, original, updated, len(blocks)


def _blocks(arguments: dict) -> list[tuple[str, str]]:
    raw = arguments.get("blocos")
    blocks: list[tuple[str, str]] = []
    if isinstance(raw, str):
        try:
            import json

            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            search = str(item.get("buscar") or "")
            replace = str(item.get("substituir") if item.get("substituir") is not None else "")
            if search:
                blocks.append((search, replace))
    patch = str(arguments.get("patch") or "")
    if patch.strip():
        marked = [(match.group("search"), match.group("replace")) for match in _MARKED.finditer(patch)]
        blocks.extend(marked or _unified(patch))
    return blocks


def _unified(patch: str) -> list[tuple[str, str]]:
    """Converte hunks @@ em pares buscar/substituir, ignorando os números de linha."""
    if "@@" not in patch:
        return []
    blocks = []
    search: list[str] = []
    replace: list[str] = []
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            if in_hunk and (search or replace):
                blocks.append(("\n".join(search), "\n".join(replace)))
            search, replace, in_hunk = [], [], True
            continue
        if not in_hunk:
            continue
        if line.startswith("-"):
            search.append(line[1:])
        elif line.startswith("+"):
            replace.append(line[1:])
        elif line.startswith("\\"):
            continue
        else:
            body = line[1:] if line.startswith(" ") else line
            search.append(body)
            replace.append(body)
    if in_hunk and (search or replace):
        blocks.append(("\n".join(search), "\n".join(replace)))
    return [(left, right) for left, right in blocks if left]


def _replace_unique(content: str, search: str, replace: str) -> str:
    if not search:
        raise ValueError("Um bloco veio sem o trecho a buscar.")
    if search == replace:
        raise ValueError("Um bloco substitui o trecho por ele mesmo.")
    count = content.count(search)
    if count == 1:
        return content.replace(search, replace, 1)
    if count > 1:
        raise ValueError("Um trecho aparece mais de uma vez. Envie mais linhas de contexto.")
    normalized = content.replace("\r\n", "\n")
    wanted = search.replace("\r\n", "\n")
    if normalized.count(wanted) == 1:
        updated = normalized.replace(wanted, replace.replace("\r\n", "\n"), 1)
        if "\r\n" in content and "\n" in updated:
            updated = updated.replace("\n", "\r\n")
        return updated
    found = _fuzzy_once(content, search)
    if found is None:
        raise ValueError("Um trecho não foi encontrado. Leia o arquivo de novo e copie o texto exato.")
    return content[: found[0]] + replace + content[found[1] :]


def _fuzzy_once(content: str, search: str) -> tuple[int, int] | None:
    if len(search.strip()) < 24:
        return None
    lines = content.splitlines(keepends=True)
    wanted = search.splitlines()
    if not wanted or not lines:
        return None
    width = len(wanted)
    matches = []
    offset = 0
    ends = []
    cursor = 0
    for line in lines:
        cursor += len(line)
        ends.append(cursor)
    for index in range(0, len(lines) - width + 1):
        window = "".join(lines[index : index + width])
        ratio = difflib.SequenceMatcher(None, window, search).ratio()
        if ratio >= 0.92:
            start = ends[index - 1] if index else 0
            matches.append((start, start + len(window)))
            if len(matches) > 1:
                return None
        offset += 1
    return matches[0] if len(matches) == 1 else None
