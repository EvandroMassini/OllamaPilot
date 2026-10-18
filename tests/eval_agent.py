"""Avaliação local e determinística do ciclo do agente.

Uso:
    py -3 tests\\eval_agent.py                 avalia o código desta pasta
    py -3 tests\\eval_agent.py --json saida.json
    $env:OLLAMAPILOT_ROOT = "C:\\copia\\antiga"; py -3 tests\\eval_agent.py   compara com outra versão

O modelo é simulado com respostas prontas. Nada sai desta máquina: sem Ollama, nuvem, internet,
banco ou programa externo. Arquivos só em pastas temporárias. Mede o comportamento do OllamaPilot
diante de cada situação, não a qualidade de um modelo real.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.environ.get("OLLAMAPILOT_ROOT") or str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)
os.environ.setdefault("OLLAMAPILOT_STORAGE", tempfile.mkdtemp(prefix="ollamapilot-eval-"))

from unittest.mock import patch  # noqa: E402

import ollamapilot  # noqa: E402
from ollamapilot.agent import Agent, Cancelled  # noqa: E402
from ollamapilot.attachments import read_attachment  # noqa: E402
from ollamapilot.config import Settings  # noqa: E402


class ModelRefused(RuntimeError):
    pass


class ScriptedModel:
    def __init__(self, steps: list, caps: dict | None = None) -> None:
        self.steps = list(steps)
        self.requests: list[dict] = []
        self.caps = caps

    def capabilities(self) -> dict:
        return self.caps or {}

    def chat(self, messages, tools, on_event, cancel):
        self.requests.append({"messages": json.loads(json.dumps(messages, default=str)), "tools": tools})
        if self.caps and self.caps.get("conhecido") and not self.caps.get("ferramentas") and tools:
            raise ModelRefused("registry.ollama.ai/library/modelo does not support tools")
        if not self.steps:
            return {"content": "fim", "tool_calls": []}
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def call(name: str, **arguments) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


def tools(*calls) -> dict:
    return {"content": "", "tool_calls": list(calls)}


def say(text: str) -> dict:
    return {"content": text, "tool_calls": []}


def valid(transcript: list[dict]) -> bool:
    index = 0
    while index < len(transcript):
        message = transcript[index]
        if message.get("role") == "tool":
            return False
        expected = len(message.get("tool_calls") or [])
        index += 1
        seen = 0
        while index < len(transcript) and transcript[index].get("role") == "tool":
            seen += 1
            index += 1
        if seen < expected:
            return False
    return True


def tool_texts(transcript: list[dict]) -> list[str]:
    return [
        str(m.get("content") or "")
        for m in transcript
        if m.get("role") == "tool" or str(m.get("content") or "").startswith("RESULTADO CONFIÁVEL")
    ]


class Run:
    def __init__(self, steps: list, settings: Settings | None = None, caps: dict | None = None) -> None:
        self.model = ScriptedModel(steps, caps)
        self.agent = Agent(settings or Settings(model="avaliacao"), self.model)
        self.asked = 0
        self.error = ""
        self.ms = 0

    def turn(self, text: str, decision="once", cancel=lambda: False, attachments=None) -> str:
        def approve(_preview):
            self.asked += 1
            return decision(_preview) if callable(decision) else decision

        started = time.perf_counter()
        try:
            return self.agent.turn(text, attachments or [], lambda *_: None, approve, cancel)
        except Cancelled:
            self.error = "cancelado"
            return ""
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            return ""
        finally:
            self.ms += int((time.perf_counter() - started) * 1000)

    def metrics(self, success: bool, right_tool: bool | None) -> dict:
        texts = tool_texts(self.agent.transcript)
        redundant = sum(
            1 for text in texts if any(mark in text for mark in ("repetida", "já foi feita", "já recusou", "já falhou"))
        )
        errors = sum(1 for text in texts if '"erro"' in text)
        return {
            "sucesso": bool(success),
            "ferramenta_correta": right_tool,
            "chamadas_modelo": len(self.model.requests),
            "redundantes": redundant,
            "erros_ferramenta": errors,
            "intervencoes": self.asked,
            "latencia_ms": self.ms,
            "transcricao_valida": valid(self.agent.transcript),
            "excecao": self.error,
        }


def scenario_correct_tool(folder: Path) -> dict:
    target = folder / "leia.txt"
    target.write_text("conteúdo-esperado", encoding="utf-8")
    run = Run([tools(call("ler_arquivo", caminho=str(target))), say("O arquivo diz conteúdo-esperado.")])
    answer = run.turn(f"Leia {target}")
    read_ok = any("conteúdo-esperado" in text for text in tool_texts(run.agent.transcript))
    return run.metrics("conteúdo-esperado" in answer and read_ok, read_ok)


def scenario_text_json_fallback(folder: Path) -> dict:
    target = folder / "index.html"
    payload = json.dumps({"name": "gravar_arquivo", "arguments": {"caminho": str(target), "conteudo": "<p>oi</p>"}})
    run = Run([say("```json\n" + payload + "\n```"), say("Gravei.")])
    answer = run.turn(f"Crie a página e salve em {folder}")
    created = target.is_file()
    return run.metrics(created and answer == "Gravei.", created)


def scenario_missing_argument(folder: Path) -> dict:
    target = folder / "sem_conteudo.txt"
    run = Run([tools(call("gravar_arquivo", caminho=str(target))), say("Faltou o conteúdo.")])
    run.turn(f"Grave em {target}")
    return run.metrics(not target.exists() and run.asked == 0, not target.exists())


def scenario_malformed_arguments(folder: Path) -> dict:
    bad = {"function": {"name": "ler_arquivo", "arguments": "{caminho: " + str(folder)}}
    run = Run([tools(bad), say("Os argumentos vieram quebrados.")])
    run.turn("leia o arquivo")
    explained = any("JSON" in text for text in tool_texts(run.agent.transcript))
    return run.metrics(explained, None)


def scenario_refusal_not_repeated(folder: Path) -> dict:
    target = str(folder / "recusado.txt")
    step = tools(call("gravar_arquivo", caminho=target, conteudo="x"))
    run = Run([step, say("Não gravei."), json.loads(json.dumps(step)), say("Continuo sem gravar.")])
    run.agent.workspace = folder
    run.turn("grave", decision="deny")
    run.turn("grave de novo", decision="deny")
    return run.metrics(run.asked == 1 and not Path(target).exists(), None)


def scenario_cancel_during_approval(folder: Path) -> dict:
    state = {"cancel": False}

    def deny_and_cancel(_preview):
        state["cancel"] = True
        return "deny"

    run = Run([tools(call("gravar_arquivo", caminho=str(folder / "c.txt"), conteudo="x"))])
    run.agent.workspace = folder
    run.turn("grave", decision=deny_and_cancel, cancel=lambda: state["cancel"])
    return run.metrics(run.error == "cancelado" and valid(run.agent.transcript) and not (folder / "c.txt").exists(), None)


def scenario_duplicate_calls(folder: Path) -> dict:
    target = folder / "uma_vez.txt"
    write = call("gravar_arquivo", caminho=str(target), conteudo="1")
    run = Run([tools(write, json.loads(json.dumps(write))), say("Gravei uma vez.")])
    run.agent.workspace = folder
    run.turn("grave")
    return run.metrics(run.asked == 1 and target.read_text(encoding="utf-8") == "1", True)


def scenario_no_progress(folder: Path) -> dict:
    missing = call("ler_arquivo", caminho=str(folder / "nao_existe.txt"))
    steps = [tools(json.loads(json.dumps(missing))) for _ in range(20)]
    run = Run(steps, Settings(model="avaliacao", max_tool_rounds=16))
    run.turn("leia")
    return run.metrics(len(run.model.requests) <= 4 and not run.error, None)


def scenario_reread_after_edit(folder: Path) -> dict:
    target = folder / "edit.txt"
    target.write_text("valor = 1\n", encoding="utf-8")
    read = call("ler_arquivo", caminho=str(target))
    run = Run(
        [
            tools(read),
            tools(call("editar_arquivo", caminho=str(target), buscar="valor = 1", substituir="valor = 2")),
            tools(json.loads(json.dumps(read))),
            say("Conferido."),
        ]
    )
    run.agent.workspace = folder
    run.turn("troque e confira")
    texts = tool_texts(run.agent.transcript)
    return run.metrics(bool(texts) and "valor = 2" in texts[-1], bool(texts) and "valor = 2" in texts[-1])


def scenario_context_budget(folder: Path) -> dict:
    files = []
    for n in range(6):
        path = folder / f"grande{n}.txt"
        path.write_text(("registro %d\n" % n) * 1200, encoding="utf-8")
        files.append(path)
    run = Run([tools(call("ler_arquivo", caminho=str(p))) for p in files] + [say("Resumo pronto.")], Settings(model="avaliacao", num_ctx=8192))
    run.agent.transcript = [
        {"role": "user", "content": "pedido antigo " + "y" * 30000},
        {"role": "assistant", "content": "ok"},
    ]
    run.turn("PEDIDO-ATUAL resuma os seis arquivos")
    within = all(
        sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in r["messages"]) // 4
        + len(json.dumps(r["tools"])) // 4
        <= 8192
        for r in run.model.requests
    )
    kept = all(any("PEDIDO-ATUAL" in str(m.get("content")) for m in r["messages"]) for r in run.model.requests)
    return run.metrics(within and kept and not run.error, None)


def scenario_bad_attachment_isolated(folder: Path) -> dict:
    good = folder / "ok.txt"
    good.write_text("texto", encoding="utf-8")
    started = time.perf_counter()
    results = [read_attachment(good), read_attachment(folder / "sumiu.txt")]
    ok = results[0].get("ok") and not results[1].get("ok")
    return {
        "sucesso": bool(ok),
        "ferramenta_correta": None,
        "chamadas_modelo": 0,
        "redundantes": 0,
        "erros_ferramenta": 0,
        "intervencoes": 0,
        "latencia_ms": int((time.perf_counter() - started) * 1000),
        "transcricao_valida": True,
        "excecao": "",
    }


def scenario_office_attachment(folder: Path) -> dict:
    from docx import Document

    path = folder / "contrato.docx"
    document = Document()
    document.add_paragraph("Cláusula de teste")
    document.save(str(path))
    started = time.perf_counter()
    result = read_attachment(path)
    ok = result.get("ok") and "Cláusula de teste" in str(result.get("anexo", {}).get("text"))
    return {
        "sucesso": bool(ok),
        "ferramenta_correta": None,
        "chamadas_modelo": 0,
        "redundantes": 0,
        "erros_ferramenta": 0,
        "intervencoes": 0,
        "latencia_ms": int((time.perf_counter() - started) * 1000),
        "transcricao_valida": True,
        "excecao": "" if ok else str(result.get("erro")),
    }


def scenario_invalid_settings(folder: Path) -> dict:
    path = folder / "settings.json"
    path.write_text("{", encoding="utf-8")
    started = time.perf_counter()
    with patch("ollamapilot.config.SETTINGS_FILE", path):
        loaded = Settings.load()
    parked = any(item.name.startswith("settings.json.invalido") for item in folder.iterdir())
    return {
        "sucesso": loaded.num_ctx == Settings().num_ctx and parked,
        "ferramenta_correta": None,
        "chamadas_modelo": 0,
        "redundantes": 0,
        "erros_ferramenta": 0,
        "intervencoes": 0,
        "latencia_ms": int((time.perf_counter() - started) * 1000),
        "transcricao_valida": True,
        "excecao": "",
    }


def scenario_model_without_tools(folder: Path) -> dict:
    run = Run([say("Respondo só com texto.")], caps={"conhecido": True, "ferramentas": False, "visao": False})
    answer = run.turn("explique recursão")
    return run.metrics(answer == "Respondo só com texto.", None)


def scenario_server_failure_recovery(folder: Path) -> dict:
    run = Run([tools(call("listar_pasta", caminho=str(folder))), ConnectionError("conexão caiu"), say("Retomei.")])
    run.turn("liste")
    first_valid = valid(run.agent.transcript)
    answer = run.turn("continue")
    return run.metrics(first_valid and answer == "Retomei.", None)


def scenario_nudge_keeps_attachment(folder: Path) -> dict:
    run = Run([say("Desculpe, mas não posso salvar arquivos."), say("Usei o anexo.")])
    attachment = {"name": "dados.csv", "text": "ANEXO-777;1", "codificacao": "utf-8"}
    run.turn(f"Crie o relatório e salve em {folder}", attachments=[attachment])
    second = run.model.requests[1]["messages"] if len(run.model.requests) > 1 else []
    kept = any("ANEXO-777" in str(m.get("content")) for m in second)
    return run.metrics(kept, None)


SCENARIOS = {
    "ferramenta_correta": scenario_correct_tool,
    "fallback_json_no_texto": scenario_text_json_fallback,
    "argumento_ausente": scenario_missing_argument,
    "argumento_malformado": scenario_malformed_arguments,
    "recusa_nao_repetida": scenario_refusal_not_repeated,
    "cancelamento_na_autorizacao": scenario_cancel_during_approval,
    "chamada_duplicada": scenario_duplicate_calls,
    "sem_progresso": scenario_no_progress,
    "reler_apos_editar": scenario_reread_after_edit,
    "limite_de_contexto": scenario_context_budget,
    "anexo_ruim_isolado": scenario_bad_attachment_isolated,
    "anexo_docx": scenario_office_attachment,
    "configuracao_invalida": scenario_invalid_settings,
    "modelo_sem_ferramentas": scenario_model_without_tools,
    "falha_do_servidor": scenario_server_failure_recovery,
    "nudge_preserva_anexo": scenario_nudge_keeps_attachment,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", help="grava os resultados neste arquivo")
    options = parser.parse_args()
    results = {}
    with tempfile.TemporaryDirectory(prefix="ollamapilot-eval-logs-") as logs:
        with patch("ollamapilot.logbook.LOGS", Path(logs)):
            for name, scenario in SCENARIOS.items():
                with tempfile.TemporaryDirectory(prefix=f"eval-{name}-") as folder:
                    try:
                        results[name] = scenario(Path(folder))
                    except Exception as error:
                        results[name] = {"sucesso": False, "excecao": f"{type(error).__name__}: {error}"}
    print(f"Código avaliado: {Path(ollamapilot.__file__).resolve().parent}")
    header = f"{'cenário':30} {'ok':>3} {'modelo':>6} {'redund':>6} {'erros':>5} {'interv':>6} {'ms':>6} {'válida':>6}"
    print(header)
    print("-" * len(header))
    for name, item in results.items():
        print(
            f"{name:30} {'sim' if item.get('sucesso') else 'NÃO':>3} {item.get('chamadas_modelo', '-'):>6} "
            f"{item.get('redundantes', '-'):>6} {item.get('erros_ferramenta', '-'):>5} {item.get('intervencoes', '-'):>6} "
            f"{item.get('latencia_ms', '-'):>6} {('sim' if item.get('transcricao_valida') else 'NÃO'):>6}"
        )
        if item.get("excecao") and not item.get("sucesso"):
            print(f"    {item['excecao'][:150]}")
    passed = sum(1 for item in results.values() if item.get("sucesso"))
    calls = sum(int(item.get("chamadas_modelo") or 0) for item in results.values())
    redundant = sum(int(item.get("redundantes") or 0) for item in results.values())
    print("-" * len(header))
    print(f"Sucesso: {passed}/{len(results)} · chamadas ao modelo: {calls} · chamadas redundantes registradas: {redundant}")
    if options.json:
        Path(options.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
