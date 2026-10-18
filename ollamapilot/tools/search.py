"""Busca por texto e por nome numa pasta, sem índice e sem embeddings."""

from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path

from ollamapilot.project_index import _SKIP_DIRS, _ignore_rules, _ignored
from ollamapilot.tools.base import Tool, bool_prop, int_prop, schema, text_prop, tool
from ollamapilot.tools.paths import is_secret, resolve_path

MAX_FILES = 20_000
MAX_SECONDS = 20
MAX_TEXT_BYTES = 2_000_000
_BINARY = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".o", ".lib", ".pdb", ".zip", ".7z", ".rar", ".gz",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".pdf", ".docx", ".xlsx", ".pptx", ".class", ".jar", ".pyc", ".sqlite", ".db", ".iso", ".msi",
}


def _root(arguments: dict) -> Path:
    root = resolve_path(arguments.get("pasta"))
    if not root.is_dir():
        raise ValueError("Informe uma pasta que exista.")
    return root


def _walk(root: Path, cancel):
    """Arquivos da pasta, pulando dependências, pastas ocultas e o que o .gitignore da raiz ignora."""
    rules = _ignore_rules(root)
    started = time.monotonic()
    count = 0
    for folder, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS and not name.startswith("."))
        for name in sorted(filenames):
            if cancel():
                return
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if rules and _ignored(relative, rules):
                continue
            count += 1
            if count > MAX_FILES or time.monotonic() - started > MAX_SECONDS:
                yield None
                return
            yield path


def _matches_name(relative: str, pattern: str) -> bool:
    if not pattern:
        return True
    name = relative.rsplit("/", 1)[-1]
    if "/" not in pattern:
        return fnmatch.fnmatch(name.lower(), pattern.lower())
    simple = pattern.replace("**/", "")
    return fnmatch.fnmatch(relative.lower(), pattern.lower()) or fnmatch.fnmatch(relative.lower(), simple.lower())


@tool
class SearchText(Tool):
    name = "buscar_texto"
    order = 22
    prompt_hint = (
        "Para achar onde algo aparece no código, use buscar_texto e leia só as linhas devolvidas com ler_arquivo inicio e fim."
    )
    description = (
        "Procura um texto ou expressão regular nos arquivos de texto de uma pasta, como o grep. "
        "Pula node_modules, .git, pastas ocultas, binários e arquivos de senha. "
        "Retorno: {resultados: [{arquivo, linha, texto}], arquivos_lidos, truncated}."
    )
    parameters = schema(
        {
            "pasta": text_prop("Pasta absoluta onde procurar."),
            "padrao": text_prop("Texto ou expressão regular."),
            "arquivos": text_prop("Opcional. Filtro de nome, como *.py ou src/**/*.ts."),
            "regex": bool_prop("Opcional. false trata o padrão como texto literal. Padrão true."),
            "maiusculas": bool_prop("Opcional. true diferencia maiúsculas. Padrão false."),
            "limite": int_prop("Opcional. Máximo de resultados, até 300. Padrão 80."),
        },
        ["pasta", "padrao"],
    )

    def __init__(self, cancel) -> None:
        self.cancel = cancel

    def run(self, arguments: dict) -> dict:
        root = _root(arguments)
        raw = str(arguments.get("padrao") or "")
        if not raw:
            raise ValueError("Informe o texto a procurar.")
        flags = 0 if arguments.get("maiusculas") else re.IGNORECASE
        literal = arguments.get("regex") is False
        try:
            pattern = re.compile(re.escape(raw) if literal else raw, flags)
        except re.error as error:
            raise ValueError(f"Expressão regular inválida: {error}. Use regex=false para texto literal.") from error
        names = str(arguments.get("arquivos") or "").strip().replace("\\", "/")
        limit = min(max(int(arguments.get("limite") or 80), 1), 300)
        results: list[dict] = []
        scanned = 0
        skipped_secret = 0
        incomplete = False
        for path in _walk(root, self.cancel):
            if path is None:
                incomplete = True
                break
            relative = path.relative_to(root).as_posix()
            if not _matches_name(relative, names) or path.suffix.lower() in _BINARY:
                continue
            if is_secret(path):
                skipped_secret += 1
                continue
            try:
                if path.stat().st_size > MAX_TEXT_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            scanned += 1
            text = data.decode("utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line[:4000]):
                    results.append({"arquivo": str(path), "linha": number, "texto": line.strip()[:300]})
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                incomplete = True
                break
        result = {"pasta": str(root), "resultados": results, "arquivos_lidos": scanned, "truncated": incomplete}
        if skipped_secret:
            result["arquivos_de_segredo_pulados"] = skipped_secret
        if not results:
            result["mensagem"] = "Nenhuma ocorrência."
        return result


@tool
class FindFiles(Tool):
    name = "localizar_arquivos"
    order = 24
    description = (
        "Lista arquivos de uma pasta e subpastas cujo nome combina com um padrão, como *.py, teste_*.js ou src/**/*.cs. "
        "Pula node_modules, .git e pastas ocultas. Retorno: {arquivos: [{caminho, bytes}], total, truncated}."
    )
    parameters = schema(
        {
            "pasta": text_prop("Pasta absoluta onde procurar."),
            "padrao": text_prop("Padrão do nome, como *.py. Vazio lista tudo."),
            "limite": int_prop("Opcional. Máximo de arquivos, até 500. Padrão 200."),
        },
        ["pasta"],
    )

    def __init__(self, cancel) -> None:
        self.cancel = cancel

    def run(self, arguments: dict) -> dict:
        root = _root(arguments)
        pattern = str(arguments.get("padrao") or "").strip().replace("\\", "/")
        limit = min(max(int(arguments.get("limite") or 200), 1), 500)
        found: list[dict] = []
        total = 0
        incomplete = False
        for path in _walk(root, self.cancel):
            if path is None:
                incomplete = True
                break
            relative = path.relative_to(root).as_posix()
            if not _matches_name(relative, pattern):
                continue
            total += 1
            if len(found) < limit:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = None
                found.append({"caminho": str(path), "bytes": size})
        return {"pasta": str(root), "arquivos": found, "total": total, "truncated": incomplete or total > limit}
