"""Lista os arquivos de uma mudança e, depois, desfaz o lote."""

from __future__ import annotations

import secrets

from ollamapilot import undo
from ollamapilot.tools.base import Tool, schema, text_prop, tool
from ollamapilot.tools.paths import resolve_path

_ACTIONS = {"criar", "editar", "excluir", "mover", "copiar"}


@tool
class ProposePlan(Tool):
    name = "propor_plano"
    order = 100
    prompt_hint = (
        "Se o pedido citar mais de um caminho absoluto a alterar, chame propor_plano com a lista "
        "antes de gravar, editar, aplicar patch ou excluir."
    )
    description = (
        "Apresenta os arquivos que serão criados, editados ou excluídos e espera autorização antes das gravações. "
        "Use quando a tarefa alterar mais de um caminho. "
        "Retorno: {plano, objetivo, arquivos}. Cada arquivo do plano ainda passa pelo cartão da própria gravação."
    )
    sensitive = True
    parameters = schema(
        {
            "objetivo": text_prop("O que a mudança faz, em uma frase."),
            "passos": {
                "type": "array",
                "description": "Objetos {caminho, acao, motivo}. acao é criar, editar ou excluir.",
            },
        },
        ["objetivo", "passos"],
    )

    def preview(self, arguments: dict) -> dict:
        steps = _steps(arguments)
        lines = [f"{item['acao']}: {item['caminho']}" + (f" — {item['motivo']}" if item["motivo"] else "") for item in steps]
        return {
            "title": "Plano de alteração",
            "summary": f"{len(steps)} arquivos",
            "detail": str(arguments.get("objetivo") or "").strip() + "\n\n" + "\n".join(lines),
        }

    def run(self, arguments: dict) -> dict:
        steps = _steps(arguments)
        plan_id = secrets.token_hex(4)
        paths = [item["caminho"] for item in steps]
        agent = getattr(self, "ctx", None)
        agent = getattr(agent, "agent", None)
        if agent is not None:
            agent.approved_plan = {path.lower() for path in paths}
            agent.plan_id = plan_id
        return {"plano": plan_id, "objetivo": str(arguments.get("objetivo") or "").strip(), "arquivos": paths}


@tool
class UndoPlan(Tool):
    name = "desfazer_plano"
    order = 105
    mutates = True
    scope_label = "gravar, editar, criar pasta e desfazer arquivos"
    description = (
        "Desfaz, na ordem inversa, as gravações de um plano já executado. "
        "Retorno: {plano, restaurados}."
    )
    sensitive = True
    grant_key = "arquivos"
    parameters = schema({"plano": text_prop("Identificador devolvido por propor_plano.")}, ["plano"])

    def preview(self, arguments: dict) -> dict:
        plan_id = str(arguments.get("plano") or "").strip()
        return {
            "title": "Desfazer plano",
            "summary": plan_id,
            "detail": f"Restaura os arquivos guardados no plano {plan_id}.",
        }

    def run(self, arguments: dict) -> dict:
        return undo.restore_plan(str(arguments.get("plano") or ""))


def _steps(arguments: dict) -> list[dict]:
    raw = arguments.get("passos")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("Os passos precisam ser uma lista.") from error
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ValueError("Informe ao menos um arquivo no plano.")
    steps = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Cada passo precisa ter caminho e ação.")
        path = resolve_path(item.get("caminho"))
        action = str(item.get("acao") or "editar").strip().lower()
        if action not in _ACTIONS:
            raise ValueError("A ação do passo é criar, editar, excluir, mover ou copiar.")
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        steps.append({"caminho": str(path), "acao": action, "motivo": str(item.get("motivo") or "").strip()[:200]})
        if len(steps) == 40:
            break
    if not steps:
        raise ValueError("Nenhum caminho válido no plano.")
    return steps
