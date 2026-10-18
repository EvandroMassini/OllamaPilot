"""Memória entre conversas e a lista de tarefas desta conversa."""

from __future__ import annotations

from ollamapilot import memory
from ollamapilot.tools.base import Tool, schema, text_prop, tool

MAX_TASKS = 40


def _tasks(tool) -> list[dict]:
    agent = getattr(getattr(tool, "ctx", None), "agent", None)
    if agent is None:
        raise ValueError("A lista de tarefas só existe dentro de uma conversa.")
    current = getattr(agent, "tasks", None)
    if not isinstance(current, list):
        agent.tasks = []
    return agent.tasks


@tool
class Remember(Tool):
    name = "lembrar"
    group = "continuidade"
    order = 30
    description = (
        "Guarda uma frase curta, até 240 caracteres, para as próximas conversas. "
        "Não guarda senha, chave nem token. Retorno: {guardado, total}."
    )
    parameters = schema({"texto": text_prop("Frase curta que o usuário pediu para guardar.")}, ["texto"])

    def run(self, arguments: dict) -> dict:
        return memory.remember(str(arguments.get("texto") or ""))


@tool
class Forget(Tool):
    name = "esquecer"
    group = "continuidade"
    order = 31
    description = "Apaga da memória as frases que contêm o texto informado. Retorno: {apagadas, total}."
    parameters = schema({"texto": text_prop("Trecho da frase que deve sair da memória.")}, ["texto"])

    def run(self, arguments: dict) -> dict:
        return memory.forget(str(arguments.get("texto") or ""))


@tool
class Tasks(Tool):
    name = "tarefas"
    group = "continuidade"
    order = 32
    description = (
        "Lista ordenada desta conversa. acao: listar, adicionar ou concluir. "
        "Cada item fica pendente ou feito. Marque o item ao concluir. "
        "Retorno: {itens: [{texto, estado}]}."
    )
    parameters = schema(
        {
            "acao": text_prop("listar, adicionar ou concluir. Padrão listar."),
            "texto": text_prop("Frase do item, ao adicionar ou ao concluir."),
            "indice": {"type": "integer", "description": "Opcional. Posição do item, começando em 1, ao concluir."},
        },
        [],
    )

    def run(self, arguments: dict) -> dict:
        items = _tasks(self)
        action = str(arguments.get("acao") or "listar").strip().lower()
        if action in {"", "listar"}:
            return {"itens": items}
        if action == "adicionar":
            text = " ".join(str(arguments.get("texto") or "").split())
            if not text:
                raise ValueError("Informe o texto da tarefa.")
            if len(text) > 240:
                raise ValueError("A tarefa passa de 240 caracteres.")
            if len(items) >= MAX_TASKS:
                raise ValueError(f"A lista já tem {MAX_TASKS} itens.")
            items.append({"texto": text, "estado": "pendente"})
            return {"itens": items}
        if action == "concluir":
            index = arguments.get("indice")
            text = " ".join(str(arguments.get("texto") or "").split()).lower()
            chosen = None
            if index not in (None, "", 0):
                try:
                    number = int(index)
                except (TypeError, ValueError) as error:
                    raise ValueError("O índice precisa ser um número.") from error
                if number < 1 or number > len(items):
                    raise ValueError("Não há tarefa nessa posição.")
                chosen = items[number - 1]
            else:
                for item in items:
                    if text and text in item["texto"].lower() and item.get("estado") != "feito":
                        chosen = item
                        break
            if chosen is None:
                raise ValueError("Não encontrei a tarefa para marcar como feita.")
            chosen["estado"] = "feito"
            return {"itens": items}
        raise ValueError("Ação desconhecida. Use listar, adicionar ou concluir.")
