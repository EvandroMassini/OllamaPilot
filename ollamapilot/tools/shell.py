"""PowerShell para o que pede pipe, filtro ou cmdlet. O cartão mostra o script inteiro."""

from __future__ import annotations

import base64
import os

from ollamapilot.config import Settings
from ollamapilot.process_run import run_process
from ollamapilot.tools.base import Tool, int_prop, schema, text_prop, tool
from ollamapilot.tools.programs import _cwd, describe_environment, environment_argument, resolve_environment

MAX_SCRIPT = 8000
_PREFIX = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$OutputEncoding=[Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';\n"


@tool
class RunPowerShell(Tool):
    name = "executar_powershell"
    group = "programas"
    order = 111
    repeatable = True
    sensitive = True
    scope_label = "executar comandos do PowerShell"
    description = (
        "Executa um script do Windows PowerShell (pipes, filtros, cmdlets) e devolve {exit_code, sucesso, stdout, stderr}. "
        "Pede autorização mostrando o script inteiro. Para um programa só, prefira executar_programa."
    )
    parameters = schema(
        {
            "script": text_prop("Comandos do PowerShell, até 8.000 caracteres."),
            "pasta": text_prop("Opcional. Pasta de trabalho absoluta."),
            "ambiente": {"type": "object", "description": "Opcional. Variáveis extras; ${cofre:nome} lê do cofre."},
            "timeout_segundos": int_prop("Opcional. Até 3600; padrão das configurações."),
        },
        ["script"],
    )

    def __init__(self, settings: Settings, cancel) -> None:
        self.settings = settings
        self.cancel = cancel

    def _script(self, arguments: dict) -> str:
        script = str(arguments.get("script") or "").strip()
        if not script:
            raise ValueError("Informe o script.")
        if len(script) > MAX_SCRIPT or "\x00" in script:
            raise ValueError(f"O script precisa ter até {MAX_SCRIPT} caracteres.")
        return script

    def _timeout(self, arguments: dict) -> int:
        try:
            value = int(arguments.get("timeout_segundos") or self.settings.command_timeout)
        except (TypeError, ValueError):
            value = int(self.settings.command_timeout)
        return min(max(value, 1), 3600)

    def preview(self, arguments: dict) -> dict:
        script = self._script(arguments)
        from ollamapilot.tools.database import refuse_postgres_shell

        refuse_postgres_shell(self.settings, "powershell", script)
        cwd = _cwd(arguments.get("pasta"))
        detail = f"Pasta: {cwd or os.getcwd()}\nAté {self._timeout(arguments)} s.\n\n{script}"
        detail += describe_environment(environment_argument(arguments.get("ambiente")))
        first = script.splitlines()[0][:160]
        return {"title": "Executar PowerShell", "summary": first, "detail": detail}

    def run(self, arguments: dict) -> dict:
        script = self._script(arguments)
        from ollamapilot.tools.database import refuse_postgres_shell

        refuse_postgres_shell(self.settings, "powershell", script)
        cwd = _cwd(arguments.get("pasta"))
        environment = resolve_environment(environment_argument(arguments.get("ambiente")))
        encoded = base64.b64encode((_PREFIX + script).encode("utf-16-le")).decode("ascii")
        shell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.isfile(shell):
            shell = "powershell.exe"
        timeout = self._timeout(arguments)
        result = run_process(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            cwd,
            timeout,
            cancel=self.cancel,
            env=environment,
        )
        result["sucesso"] = result["exit_code"] == 0 and not result["timed_out"] and not result["cancelled"]
        if result["cancelled"]:
            result["mensagem"] = "Interrompido pelo usuário; o PowerShell foi encerrado."
        elif result["timed_out"]:
            result["mensagem"] = f"O script não terminou em {timeout} s e foi encerrado."
        elif result["exit_code"] != 0:
            result["mensagem"] = f"O PowerShell terminou com código {result['exit_code']}; leia o stderr."
        else:
            result["mensagem"] = "O PowerShell terminou com código 0."
        return result
