"""Pasta de trabalho da conversa: o que pode seguir com a concessão e o que pede cartão de novo."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ollamapilot.tools.paths import resolve_path

FILE_MUTATIONS = frozenset(
    {
        "gravar_arquivo",
        "editar_arquivo",
        "aplicar_patch",
        "excluir",
        "mover_arquivo",
        "copiar_arquivo",
        "criar_pasta",
        "desfazer_alteracao",
        "desfazer_plano",
        "git_commit",
    }
)
_COMMANDS = frozenset({"executar_programa", "executar_powershell", "iniciar_sessao"})
_DRIVE = re.compile(r"[A-Za-z]:\\")
_ABS_IN_TEXT = re.compile(r"[A-Za-z]:\\[^\s\"'<>|?*\r\n]+")
_RUN_WORDS = (
    "teste",
    "testar",
    "testes",
    "rode",
    "rodar",
    "execute",
    "executar",
    "verifique",
    "verificar",
    "pytest",
    "unittest",
)


def directory_in_text(text: str) -> Path | None:
    """Primeiro caminho absoluto de diretório escrito pelo usuário. Arquivo não vira pasta."""
    for match in _DRIVE.finditer(text or ""):
        raw = re.split(r"[\r\n\"']", text[match.start() :], maxsplit=1)[0]
        candidate = raw.strip()
        while candidate:
            cleaned = candidate.rstrip(" \t.,);]")
            if len(cleaned) < 3 or not re.match(r"^[A-Za-z]:\\", cleaned):
                break
            path = Path(cleaned)
            try:
                if path.is_dir():
                    return path.resolve()
                if path.exists() and path.is_file():
                    break
            except OSError:
                break
            if " " not in cleaned:
                if path.suffix == "" and not cleaned.endswith("."):
                    return path
                break
            # O caminho pode ter espaço no nome. Encurta uma palavra por vez até achar a pasta.
            candidate = cleaned.rsplit(" ", 1)[0]
    return None


def inside(root: Path | None, path: Path) -> bool:
    if root is None:
        return False
    root_text = os.path.normcase(os.path.abspath(str(root)))
    path_text = os.path.normcase(os.path.abspath(str(path)))
    if path_text == root_text:
        return True
    prefix = root_text if root_text.endswith("\\") else root_text + "\\"
    return path_text.startswith(prefix)


def target_paths(tool, arguments: dict) -> list[Path]:
    try:
        return [path for path in tool.touched_paths(arguments) if isinstance(path, Path)]
    except Exception:
        return []


def command_escapes(root: Path, arguments: dict) -> list[Path]:
    """Pasta de trabalho ou caminho absoluto de arquivo, nos argumentos, que sai da raiz."""
    found: list[Path] = []
    pasta = arguments.get("pasta")
    if pasta not in (None, ""):
        try:
            path = resolve_path(pasta)
        except ValueError:
            path = None
        if path is not None and not inside(root, path):
            found.append(path)
    blobs: list[str] = []
    args = arguments.get("argumentos")
    if isinstance(args, list):
        blobs.extend(str(item) for item in args)
    if arguments.get("script"):
        blobs.append(str(arguments.get("script")))
    for blob in blobs:
        for match in _ABS_IN_TEXT.findall(blob):
            try:
                path = resolve_path(match)
            except ValueError:
                continue
            if not inside(root, path):
                found.append(path)
    return found


def workspace_block(agent, tool, arguments: dict) -> str | None:
    if getattr(tool, "name", "") not in FILE_MUTATIONS:
        return None
    if getattr(agent, "workspace", None):
        return None
    return (
        "Esta conversa não tem pasta de trabalho. Indique a pasta raiz para gravar, editar, excluir, "
        "aplicar patch, mover ou fazer commit. Não inventei uma pasta."
    )


def grant_scope(agent, tool, arguments: dict) -> str:
    """Dentro da raiz, arquivos continuam na chave arquivos. Fora, a concessão fica no caminho."""
    custom = getattr(tool, "grant_key_for", None)
    if callable(custom):
        try:
            scoped = str(custom(arguments) or "")
        except Exception:
            scoped = ""
        if scoped:
            return scoped
    base = tool.grant_key or tool.name
    root = getattr(agent, "workspace", None)
    if base == "arquivos" and root is not None:
        outside = [path for path in target_paths(tool, arguments) if not inside(root, path)]
        if outside:
            return "caminho:" + os.path.normcase(str(outside[0]))
        return "arquivos"
    if tool.name in _COMMANDS and root is not None:
        escaped = command_escapes(root, arguments)
        if escaped:
            return base + ":" + os.path.normcase(str(escaped[0]))
    return base


def annotate_preview(agent, preview: dict, tool, arguments: dict) -> dict:
    shown = dict(preview)
    root = getattr(agent, "workspace", None)
    paths = target_paths(tool, arguments)
    if tool.name in _COMMANDS and root is not None:
        paths = list(paths) + command_escapes(root, arguments)
    if root is None:
        shown["raiz"] = ""
        shown["dentro"] = None
        line = "Pasta de trabalho: (nenhuma nesta conversa)."
    else:
        shown["raiz"] = str(root)
        inside_all = all(inside(root, path) for path in paths) if paths else True
        shown["dentro"] = inside_all
        where = "dentro da pasta de trabalho" if inside_all else "fora da pasta de trabalho"
        line = f"Pasta de trabalho: {root}\nAlvo: {where}."
    detail = str(shown.get("detail") or "")
    shown["detail"] = line + ("\n\n" + detail if detail else "")
    return shown


def has_word(text: str, word: str) -> bool:
    """Casa a palavra inteira, na fronteira de letra ou dígito."""
    token = word.lower().strip()
    if not token:
        return False
    if token.startswith("."):
        pattern = re.escape(token)
    else:
        pattern = r"(?<![0-9A-Za-zÀ-ÿ])" + re.escape(token)
    if token[-1].isalnum() or token[-1] == "_":
        pattern += r"(?![0-9A-Za-zÀ-ÿ])"
    return re.search(pattern, (text or "").lower()) is not None


def asks_to_run(question: str) -> bool:
    folded = question or ""
    if "npm test" in folded.lower() or "py -3 -m unittest" in folded.lower():
        return True
    return any(has_word(folded, word) for word in _RUN_WORDS)


def verify_note(question: str, root: Path, changed: Path) -> str:
    """Uma nota por turno, depois de alterar código dentro da raiz."""
    if not asks_to_run(question):
        return (
            "A alteração está no disco. O pedido foi só gravar: releia o trecho alterado antes de responder "
            "e não execute o projeto."
        )
    command = _verifier(root, changed)
    if not command:
        return (
            "A alteração está no disco. Não há um verificador óbvio nesta pasta. "
            "Releia o trecho alterado antes de responder."
        )
    return (
        "A alteração está no disco. Antes de responder, rode o verificador: "
        f"{command}. Se falhar, corrija e rode de novo, dentro das rodadas disponíveis."
    )


def _verifier(root: Path, changed: Path) -> str | None:
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and str(scripts.get("test") or "").strip():
            return "npm test"
    if (root / "pytest.ini").is_file() or _pyproject_pytest(root / "pyproject.toml"):
        return "pytest"
    if (root / "tests").is_dir() or any(root.glob("test_*.py")):
        return "py -3 -m unittest"
    if changed.suffix.lower() == ".py" and changed.is_file():
        try:
            text = changed.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "__name__" in text and "__main__" in text:
            return f'py -3 "{changed}"'
    return None


def _pyproject_pytest(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return "pytest" in text
