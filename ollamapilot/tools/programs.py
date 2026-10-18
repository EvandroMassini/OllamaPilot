from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from pathlib import Path

from ollamapilot.config import Settings
from ollamapilot.process_run import child_environment, run_process
from ollamapilot.tools.base import Tool, bool_prop, int_prop, schema, text_prop, tool
from ollamapilot.tools.paths import clip, resolve_path, risk_note


@tool
class RunProgram(Tool):
    name = "executar_programa"
    group = "programas"
    repeatable = True
    order = 110
    scope_label = "executar qualquer programa"
    description = (
        "Executa um programa com argumentos em lista (não é shell) e devolve {exit_code, sucesso, stdout, stderr, mensagem}. "
        "Pede autorização. entrada vai para o stdin. Programa com janela: aguardar=false, devolve {pid}. "
        "Para conversar com um programa interativo, use iniciar_sessao."
    )
    sensitive = True
    parameters = schema(
        {
            "programa": text_prop("Nome no PATH, como git, ou caminho absoluto do executável."),
            "argumentos": {"type": "array", "items": {"type": "string"}, "description": "Até 40, como [\"--version\"]."},
            "pasta": text_prop("Opcional. Pasta de trabalho absoluta."),
            "entrada": text_prop("Opcional. Texto enviado ao stdin; o stdin é fechado depois."),
            "ambiente": {
                "type": "object",
                "description": "Opcional. Variáveis extras, como {\"MODO\": \"teste\"}. ${cofre:nome} lê do cofre do Windows.",
            },
            "timeout_segundos": int_prop("Opcional. Até 7200; padrão das configurações."),
            "aguardar": bool_prop("Opcional. false só inicia, sem esperar o fim."),
        },
        ["programa"],
    )

    def __init__(self, settings: Settings, cancel) -> None:
        self.settings = settings
        self.cancel = cancel

    def preview(self, arguments: dict) -> dict:
        program = _program(str(arguments.get("programa") or ""))
        args = _args(arguments.get("argumentos"), program)
        from ollamapilot.tools.database import refuse_postgres_shell

        refuse_postgres_shell(self.settings, program, " ".join(args))
        cwd = _cwd(arguments.get("pasta"))
        environment = environment_argument(arguments.get("ambiente"))
        line = subprocess.list2cmdline([program, *args])
        wait = _wait(arguments)
        mode = f"Espera terminar, até {self._timeout(arguments)} s." if wait else "Só inicia o programa, sem esperar o fim."
        detail = f"Programa: {program}\nPasta: {cwd or os.getcwd()}\n{mode}\n\n{line}"
        detail += describe_environment(environment)
        stdin = arguments.get("entrada")
        if stdin not in (None, "") and wait:
            shown, cut = clip(str(stdin), 2000)
            detail += f"\n\nEntrada (stdin):\n{shown}" + ("\n…" if cut else "")
        note = risk_note(resolve_path(program)) if _is_absolute(program) else ""
        if note:
            detail = note + "\n" + detail
        return {"title": "Executar programa", "summary": line, "detail": detail}

    def run(self, arguments: dict) -> dict:
        program = _program(str(arguments.get("programa") or ""))
        args = _args(arguments.get("argumentos"), program)
        from ollamapilot.tools.database import refuse_postgres_shell

        refuse_postgres_shell(self.settings, program, " ".join(args))
        cwd = _cwd(arguments.get("pasta"))
        environment = resolve_environment(environment_argument(arguments.get("ambiente")))
        if not _wait(arguments):
            return _start_only(program, args, cwd, environment)
        timeout = self._timeout(arguments)
        stdin = arguments.get("entrada")
        result = run_process(
            [program, *args],
            cwd,
            timeout,
            cancel=self.cancel,
            input_text=None if stdin in (None, "") else str(stdin),
            env=environment,
        )
        result["programa"] = program
        result["sucesso"] = result["exit_code"] == 0 and not result["timed_out"] and not result["cancelled"]
        if result["cancelled"]:
            result["mensagem"] = "Interrompido pelo usuário; o programa foi encerrado."
        elif result["timed_out"]:
            result["mensagem"] = f"O programa não terminou em {timeout} s e foi encerrado. Para programa com janela, use aguardar=false."
        elif result["exit_code"] != 0:
            result["mensagem"] = f"O programa terminou com código {result['exit_code']}, o que normalmente indica falha."
        else:
            result["mensagem"] = "O programa terminou com código 0."
        return result

    def _timeout(self, arguments: dict) -> int:
        explicit = arguments.get("timeout_segundos")
        if explicit not in (None, ""):
            try:
                timeout = int(explicit)
            except (TypeError, ValueError):
                timeout = int(self.settings.command_timeout)
        else:
            timeout = registered_command_timeout(self.settings, str(arguments.get("programa") or ""))
            if timeout is None:
                timeout = int(self.settings.command_timeout)
        return min(max(timeout, 1), 7200)


def registered_command_timeout(settings: Settings, program: str) -> int | None:
    """Tempo de comando do programa cadastrado, quando o nome ou o caminho coincidem."""
    needle = program.strip().lower()
    if not needle:
        return None
    for item in settings.programs or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("nome") or "").strip().lower()
        path = str(item.get("caminho") or "").strip()
        if needle in {name, path.lower(), Path(path).name.lower()}:
            try:
                return int(item.get("tempo_comando"))
            except (TypeError, ValueError):
                return None
    return None


def _wait(arguments: dict) -> bool:
    value = arguments.get("aguardar")
    return True if value is None else bool(value)


def _start_only(program: str, args: list[str], cwd: str | None, environment: dict | None = None) -> dict:
    proc = subprocess.Popen(
        [program, *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=child_environment(environment),
    )
    # Não espera o programa de janela terminar. O 1,5 s só distingue um processo
    # que continua aberto de um que saiu na hora, com código de erro.
    deadline = time.time() + 1.5
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    code = proc.poll()
    if code is None:
        return {
            "programa": program,
            "pid": proc.pid,
            "em_execucao": True,
            "mensagem": "O processo foi iniciado e continua em execução. Isso não confirma que a tarefa foi feita; confira pela janela.",
        }
    return {
        "programa": program,
        "pid": proc.pid,
        "em_execucao": False,
        "exit_code": code,
        "mensagem": (
            "O processo terminou logo após iniciar com código 0. Alguns programas com janela fazem isso e a janela continua aberta; confira com listar_janelas."
            if code == 0
            else f"O processo terminou logo após iniciar com código {code}, o que normalmente indica falha."
        ),
    }


def _is_absolute(value: str) -> bool:
    return bool(value[:3]) and len(value) > 2 and value[1] == ":"


def _program(value: str) -> str:
    value = value.strip().strip('"')
    if not value or any(char in value for char in "\r\n\x00"):
        raise ValueError("Programa inválido.")
    if re.fullmatch(r"[A-Za-z]:\\?", value):
        raise ValueError("Informe o programa, como notepad.exe, e não só a letra do disco.")
    if _is_absolute(value) or value.startswith("\\\\"):
        path = resolve_path(value)
        if not path.is_file():
            raise ValueError("Programa não encontrado.")
        return str(path)
    if "/" in value or "\\" in value:
        raise ValueError("Informe o nome no PATH ou o caminho absoluto do executável.")
    found = shutil.which(value)
    if not found:
        raise ValueError(f"Não encontrei '{value}' no PATH desta máquina.")
    return found


_BATCH_UNSAFE = set('&|<>^%!"\r\n')


def _args(value, program: str = "") -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("Os argumentos precisam ser uma lista.")
    if len(value) > 40:
        raise ValueError("Há argumentos demais.")
    batch = program.lower().endswith((".bat", ".cmd"))
    output = []
    for item in value:
        text = str(item)
        if "\x00" in text or len(text) > 4000:
            raise ValueError("Argumento inválido.")
        if batch and any(char in _BATCH_UNSAFE for char in text):
            # O Windows passa a linha de um .bat pelo cmd.exe, que leria & ou | como um segundo comando.
            raise ValueError(
                f"O argumento {text[:80]!r} tem um caractere que o cmd.exe interpreta (& | < > ^ % ! \"). "
                "Esse programa é um .bat/.cmd; use executar_powershell ou outro executável."
            )
        output.append(text)
    return output


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SECRET_REF = re.compile(r"^\$\{cofre:([A-Za-z0-9_-]{1,40})\}$")


def environment_argument(value) -> dict[str, str]:
    if value in (None, "", {}):
        return {}
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("ambiente precisa ser um objeto JSON.") from error
    if not isinstance(value, dict):
        raise ValueError("ambiente precisa ser um objeto com nome e valor.")
    if len(value) > 30:
        raise ValueError("São variáveis demais; envie até 30.")
    clean = {}
    for key, item in value.items():
        name = str(key)
        text = "" if item is None else str(item)
        if not _ENV_NAME.match(name) or "\x00" in text or len(text) > 4000:
            raise ValueError(f"Variável de ambiente inválida: {name[:40]}")
        clean[name] = text
    return clean


def resolve_environment(environment: dict[str, str]) -> dict[str, str]:
    """${cofre:nome} vira o valor guardado só na hora de iniciar o processo; o modelo nunca vê o segredo."""
    if not environment:
        return {}
    from ollamapilot.secrets import current

    store = current()
    resolved = {}
    for key, value in environment.items():
        match = _SECRET_REF.match(value.strip())
        if not match:
            resolved[key] = value
            continue
        secret = store.get(match.group(1))
        if not secret:
            raise ValueError(f"O segredo '{match.group(1)}' não está no cofre do Windows.")
        resolved[key] = secret
    return resolved


def describe_environment(environment: dict[str, str]) -> str:
    if not environment:
        return ""
    rows = [f"{key}={clip(value, 200)[0]}" for key, value in environment.items()]
    return "\n\nVariáveis de ambiente:\n" + "\n".join(rows)


def _cwd(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = resolve_path(text)
    if not path.is_dir():
        raise ValueError("A pasta de trabalho não existe.")
    return str(path)
