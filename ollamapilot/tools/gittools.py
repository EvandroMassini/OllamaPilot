"""Git só de leitura na pasta de trabalho, e commit com autorização, sem push."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ollamapilot.tools.base import Tool, schema, text_prop, tool
from ollamapilot.tools.paths import resolve_path
from ollamapilot.workspace import inside

_FORBIDDEN = ("--force", "--amend", "--hard", "push", "reset")


def _workspace(tool) -> Path:
    agent = getattr(getattr(tool, "ctx", None), "agent", None)
    root = getattr(agent, "workspace", None) if agent is not None else None
    if root is None:
        raise ValueError(
            "Esta conversa não tem pasta de trabalho. Indique a pasta antes de usar o Git. Não procuro outro repositório."
        )
    return Path(root)


def _repo(tool, arguments: dict) -> Path:
    root = _workspace(tool)
    raw = arguments.get("caminho") or str(root)
    path = resolve_path(raw)
    if not inside(root, path):
        raise ValueError("O Git só atua na pasta de trabalho desta conversa. Não procuro outro repositório.")
    if not path.exists():
        raise ValueError("A pasta não existe. Não procuro outro repositório.")
    completed = _git(["-C", str(path), "rev-parse", "--show-toplevel"], path)
    if completed.returncode != 0:
        raise ValueError("Essa pasta não é um repositório Git. Não procuro outro repositório.")
    top = Path(completed.stdout.strip())
    if not inside(root, top):
        raise ValueError("A raiz desse repositório está fora da pasta de trabalho. Não uso esse repositório.")
    return top


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _result(completed: subprocess.CompletedProcess[str], **extra) -> dict:
    payload = {
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[:16000],
        "stderr": (completed.stderr or "")[:4000],
    }
    payload.update(extra)
    if completed.returncode != 0:
        payload["erro"] = (completed.stderr or completed.stdout or "O Git recusou o comando.").strip()[:2000]
    return payload


def _paths_from_status(repo: Path) -> list[str]:
    completed = _git(["status", "--porcelain", "-uall"], repo)
    if completed.returncode != 0:
        raise ValueError((completed.stderr or "Não foi possível listar o status.").strip())
    found = []
    for line in (completed.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if path:
            found.append(path)
    return found


def _refuse_flags(message: str, paths: list[str]) -> str | None:
    if not str(message or "").strip():
        return "A mensagem do commit está vazia. O commit não rodou."
    lowered = message.lower()
    if "--force" in lowered or "--amend" in lowered:
        return "A mensagem contém --force ou --amend. O commit não rodou."
    for path in paths:
        text = str(path).strip().lower()
        if text.startswith("-") or any(flag in text for flag in _FORBIDDEN):
            return "Há argumento de push, amend, force ou reset. O commit não rodou."
    return None


@tool
class GitStatus(Tool):
    name = "git_status"
    group = "git"
    order = 210
    description = (
        "Mostra o status do Git da pasta de trabalho, ou de um repositório cuja raiz esteja dentro dela. "
        "Não procura outro repositório. Retorno: {raiz, stdout, stderr, exit_code}."
    )
    parameters = schema({"caminho": text_prop("Opcional. Pasta do repositório, dentro da pasta de trabalho.")}, [])

    def run(self, arguments: dict) -> dict:
        repo = _repo(self, arguments)
        completed = _git(["status", "--porcelain", "-uall", "--branch"], repo)
        return _result(completed, raiz=str(repo))


@tool
class GitDiff(Tool):
    name = "git_diff"
    group = "git"
    order = 211
    description = (
        "Mostra o diff do Git na pasta de trabalho, sem alterar nada. "
        "Retorno: {raiz, stdout, stderr, exit_code}."
    )
    parameters = schema({"caminho": text_prop("Opcional. Pasta do repositório, dentro da pasta de trabalho.")}, [])

    def run(self, arguments: dict) -> dict:
        repo = _repo(self, arguments)
        completed = _git(["diff", "--no-ext-diff", "--stat", "--patch"], repo)
        return _result(completed, raiz=str(repo))


@tool
class GitLog(Tool):
    name = "git_log"
    group = "git"
    order = 212
    description = "Mostra os commits recentes do repositório na pasta de trabalho. Retorno: {raiz, stdout, stderr, exit_code}."
    parameters = schema(
        {
            "caminho": text_prop("Opcional. Pasta do repositório, dentro da pasta de trabalho."),
            "limite": {"type": "integer", "description": "Opcional. De 1 a 30. Padrão 15."},
        },
        [],
    )

    def run(self, arguments: dict) -> dict:
        repo = _repo(self, arguments)
        try:
            limit = int(arguments.get("limite") or 15)
        except (TypeError, ValueError):
            limit = 15
        limit = min(max(limit, 1), 30)
        completed = _git(["log", f"-n{limit}", "--oneline", "--decorate"], repo)
        return _result(completed, raiz=str(repo))


@tool
class GitCommit(Tool):
    name = "git_commit"
    group = "git"
    order = 213
    sensitive = True
    mutates = True
    scope_label = "gravar um commit nesta pasta de trabalho, sem push"
    description = (
        "Grava um commit na pasta de trabalho. Pede autorização, mostra a mensagem e os caminhos. "
        "Recusa mensagem vazia, caminho fora da pasta, push, amend, force e reset. Não configura o usuário do Git. "
        "Retorno: {raiz, caminhos, stdout, stderr, exit_code}."
    )
    parameters = schema(
        {
            "mensagem": text_prop("Mensagem do commit, obrigatória."),
            "caminhos": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Opcional. Caminhos relativos ao repositório. Sem isto, entram os arquivos já modificados.",
            },
            "caminho": text_prop("Opcional. Pasta do repositório, dentro da pasta de trabalho."),
        },
        ["mensagem"],
    )

    def touched_paths(self, arguments: dict) -> list[Path]:
        root = _workspace(self)
        repo = root
        raw = arguments.get("caminho")
        if raw:
            repo = resolve_path(raw)
        paths = []
        for item in arguments.get("caminhos") or []:
            text = str(item)
            if text.startswith("-") or ":" in text[:3]:
                continue
            candidate = Path(text)
            if candidate.is_absolute():
                paths.append(resolve_path(text))
            else:
                paths.append((repo / text).resolve())
        return paths or [repo]

    def preview(self, arguments: dict) -> dict:
        repo = _repo(self, arguments)
        message = str(arguments.get("mensagem") or "")
        paths = _commit_paths(repo, arguments)
        refused = _refuse_flags(message, paths)
        if refused:
            raise ValueError(refused)
        listed = "\n".join(paths) if paths else "(nenhum arquivo modificado)"
        return {
            "title": "Commit",
            "summary": message.strip()[:160] or "(sem mensagem)",
            "detail": f"Repositório: {repo}\n\nMensagem:\n{message.strip()}\n\nCaminhos:\n{listed}",
        }

    def run(self, arguments: dict) -> dict:
        repo = _repo(self, arguments)
        message = str(arguments.get("mensagem") or "")
        paths = _commit_paths(repo, arguments)
        refused = _refuse_flags(message, paths)
        if refused:
            return {"erro": refused, "rodou": False}
        if not paths:
            return {"erro": "Não há arquivos para o commit.", "rodou": False, "raiz": str(repo)}
        for relative in paths:
            target = (repo / relative).resolve()
            if not inside(_workspace(self), target):
                return {"erro": f"O caminho sai da pasta de trabalho: {relative}. O commit não rodou.", "rodou": False}
        added = _git(["add", "--", *paths], repo)
        if added.returncode != 0:
            return _result(added, raiz=str(repo), caminhos=paths, rodou=False)
        completed = _git(["commit", "-m", message.strip(), "--", *paths], repo)
        payload = _result(completed, raiz=str(repo), caminhos=paths, rodou=completed.returncode == 0)
        return payload


def _commit_paths(repo: Path, arguments: dict) -> list[str]:
    raw = arguments.get("caminhos")
    if isinstance(raw, list) and raw:
        return [str(item).strip() for item in raw if str(item).strip()]
    return _paths_from_status(repo)
