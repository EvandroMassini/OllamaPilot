"""Sessões de programa: iniciar uma vez com autorização, depois mandar texto e ler a saída."""

from __future__ import annotations

import os
import subprocess

from ollamapilot.sessions import MANAGER
from ollamapilot.tools.base import Tool, bool_prop, int_prop, schema, text_prop, tool
from ollamapilot.tools.paths import clip, resolve_path, risk_note
from ollamapilot.tools.programs import (
    _args,
    _cwd,
    _is_absolute,
    _program,
    describe_environment,
    environment_argument,
    resolve_environment,
)


def _seconds(value, default: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, 0.0), high)


class SessionTool(Tool):
    group = "terminal"
    repeatable = True

    def __init__(self, cancel) -> None:
        self.cancel = cancel


@tool
class StartSession(SessionTool):
    name = "iniciar_sessao"
    order = 112
    sensitive = True
    grant_key = "sessao_programa"
    scope_label = "iniciar programas interativos"
    prompt_hint = (
        "Programa interativo (REPL, cliente de banco, servidor, instalador que pergunta): iniciar_sessao, depois "
        "enviar_entrada e ler_saida; encerre com encerrar_sessao. Python interativo precisa de -i."
    )
    description = (
        "Inicia um programa que continua rodando, com stdin e stdout ligados ao agente. Pede autorização uma vez; "
        "depois enviar_entrada e ler_saida usam a sessão sem novo cartão. Retorno: {sessao, pid, saida, em_execucao}."
    )
    parameters = schema(
        {
            "programa": text_prop("Nome no PATH ou caminho absoluto do executável."),
            "argumentos": {"type": "array", "items": {"type": "string"}, "description": "Até 40, como [\"-i\"]."},
            "pasta": text_prop("Opcional. Pasta de trabalho absoluta."),
            "ambiente": {"type": "object", "description": "Opcional. Variáveis extras; ${cofre:nome} lê do cofre."},
            "esperar_texto": text_prop("Opcional. Espera este texto (ou regex) na saída inicial, como >>> ."),
            "esperar_segundos": int_prop("Opcional. Tempo da leitura inicial, até 120. Padrão 3."),
        },
        ["programa"],
    )

    def preview(self, arguments: dict) -> dict:
        program = _program(str(arguments.get("programa") or ""))
        args = _args(arguments.get("argumentos"), program)
        cwd = _cwd(arguments.get("pasta"))
        line = subprocess.list2cmdline([program, *args])
        detail = (
            f"Programa: {program}\nPasta: {cwd or os.getcwd()}\n\n{line}\n\n"
            "O programa fica rodando. O agente poderá mandar texto a ele e ler a saída sem novo cartão; "
            "cada envio aparece no rastro. Interromper encerra a sessão."
        )
        detail += describe_environment(environment_argument(arguments.get("ambiente")))
        note = risk_note(resolve_path(program)) if _is_absolute(program) else ""
        if note:
            detail = note + "\n" + detail
        return {"title": "Iniciar sessão de programa", "summary": line, "detail": detail}

    def run(self, arguments: dict) -> dict:
        program = _program(str(arguments.get("programa") or ""))
        args = _args(arguments.get("argumentos"), program)
        cwd = _cwd(arguments.get("pasta"))
        environment = resolve_environment(environment_argument(arguments.get("ambiente")))
        session = MANAGER.start([program, *args], cwd, environment)
        result = session.read(
            str(arguments.get("esperar_texto") or ""),
            _seconds(arguments.get("esperar_segundos"), 3, 120),
            cancel=self.cancel,
        )
        result["pid"] = session.proc.pid
        result["mensagem"] = f"Sessão {session.ident} iniciada: {subprocess.list2cmdline([program, *args])[:160]}"
        return result


@tool
class SendInput(SessionTool):
    name = "enviar_entrada"
    order = 114
    description = (
        "Manda texto ao stdin de uma sessão aberta e devolve a saída que veio depois. "
        "Retorno: {sessao, saida, motivo, em_execucao, exit_code}."
    )
    parameters = schema(
        {
            "sessao": text_prop("Identificador devolvido por iniciar_sessao, como s1."),
            "texto": text_prop("Texto a enviar."),
            "nova_linha": bool_prop("Opcional. Acrescenta Enter ao fim. Padrão true."),
            "fechar_entrada": bool_prop("Opcional. Fecha o stdin depois do envio, como Ctrl+Z."),
            "esperar_texto": text_prop("Opcional. Espera este texto (ou regex) na saída."),
            "esperar_segundos": int_prop("Opcional. Até 120. Padrão 5."),
        },
        ["sessao", "texto"],
    )

    def run(self, arguments: dict) -> dict:
        session = MANAGER.get(str(arguments.get("sessao") or ""))
        text = str(arguments.get("texto") or "")
        if len(text) > 100_000:
            raise ValueError("O texto passa de 100.000 caracteres.")
        if arguments.get("nova_linha") is not False:
            text += "\n"
        session.send(text)
        if arguments.get("fechar_entrada"):
            session.close_input()
        result = session.read(
            str(arguments.get("esperar_texto") or ""),
            _seconds(arguments.get("esperar_segundos"), 5, 120),
            cancel=self.cancel,
        )
        shown, cut = clip(text.strip(), 80)
        result["mensagem"] = f"Enviado à sessão {session.ident}: {shown}" + ("…" if cut else "")
        return result


@tool
class ReadOutput(SessionTool):
    name = "ler_saida"
    order = 116
    description = (
        "Lê a saída nova de uma sessão aberta, esperando um texto, a saída parar ou o tempo acabar. "
        "Retorno: {sessao, saida, motivo, em_execucao, exit_code}."
    )
    parameters = schema(
        {
            "sessao": text_prop("Identificador da sessão, como s1."),
            "esperar_texto": text_prop("Opcional. Espera este texto (ou regex)."),
            "esperar_segundos": int_prop("Opcional. Até 300. Padrão 3."),
        },
        ["sessao"],
    )

    def run(self, arguments: dict) -> dict:
        session = MANAGER.get(str(arguments.get("sessao") or ""))
        result = session.read(
            str(arguments.get("esperar_texto") or ""),
            _seconds(arguments.get("esperar_segundos"), 3, 300),
            cancel=self.cancel,
        )
        result["mensagem"] = f"Leitura da sessão {session.ident} ({result['motivo']})"
        return result


@tool
class ListSessions(SessionTool):
    name = "listar_sessoes"
    order = 118
    description = "Lista as sessões de programa abertas nesta conversa. Retorno: {sessoes: [{sessao, comando, pid, em_execucao}]}."
    parameters = schema({}, [])

    def run(self, arguments: dict) -> dict:
        return {"sessoes": MANAGER.list()}


@tool
class StopSession(SessionTool):
    name = "encerrar_sessao"
    order = 119
    description = "Encerra o programa de uma sessão e os processos filhos dele. Retorno: {sessao, exit_code}."
    parameters = schema({"sessao": text_prop("Identificador da sessão, como s1.")}, ["sessao"])

    def run(self, arguments: dict) -> dict:
        result = MANAGER.stop(str(arguments.get("sessao") or ""))
        result["mensagem"] = f"Sessão {result['sessao']} encerrada."
        return result
