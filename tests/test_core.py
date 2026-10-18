from __future__ import annotations

import os

os.environ.setdefault("OLLAMAPILOT_VAULT", "memory")

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ollamapilot.agent import Agent, AgentError, Cancelled, fit_transcript, repair_transcript, _fallback_answer
from ollamapilot.attachments import read_attachment
from ollamapilot.config import Settings
from ollamapilot.ollama_client import OllamaClient, OllamaError, describe_balance
from ollamapilot.tools.artifacts import CreateArtifact
from ollamapilot.tools.binaryinfo import ReadBinary
from ollamapilot.tools.database import guard_select
from ollamapilot.tools.desktop import calculator_button_names, coerce_actions, to_sendkeys
from ollamapilot.tools.filesystem import EditFile, MakeDirectory, ReadFile, WriteFile
from ollamapilot.logbook import log_error
from ollamapilot.tools.ghidra_tool import _summarize, build_bat, failure_detail
from ollamapilot.tools.paths import is_secret, resolve_path
from ollamapilot.tools.registry import build_registry
from ollamapilot.tools.webtools import _html_to_text, is_local_url


class FakeClient:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.calls = 0

    def chat(self, messages, tools, on_event, cancel):
        step = self.steps[self.calls]
        self.calls += 1
        if step.get("content"):
            on_event("content", step["content"])
        return step


class FilesystemTests(unittest.TestCase):
    def test_relative_path_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_path("relativo\\arquivo.txt")

    def test_secret_detection(self) -> None:
        self.assertTrue(is_secret(Path(r"C:\trabalho\.env")))
        self.assertFalse(is_secret(Path(r"C:\trabalho\main.py")))

    def test_write_read_edit(self) -> None:
        settings = Settings()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app" / "main.py"
            write = WriteFile(settings)
            created = write.run({"caminho": str(target), "conteudo": "valor = 1\n"})
            self.assertTrue(created["criado"])
            read = ReadFile(settings).run({"caminho": str(target)})
            self.assertIn("valor = 1", read["conteudo"])
            EditFile(settings).run({"caminho": str(target), "buscar": "valor = 1", "substituir": "valor = 2"})
            self.assertIn("valor = 2", target.read_text(encoding="utf-8"))
            folder_path = Path(folder) / "vazio"
            self.assertTrue(MakeDirectory().needs_approval({"caminho": str(folder_path)}))
            MakeDirectory().run({"caminho": str(folder_path)})
            self.assertTrue(folder_path.is_dir())
            self.assertFalse(MakeDirectory().needs_approval({"caminho": str(folder_path)}))

    def test_binary_map_of_python(self) -> None:
        report = ReadBinary().run({"caminho": __import__("sys").executable})
        self.assertEqual(report["tipo"][:2], "PE")
        self.assertTrue(report["sha256"])
        self.assertTrue(report["imports"])


class ToolPolicyTests(unittest.TestCase):
    def test_select_guard(self) -> None:
        self.assertTrue(guard_select("SELECT 1").startswith("SELECT"))
        with self.assertRaises(ValueError):
            guard_select("DROP TABLE clientes")
        with self.assertRaises(ValueError):
            guard_select("SELECT 1; DELETE FROM clientes")

    def test_local_url_and_html(self) -> None:
        self.assertTrue(is_local_url("http://127.0.0.1/painel"))
        self.assertTrue(is_local_url("http://192.168.0.20/app"))
        self.assertFalse(is_local_url("https://ollama.com/search"))
        self.assertEqual(_html_to_text("<p>Olá <b>mundo</b></p>"), "Olá mundo")

    def test_ghidra_launcher_quotes_paths(self) -> None:
        text = build_bat(
            Path(r"C:\ghidra\support\analyzeHeadless.bat"),
            Path(r"C:\temp\projeto"),
            Path(r"E:\Programas\app.exe"),
            Path(r"E:\Projetos com IA\OllamaPilot\ollamapilot\ghidra_scripts\ExportarRelatorio.py"),
            Path(r"C:\temp\relatorio.json"),
            Path(r"C:\temp\ghidra.log"),
            15,
            "-",
        )
        self.assertIn("analyzeHeadless.bat", text)
        self.assertIn("ExportarRelatorio.py", text)
        self.assertIn('"E:\\Projetos com IA\\OllamaPilot\\ollamapilot\\ghidra_scripts"', text)

    def test_ghidra_failure_ignores_java_warnings(self) -> None:
        warnings = "WARNING: sun.misc.Unsafe::staticFieldOffset has been called\n" * 20
        stdout = "INFO  REPORT: Analysis succeeded\nERROR REPORT SCRIPT ERROR: ExportarRelatorio.py : Ghidra was not started with PyGhidra. Python is not available\n"
        detail = failure_detail(stdout, warnings, "")
        self.assertIn("Python is not available", detail)
        self.assertNotIn("Unsafe", detail)

    def test_ghidra_summary_prefers_behavior_over_startup(self) -> None:
        payload = {
            "programa": "Estoque.exe",
            "formato": "Portable Executable (PE)",
            "funcoes_total": 22000,
            "imports": [{"nome": f"Fun{i}", "biblioteca": "KERNEL32.DLL"} for i in range(70)]
            + [{"nome": "OpenPrinterW", "biblioteca": "WINSPOOL.DRV"}],
            "strings": ["Bogus Huffman table definition"] * 40,
            "sinais": [
                "Bogus Huffman table",
                "select * from usuarios where status=1",
                "Estoque.fdb",
                "FrmCadEstoque",
                "Cadastro de estoque (almoxarifado)",
            ],
            "funcoes": [{"nome": "entry", "entrada": "00403320", "pseudocodigo": "void entry(){ memcpy(); }" + ("x" * 8000)}],
            "blocos": [],
            "uso": "entender",
        }
        summary = _summarize(payload)
        libraries = {item["biblioteca"]: item["quantidade"] for item in summary["imports"]}
        self.assertEqual(libraries["KERNEL32.DLL"], 70)
        self.assertEqual(libraries["WINSPOOL.DRV"], 1)
        self.assertIn("select * from usuarios where status=1", summary["textos"])
        self.assertIn("Estoque.fdb", summary["textos"])
        self.assertNotIn("Bogus Huffman table", summary["textos"])
        noisy = _summarize({
            "sinais": [
                "SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'",
                "-- PostgreSQL SQL sample source",
                "Cannot delete an empty row",
                "TFrmAcesso",
                "Estoque.fdb",
                "Encerrar a ordem de compra selecionada?",
                "select * from sp_saida(",
            ],
            "funcoes": [],
            "imports": [],
            "blocos": [],
        })
        joined = "\n".join(noisy["textos"])
        self.assertIn("TFrmAcesso", joined)
        self.assertIn("Estoque.fdb", joined)
        self.assertIn("Encerrar a ordem", joined)
        self.assertIn("sp_saida", joined)
        self.assertNotIn("INFORMATION_SCHEMA", joined)
        self.assertNotIn("PostgreSQL", joined)
        self.assertNotIn("empty row", joined)
        self.assertEqual(summary["funcoes"][0]["nome"], "entry")
        self.assertLessEqual(len(summary["funcoes"][0].get("pseudocodigo") or ""), 1500)
        self.assertLess(len(__import__("json").dumps(summary)), 24000)

    def test_text_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch("ollamapilot.tools.artifacts.ARTIFACTS", Path(folder)):
                created = CreateArtifact(Settings()).run({"nome": "nota.txt", "conteudo": "sala limpa"})
            self.assertEqual(Path(created["caminho"]).read_text(encoding="utf-8"), "sala limpa")


class AgentLoopTests(unittest.TestCase):
    def test_once_asks_again_and_chat_grant_persists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = str(Path(folder) / "um.txt")
            second = str(Path(folder) / "dois.txt")
            third = str(Path(folder) / "tres.txt")
            fourth = str(Path(folder) / "quatro.txt")
            fifth = str(Path(folder) / "cinco.txt")
            client = FakeClient(
                [
                    {"content": "", "tool_calls": [{"function": {"name": "gravar_arquivo", "arguments": {"caminho": first, "conteudo": "1"}}}]},
                    {"content": "", "tool_calls": [{"function": {"name": "gravar_arquivo", "arguments": {"caminho": second, "conteudo": "2"}}}]},
                    {"content": "Pediu duas vezes.", "tool_calls": []},
                    {"content": "", "tool_calls": [{"function": {"name": "gravar_arquivo", "arguments": {"caminho": third, "conteudo": "3"}}}]},
                    {"content": "", "tool_calls": [{"function": {"name": "gravar_arquivo", "arguments": {"caminho": fourth, "conteudo": "4"}}}]},
                    {"content": "Chat cobriu os dois.", "tool_calls": []},
                    {"content": "", "tool_calls": [{"function": {"name": "gravar_arquivo", "arguments": {"caminho": fifth, "conteudo": "5"}}}]},
                    {"content": "Sem novo pedido.", "tool_calls": []},
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            agent.workspace = Path(folder)
            once = []

            def approve_once(preview):
                once.append(preview["title"])
                return "once"

            answer = agent.turn("crie os arquivos", [], lambda *_: None, approve_once, lambda: False)
            self.assertEqual(answer, "Pediu duas vezes.")
            self.assertEqual(once, ["Criar arquivo", "Criar arquivo"])
            self.assertTrue(Path(first).is_file())
            self.assertTrue(Path(second).is_file())

            chat = []

            def approve_chat(preview):
                chat.append(preview["title"])
                return "chat"

            answer = agent.turn("crie mais", [], lambda *_: None, approve_chat, lambda: False)
            self.assertEqual(answer, "Chat cobriu os dois.")
            self.assertEqual(chat, ["Criar arquivo"])
            self.assertEqual(agent.chat_grants, {"arquivos"})
            self.assertTrue(Path(fourth).is_file())

            answer = agent.turn("mais um", [], lambda *_: None, approve_chat, lambda: False)
            self.assertEqual(answer, "Sem novo pedido.")
            self.assertEqual(chat, ["Criar arquivo"])
            self.assertTrue(Path(fifth).is_file())

    def test_refusal_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = str(Path(folder) / "novo.txt")
            client = FakeClient(
                [
                    {"content": "", "tool_calls": [{"function": {"name": "gravar_arquivo", "arguments": {"caminho": target, "conteudo": "x"}}}]},
                    {"content": "Não gravei.", "tool_calls": []},
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            answer = agent.turn("grave", [], lambda *_: None, lambda _preview: False, lambda: False)
            self.assertEqual(answer, "Não gravei.")
            self.assertFalse(Path(target).exists())

    def test_empty_model_reply_explains_itself(self) -> None:
        client = FakeClient([{"content": "   ", "tool_calls": [], "metrics": {"eval_count": 0}}])
        agent = Agent(Settings(model="qwen3.5:9b", num_ctx=8192), client)
        with self.assertRaises(AgentError) as caught:
            agent.turn("crie o php", [], lambda *_: None, lambda _preview: False, lambda: False)
        self.assertIn("não produziu", str(caught.exception))
        self.assertEqual(caught.exception.details["modelo"], "qwen3.5:9b")
        self.assertEqual(caught.exception.details["metricas"]["eval_count"], 0)

    def test_error_log_records_the_message(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch("ollamapilot.logbook.LOGS", Path(folder)):
                path = log_error("turno", "O modelo não produziu uma resposta.", modelo="qwen3.5:9b")
            self.assertEqual(path, Path(folder) / "erros.log")
            text = path.read_text(encoding="utf-8")
            self.assertIn("O modelo não produziu uma resposta.", text)
            self.assertIn("qwen3.5:9b", text)

    def test_silent_model_still_reports_the_tool(self) -> None:
        text = _fallback_answer(
            [
                {"role": "tool", "tool_name": "acionar_janela", "content": '{"textos": ["A exibição é 32"]}'},
            ]
        )
        self.assertIn("32", text)
        self.assertIn("acionar_janela", text)

    def test_sendkeys_keeps_plus(self) -> None:
        self.assertEqual(to_sendkeys("2*16="), "2*16=")
        self.assertEqual(to_sendkeys("2+16="), "2{+}16=")

    def test_acoes_acentuadas_viram_lista(self) -> None:
        actions = coerce_actions(
            {"titulo": "Calculadora", "ações": '[{"acao": "teclas", "texto": "2*8="}]'}
        )
        self.assertEqual(actions, [{"acao": "teclas", "texto": "2*8="}])
        self.assertEqual(
            calculator_button_names("2*8="),
            ["Dois", "Multiplicar por", "Oito", "Igual a"],
        )

    def test_registry_hides_optional_tools(self) -> None:
        registry = build_registry(Settings(), None, lambda: False)
        self.assertIn("ghidra_analisar", registry.names())
        self.assertIn("ler_binario", registry.names())
        self.assertNotIn("banco_consultar", registry.names())
        self.assertNotIn("pesquisar_web", registry.names())


class CloudModelTests(unittest.TestCase):
    def test_local_chat_stays_on_the_configured_server(self) -> None:
        client = OllamaClient(Settings(ollama_url="http://127.0.0.1:11434/", cloud_api_key="abc"))
        url, headers = client.chat_target()
        self.assertEqual(url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(headers, {})

    def test_online_model_uses_the_api_key(self) -> None:
        client = OllamaClient(Settings(model="gemma4:31b", model_remote=True, model_source="online", cloud_api_key="abc"))
        url, headers = client.chat_target()
        self.assertEqual(url, "https://ollama.com/api/chat")
        self.assertEqual(headers["Authorization"], "Bearer abc")

    def test_online_model_omits_unlimited_prediction(self) -> None:
        local = Settings(num_predict=-1).generation_options()
        remote = Settings(num_predict=-1).generation_options(remote=True)
        limited = Settings(num_predict=512).generation_options(remote=True)
        self.assertEqual(local["num_predict"], -1)
        self.assertNotIn("num_predict", remote)
        self.assertEqual(limited["num_predict"], 512)

    def test_online_model_without_key_is_refused(self) -> None:
        client = OllamaClient(Settings(model_remote=True, model_source="online", cloud_api_key=""))
        with self.assertRaises(OllamaError):
            client.chat_target()

    def test_cloud_catalog_sends_the_api_key(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"models": [{"name": "gemma4:31b"}]}

        client = OllamaClient(Settings(cloud_api_key="abc"))
        with patch("ollamapilot.ollama_client.httpx.get", return_value=Response()) as get:
            models = client.cloud_tags()
        self.assertEqual(models[0]["name"], "gemma4:31b")
        self.assertEqual(get.call_args.args[0], "https://ollama.com/api/tags")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer abc")

    def test_balance_describes_the_account_not_one_model(self) -> None:
        text = describe_balance(
            {
                "included": {
                    "balance_usd": 99.9,
                    "allowance_usd": 100,
                    "period": {"until": "2099-01-01T00:00:00Z"},
                },
                "purchased": {"balance_usd": 0},
            },
            "gpt-oss:120b",
            True,
        )
        self.assertIn("gpt-oss:120b", text)
        self.assertIn("0,1%", text)
        self.assertIn("US$ 99,90 de US$ 100,00", text)
        self.assertIn("não informa quais modelos", text)

    def test_balance_request_uses_the_api_key(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"included": {}, "purchased": {"balance_usd": 0}}

        client = OllamaClient(Settings(cloud_api_key="abc"))
        with patch("ollamapilot.ollama_client.httpx.get", return_value=Response()) as get:
            client.cloud_balance()
        self.assertEqual(get.call_args.args[0], "https://ollama.com/api/balance")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer abc")


class IntegrityTests(unittest.TestCase):
    def test_cancel_closes_the_pending_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "novo.txt"
            client = FakeClient(
                [
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "gravar_arquivo",
                                    "arguments": {"caminho": str(target), "conteudo": "x"},
                                }
                            }
                        ],
                    }
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            agent.workspace = Path(folder)
            state = {"cancel": False}

            def approve(_preview):
                state["cancel"] = True
                return "deny"

            with self.assertRaises(Cancelled):
                agent.turn("grave", [], lambda *_: None, approve, lambda: state["cancel"])
            self.assertEqual([item["role"] for item in agent.transcript], ["user", "assistant", "tool"])
            self.assertIn("interrompida", agent.transcript[-1]["content"])
            self.assertFalse(target.exists())

    def test_unknown_tool_gets_an_error_result(self) -> None:
        client = FakeClient(
            [
                {"content": "", "tool_calls": [{"function": {"name": "nao_existe", "arguments": {}}}]},
                {"content": "Essa ferramenta não existe.", "tool_calls": []},
            ]
        )
        agent = Agent(Settings(model="teste"), client)
        answer = agent.turn("faça", [], lambda *_: None, lambda _preview: False, lambda: False)
        self.assertEqual(answer, "Essa ferramenta não existe.")
        self.assertEqual(agent.transcript[2]["role"], "tool")
        self.assertIn("não existe", agent.transcript[2]["content"])

    def test_duplicate_call_is_reported_and_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "um.txt"
            call = {
                "function": {
                    "name": "gravar_arquivo",
                    "arguments": {"caminho": str(target), "conteudo": "oi"},
                }
            }
            client = FakeClient(
                [
                    {"content": "", "tool_calls": [call, json.loads(json.dumps(call))]},
                    {"content": "Gravei uma vez.", "tool_calls": []},
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            agent.workspace = Path(folder)
            asked = {"n": 0}

            def approve(_preview):
                asked["n"] += 1
                return "once"

            answer = agent.turn("grave", [], lambda *_: None, approve, lambda: False)
            self.assertEqual(answer, "Gravei uma vez.")
            self.assertEqual(asked["n"], 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "oi")
            tools = [item for item in agent.transcript if item["role"] == "tool"]
            self.assertEqual(len(tools), 2)
            self.assertIn("já foi feita", tools[1]["content"])

    def test_refused_action_is_not_asked_again(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = str(Path(folder) / "novo.txt")
            call = {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "gravar_arquivo", "arguments": {"caminho": target, "conteudo": "x"}}}
                ],
            }
            client = FakeClient(
                [
                    call,
                    {"content": "Não gravei.", "tool_calls": []},
                    json.loads(json.dumps(call)),
                    {"content": "Continuo sem gravar.", "tool_calls": []},
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            agent.workspace = Path(folder)
            asked = {"n": 0}

            def approve(_preview):
                asked["n"] += 1
                return False

            agent.turn("grave", [], lambda *_: None, approve, lambda: False)
            answer = agent.turn("grave de novo", [], lambda *_: None, approve, lambda: False)
            self.assertEqual(answer, "Continuo sem gravar.")
            self.assertEqual(asked["n"], 1)
            self.assertFalse(Path(target).exists())
            self.assertIn("já recusou", agent.transcript[-2]["content"])

    def test_context_keeps_the_request_and_the_tool_pair(self) -> None:
        old = {"role": "user", "content": r"A:\dados\velho.txt " + ("x" * 20000)}
        assistant = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "ler_arquivo"}}]}
        tool = {"role": "tool", "name": "ler_arquivo", "content": "ok"}
        current = {"role": "user", "content": "agora"}
        result = fit_transcript([old, assistant, tool, current], 4096)
        self.assertEqual(result[-1]["content"], "agora")
        self.assertTrue(any("velho.txt" in item["content"] for item in result))
        for index, item in enumerate(result):
            if item.get("role") == "tool":
                self.assertIn(result[index - 1]["role"], ("assistant", "tool"))

    def test_orphan_tool_call_is_closed_on_repair(self) -> None:
        fixed = repair_transcript(
            [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "ler_arquivo"}}]}]
        )
        self.assertEqual([item["role"] for item in fixed], ["assistant", "tool"])
        self.assertIn("cancelado", fixed[1]["content"])

    def test_bad_setting_type_does_not_drop_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(
                json.dumps({"model": "qwen", "num_ctx": "nope", "model_remote": "sim"}),
                encoding="utf-8",
            )
            with patch("ollamapilot.config.SETTINGS_FILE", path):
                loaded = Settings.load()
            self.assertEqual(loaded.model, "qwen")
            self.assertEqual(loaded.num_ctx, 8192)
            self.assertFalse(loaded.model_remote)

    def test_invalid_settings_are_parked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{", encoding="utf-8")
            with patch("ollamapilot.config.SETTINGS_FILE", path):
                loaded = Settings.load()
                loaded.model = "qwen"
                loaded.save()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["model"], "qwen")
            self.assertEqual(Path(str(path) + ".invalido").read_text(encoding="utf-8"), "{")

    def test_one_bad_attachment_does_not_hide_the_other(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            good = Path(folder) / "a.txt"
            good.write_bytes("olá".encode("utf-8"))
            long_file = Path(folder) / "longo.txt"
            long_file.write_text("x" * 60_000, encoding="utf-8")
            first = read_attachment(good)
            second = read_attachment(long_file)
            missing = read_attachment(Path(folder) / "sumiu.txt")
        self.assertTrue(first["ok"])
        self.assertEqual(first["anexo"]["codificacao"], "utf-8")
        self.assertTrue(second["anexo"]["truncado"])
        self.assertFalse(missing["ok"])
        self.assertIn("sumiu.txt", missing["erro"])

    def test_tool_json_stays_out_of_the_bubble(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "index.html"
            payload = json.dumps(
                {"name": "gravar_arquivo", "arguments": {"caminho": str(target), "conteudo": "<html></html>"}},
                ensure_ascii=False,
            )
            client = FakeClient(
                [
                    {"content": "```json\n" + payload + "\n```", "tool_calls": []},
                    {"content": "Gravei o index.html.", "tool_calls": []},
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            events = []
            answer = agent.turn(
                f"Crie a página e salve em {folder}",
                [],
                lambda kind, data: events.append((kind, data)),
                lambda _preview: "once",
                lambda: False,
            )
            shown = ""
            for kind, data in events:
                if kind == "content_reset":
                    shown = ""
                elif kind == "content":
                    shown += str(data)
            self.assertEqual(answer, "Gravei o index.html.")
            self.assertEqual(shown, "Gravei o index.html.")
            self.assertNotIn("não posso", shown)
            self.assertNotIn("gravar_arquivo", shown)
            self.assertEqual(target.read_text(encoding="utf-8"), "<html></html>")

    def test_refusal_and_loose_json_are_not_the_reply(self) -> None:
        loose = '{ "name": "gravar_arquivo", "arguments": { "conteudo": "<html lang="pt-BR">'
        client = FakeClient(
            [
                {"content": "Desculpe, mas não posso criar ou salvar arquivos diretamente.", "tool_calls": []},
                {"content": loose, "tool_calls": []},
                {"content": "Desculpe, mas não posso criar ou salvar arquivos.", "tool_calls": []},
            ]
        )
        agent = Agent(Settings(model="teste"), client)
        events = []
        answer = agent.turn(
            r"Crie o site e salve em c:\xampp\htdocs\temp",
            [],
            lambda kind, data: events.append((kind, data)),
            lambda _preview: "once",
            lambda: False,
        )
        shown = ""
        for kind, data in events:
            if kind == "content_reset":
                shown = ""
            elif kind == "content":
                shown += str(data)
        self.assertIn("não chamou a gravação", answer)
        self.assertEqual(shown, answer)
        self.assertNotIn("gravar_arquivo", shown)
        self.assertNotIn("não posso", shown)

    def test_write_request_does_not_stop_at_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "index.html"
            client = FakeClient(
                [
                    {
                        "content": "Desculpe, mas não posso criar arquivos.\n```html\n<!DOCTYPE html><html></html>\n```",
                        "tool_calls": [],
                    },
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "gravar_arquivo",
                                    "arguments": {"caminho": str(target), "conteudo": "<html></html>"},
                                }
                            }
                        ],
                    },
                    {"content": "Gravei a página.", "tool_calls": []},
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            events = []
            answer = agent.turn(
                f"Crie o site e salve em {folder}",
                [],
                lambda kind, data: events.append(kind),
                lambda _preview: "once",
                lambda: False,
            )
            self.assertEqual(answer, "Gravei a página.")
            self.assertIn("content_reset", events)
            self.assertEqual(target.read_text(encoding="utf-8"), "<html></html>")
            self.assertNotIn("não posso", agent.transcript[-1]["content"])

    def test_log_omits_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch("ollamapilot.logbook.LOGS", Path(folder)):
                path = log_error("turno", "falhou", cloud_api_key="segredo-da-conta")
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("segredo-da-conta", text)
        self.assertIn("[omitido]", text)


class ProgramCatalogTests(unittest.TestCase):
    def test_program_list_drops_incomplete_rows_and_uses_its_timeout(self) -> None:
        from ollamapilot.config import normalize_programs
        from ollamapilot.tools.programs import registered_command_timeout

        clean = normalize_programs(
            [
                {"nome": "Analisador", "caminho": r"C:\Ferramentas\analisa.exe", "tempo_comando": "30", "tempo_analise": 4000},
                {"nome": "", "caminho": r"C:\vazio.exe"},
                "texto",
            ]
        )
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["tempo_comando"], 30)
        self.assertEqual(clean[0]["tempo_analise"], 4000)
        settings = Settings(programs=clean)
        self.assertEqual(registered_command_timeout(settings, "analisa.exe"), 30)
        self.assertIsNone(registered_command_timeout(settings, "outro.exe"))


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_settings_are_tabbed_and_programs_are_generic(self) -> None:
        from PySide6.QtWidgets import QPushButton, QTabWidget

        from ollamapilot.ollama_client import OllamaClient
        from ollamapilot.ui.dialogs import KnowledgeDialog, ProgramsDialog, SettingsDialog
        from ollamapilot.ui.icons import paperclip_icon, sidebar_icon

        settings = Settings()
        dialog = SettingsDialog(settings, OllamaClient(settings))
        tabs = dialog.findChild(QTabWidget)
        self.assertEqual([tabs.tabText(index) for index in range(tabs.count())], ["Conexão", "Geração", "Programas", "Recursos"])
        programs = ProgramsDialog(settings)
        labels = [button.text() for button in programs.findChildren(QPushButton)]
        self.assertIn("Adicionar", labels)
        self.assertNotIn("Testar Ghidra", labels)
        library = KnowledgeDialog(settings, OllamaClient(settings))
        self.assertEqual(library.windowTitle(), "Biblioteca (RAG)")
        names = ("plus", "chat", "history", "library", "programs", "files", "export", "models", "settings")
        sizes = {sidebar_icon(name).availableSizes()[0] for name in names}
        self.assertEqual(len(sizes), 1)
        self.assertFalse(paperclip_icon().isNull())
        dialog.deleteLater()
        programs.deleteLater()
        library.deleteLater()

    def test_model_error_stays_inside_the_dialog_width(self) -> None:
        from PySide6.QtCore import Qt

        from ollamapilot.ui.dialogs import ModelsDialog

        class Quiet:
            def available_models(self):
                return {"models": [{"name": "gemma4:31b", "size": 20_000_000_000}], "version": {"version": "0.0.0"}, "remote": []}

        dialog = ModelsDialog(Settings(), Quiet(), lambda _payload: None)
        if dialog._job is not None:
            dialog._job.wait(2000)
        self.app.processEvents()
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.show()
        self.app.processEvents()
        before = dialog.width()
        dialog.pull_status.setText('Falha ao baixar: Get "https://registry.example/' + ("a" * 4000) + '"\n\nRegistro: E:\\Projetos com IA\\OllamaPilot\\storage\\logs\\erro.log')
        self.app.processEvents()
        dialog.adjustSize()
        self.app.processEvents()
        self.assertLessEqual(dialog.width(), before + 40)
        self.assertLessEqual(dialog.sizeHint().width(), 760)
        self.assertGreater(dialog.pull_status.height(), 22)
        self.assertLessEqual(dialog.pull_status.height(), 112)
        dialog.close()
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
