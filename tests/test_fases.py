from __future__ import annotations

import json
import os

os.environ.setdefault("OLLAMAPILOT_VAULT", "memory")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ollamapilot import undo
from ollamapilot.agent import Agent, _tool_spans, groups_for
from ollamapilot.config import Settings
from ollamapilot.ollama_client import OllamaClient, to_openai_messages
from ollamapilot.process_run import OutputCapture, decode_output
from ollamapilot.project_rules import project_rules
from ollamapilot.prompt import build_prompt
from ollamapilot.tools.database import guard_select
from ollamapilot.tools.filesystem import ReadFile
from ollamapilot.tools.programs import _args
from ollamapilot.tools.registry import build_registry


class ReadRangeTests(unittest.TestCase):
    def test_range_reaches_lines_past_the_read_limit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "grande.log"
            target.write_text("".join(f"linha {n} " + "x" * 60 + "\n" for n in range(1, 6001)), encoding="utf-8")
            self.assertGreater(target.stat().st_size, 200_000)
            result = ReadFile(Settings()).run({"caminho": str(target), "inicio": 5500, "fim": 5501, "numerar": True})
        self.assertEqual(result["inicio"], 5500)
        self.assertEqual(result["fim"], 5501)
        self.assertTrue(result["conteudo"].startswith("  5500|linha 5500 "))
        self.assertIn("5501|linha 5501", result["conteudo"])

    def test_range_past_the_end_says_how_many_lines(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "curto.txt"
            target.write_text("a\nb\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "2 linhas"):
                ReadFile(Settings()).run({"caminho": str(target), "inicio": 9})


class OutputTests(unittest.TestCase):
    def test_console_accents_in_cp850_are_readable(self) -> None:
        self.assertEqual(decode_output("Não há ação".encode("cp850")), "Não há ação")
        self.assertEqual(decode_output("ação".encode("utf-8")), "ação")

    def test_capture_keeps_the_start_and_the_end(self) -> None:
        capture = OutputCapture(4096)
        capture.add(b"INICIO\n" + b"m" * 20000)
        capture.add(b"\nERRO NO FIM\n")
        text = capture.text()
        self.assertTrue(capture.truncated)
        self.assertTrue(text.startswith("INICIO"))
        self.assertIn("ERRO NO FIM", text)
        self.assertIn("omitidos", text)


class BatchArgumentTests(unittest.TestCase):
    def test_batch_file_refuses_shell_metacharacters(self) -> None:
        with self.assertRaises(ValueError):
            _args(["arquivo.txt & del C:\\x"], r"C:\ferramentas\build.bat")
        with self.assertRaises(ValueError):
            _args(["a\nb"], r"C:\ferramentas\build.cmd")
        self.assertEqual(_args(["a & b"], r"C:\ferramentas\app.exe"), ["a & b"])


class ToolTextTests(unittest.TestCase):
    def test_call_after_a_stray_brace_is_found(self) -> None:
        registry = build_registry(Settings(), None, lambda: False)
        text = 'Use { com cuidado. {"name": "ler_arquivo", "arguments": {"caminho": "C:\\\\a.txt"}}'
        spans = _tool_spans(text, registry)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0][2]["name"], "ler_arquivo")


class DatabaseSettingsTests(unittest.TestCase):
    def test_old_checkbox_loads_as_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"db_enabled": true, "db_name": "loja"}', encoding="utf-8")
            with patch("ollamapilot.config.SETTINGS_FILE", path), patch("ollamapilot.secrets.current") as store:
                store.return_value.get.return_value = ""
                loaded = Settings.load()
        self.assertEqual(loaded.db_access, "leitura")
        self.assertEqual(loaded.db_name, "loja")


class DatabaseGuardTests(unittest.TestCase):
    def test_words_inside_quotes_are_not_commands(self) -> None:
        guard_select("SELECT * FROM pedidos WHERE status = 'delete' AND nota = 'a; b'")
        guard_select('SELECT "update" FROM t')
        with self.assertRaises(ValueError):
            guard_select("DELETE FROM pedidos")
        with self.assertRaises(ValueError):
            guard_select("SELECT 1; DROP TABLE t")

    def test_write_needs_the_setting_and_one_statement(self) -> None:
        from ollamapilot.tools.database import DatabaseExecute, guard_write, is_other_database, refuse_postgres_shell

        body = "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql"
        self.assertTrue(guard_write(body).startswith("CREATE FUNCTION"))
        with self.assertRaises(ValueError):
            guard_write("CREATE TABLE t (id int); DROP TABLE t")
        with self.assertRaises(ValueError):
            guard_write("SELECT 1")
        reading = Settings(db_access="leitura", db_name="loja")
        tool = DatabaseExecute(reading)
        with self.assertRaises(ValueError) as blocked:
            tool.preview({"sql": "CREATE TABLE nova (id int)"})
        self.assertIn("somente leitura", str(blocked.exception))
        writing = Settings(db_access="escrita", db_name="loja", db_host="127.0.0.1", db_port=5432)
        preview = DatabaseExecute(writing).preview({"sql": "CREATE TABLE nova (id int)", "banco": "outro"})
        self.assertIn("outro", preview["detail"])
        self.assertIn("CREATE TABLE nova", preview["detail"])
        self.assertTrue(is_other_database(writing, {"banco": "outro"}))
        self.assertFalse(is_other_database(writing, {}))
        with self.assertRaises(ValueError):
            refuse_postgres_shell(reading, "psql", 'psql -c "CREATE TABLE nova (id int)"')
        refuse_postgres_shell(writing, "psql", 'psql -c "CREATE TABLE nova (id int)"')


class GroupTests(unittest.TestCase):
    def test_window_tools_only_when_the_request_talks_about_windows(self) -> None:
        registry = build_registry(Settings(), None, lambda: False, groups=groups_for("Leia E:\\app\\main.py"))
        names = registry.names()
        self.assertIn("ler_arquivo", names)
        self.assertIn("executar_programa", names)
        self.assertNotIn("acionar_janela", names)
        self.assertNotIn("ghidra_analisar", names)
        self.assertTrue(registry.has("acionar_janela"))
        windows = build_registry(Settings(), None, lambda: False, groups=groups_for("Clique no botão da janela"))
        self.assertIn("acionar_janela", windows.names())

    def test_prompt_leaves_clean_room_out_without_reverse_tools(self) -> None:
        names = build_registry(Settings(), None, lambda: False, groups=set()).names()
        prompt = build_prompt(Settings(), names, [], "", ["ghidra_analisar"])
        self.assertNotIn("Sala limpa", prompt)
        self.assertIn("PDF, DOCX e XLSX", prompt)
        self.assertIn("ghidra_analisar", prompt)
        full = build_prompt(Settings(), names + ["ghidra_analisar"], [])
        self.assertIn("Sala limpa", full)

    def test_all_setting_sends_every_group(self) -> None:
        agent = Agent(Settings(model="m", tool_selection="todas"), None)
        self.assertIsNone(agent._groups())
        agent = Agent(Settings(model="m"), None)
        agent.transcript.append({"role": "user", "content": "abra a calculadora"})
        self.assertIn("janelas", agent._groups())


class SearchToolTests(unittest.TestCase):
    def test_search_text_and_find_files(self) -> None:
        registry = build_registry(Settings(), None, lambda: False)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("def total():\n    return calcular_imposto(1)\n", encoding="utf-8")
            (root / "src" / "util.js").write_text("calcular_imposto()\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "x.py").write_text("calcular_imposto\n", encoding="utf-8")
            (root / ".env").write_text("calcular_imposto=segredo\n", encoding="utf-8")
            found = registry.get("buscar_texto").run({"pasta": folder, "padrao": "calcular_imposto", "arquivos": "*.py"})
            files = registry.get("localizar_arquivos").run({"pasta": folder, "padrao": "*.js"})
        self.assertEqual([(Path(item["arquivo"]).name, item["linha"]) for item in found["resultados"]], [("app.py", 2)])
        self.assertNotIn("segredo", json.dumps(found))
        self.assertEqual([Path(item["caminho"]).name for item in files["arquivos"]], ["util.js"])


class TransferAndTurnTests(unittest.TestCase):
    def test_move_is_undone_by_its_plan(self) -> None:
        registry = build_registry(Settings(), None, lambda: False)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "a.txt"
            target = Path(folder) / "sub" / "b.txt"
            source.write_text("conteudo", encoding="utf-8")
            with patch("ollamapilot.undo.paths.BACKUPS", Path(folder) / "copias"):
                result = registry.get("mover_arquivo").run({"origem": str(source), "destino": str(target)})
                self.assertFalse(source.exists())
                self.assertEqual(target.read_text(encoding="utf-8"), "conteudo")
                undo.restore_plan(result["plano"])
            self.assertEqual(source.read_text(encoding="utf-8"), "conteudo")
            self.assertFalse(target.exists())

    def test_restore_turn_brings_back_the_files_of_that_turn(self) -> None:
        registry = build_registry(Settings(), None, lambda: False)
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "velho.txt"
            existing.write_text("antes\n", encoding="utf-8")
            created = Path(folder) / "novo.txt"
            with patch("ollamapilot.undo.paths.BACKUPS", Path(folder) / "copias"):
                undo.CURRENT_TURN = "t1"
                try:
                    registry.get("gravar_arquivo").run({"caminho": str(existing), "conteudo": "depois\n"})
                    registry.get("gravar_arquivo").run({"caminho": str(created), "conteudo": "oi\n"})
                finally:
                    undo.CURRENT_TURN = ""
                self.assertEqual(undo.turn_changes("t1"), 2)
                undo.restore_turn("t1")
                self.assertEqual(undo.turn_changes("t1"), 0)
            self.assertEqual(existing.read_text(encoding="utf-8"), "antes\n")
            self.assertFalse(created.exists())


class BatchPreviewTests(unittest.TestCase):
    def test_batch_card_shows_what_each_write_contains(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = str(Path(folder) / "a.txt")
            second = str(Path(folder) / "b.txt")

            class Client:
                calls = 0

                def chat(self, messages, tools, on_event, cancel):
                    Client.calls += 1
                    if Client.calls == 1:
                        return {
                            "content": "",
                            "tool_calls": [
                                {"function": {"name": "gravar_arquivo", "arguments": {"caminho": first, "conteudo": "LINHA-A"}}},
                                {"function": {"name": "gravar_arquivo", "arguments": {"caminho": second, "conteudo": "LINHA-B"}}},
                            ],
                        }
                    return {"content": "Pronto.", "tool_calls": []}

            details = []

            def approve(preview):
                details.append(preview.get("detail", ""))
                return "once"

            with patch("ollamapilot.undo.paths.BACKUPS", Path(folder) / "copias"):
                Agent(Settings(model="teste"), Client()).turn(f"Grave {first} e {second}", [], lambda *_: None, approve, lambda: False)
        self.assertEqual(len(details), 1)
        self.assertIn("LINHA-A", details[0])
        self.assertIn("LINHA-B", details[0])


class OpenAITests(unittest.TestCase):
    def test_conversation_is_converted_with_call_ids(self) -> None:
        messages = [
            {"role": "system", "content": "regras"},
            {"role": "user", "content": "veja", "images": ["iVBORw0KGgo="]},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "ler_arquivo", "arguments": {"caminho": "C:\\a"}}}]},
            {"role": "tool", "tool_name": "ler_arquivo", "content": "{}"},
        ]
        converted = to_openai_messages(messages)
        self.assertEqual(converted[1]["content"][1]["image_url"]["url"][:22], "data:image/png;base64,")
        call = converted[2]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"caminho": "C:\\a"})
        self.assertEqual(converted[3]["tool_call_id"], call["id"])

    def test_streamed_tool_call_is_rebuilt(self) -> None:
        lines = [
            'data: {"choices":[{"delta":{"content":"Vou ler."}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"ler_arquivo","arguments":"{\\"cami"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"nho\\": \\"C:\\\\\\\\a\\"}"}}]},"finish_reason":"tool_calls"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":5}}',
            "data: [DONE]",
        ]

        class Response:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def iter_lines(self):
                return iter(lines)

            def close(self):
                pass

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def stream(self, method, url, json=None, headers=None):
                self.payload = json
                return Response()

        settings = Settings(model="local-model", provider="openai", openai_url="http://127.0.0.1:1234/v1")
        with patch("ollamapilot.ollama_client.httpx.Client", Client):
            reply = OllamaClient(settings).chat([{"role": "user", "content": "oi"}], [], lambda *_: None, lambda: False)
        self.assertEqual(reply["content"], "Vou ler.")
        self.assertEqual(reply["tool_calls"], [{"function": {"name": "ler_arquivo", "arguments": {"caminho": "C:\\a"}}}])
        self.assertEqual(reply["metrics"]["prompt_eval_count"], 12)


class DocumentReadTests(unittest.TestCase):
    def test_named_pdf_is_read_as_text(self) -> None:
        from fpdf import FPDF

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "exame.pdf"
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Helvetica", size=12)
            pdf.cell(0, 10, "Resultado do exame 42")
            pdf.output(str(target))
            result = ReadFile(Settings()).run({"caminho": str(target)})
            binary = build_registry(Settings(), None, lambda: False).get("ler_binario").run({"caminho": str(target)})
        self.assertIn("Resultado do exame 42", result["conteudo"])
        self.assertEqual(result["paginas"], 1)
        self.assertIn("ler_arquivo", binary["observacao"])

    def test_scanned_pdf_pages_go_to_a_vision_model(self) -> None:
        from PIL import Image
        from fpdf import FPDF

        from ollamapilot.tools.base import Tool

        with tempfile.TemporaryDirectory() as folder:
            picture = Path(folder) / "folha.png"
            Image.new("RGB", (320, 180), "white").save(picture)
            target = Path(folder) / "laudo.pdf"
            pdf = FPDF()
            for _ in range(5):
                pdf.add_page()
                pdf.image(str(picture), x=10, y=10, w=100)
            pdf.output(str(target))
            tool = ReadFile(Settings(model="gemma4:12b"))
            blind = Agent(Settings(model="gemma4:12b"), None)
            blind._caps = {"conhecido": True, "visao": False}
            tool.ctx = type("Ctx", (), {"agent": blind})()
            with self.assertRaisesRegex(ValueError, "não tem visão"):
                tool.run({"caminho": str(target)})
            unknown = Agent(Settings(model="gemma4:12b"), None)
            tool.ctx = type("Ctx", (), {"agent": unknown})()
            with self.assertRaisesRegex(ValueError, "não informam visão"):
                tool.run({"caminho": str(target)})
            seeing = Agent(Settings(model="gemma4:12b"), None)
            seeing._caps = {"conhecido": True, "visao": True}
            tool.ctx = type("Ctx", (), {"agent": seeing})()
            result = tool.run({"caminho": str(target)})
            self.assertEqual(result["paginas_anexadas"], [1, 2, 3, 4])
            self.assertEqual(result["proxima_pagina"], 5)
            self.assertEqual(len(result["_imagem"]), 4)
            rest = tool.run({"caminho": str(target), "inicio": 5})
            self.assertEqual(rest["paginas_anexadas"], [5])
            self.assertNotIn("proxima_pagina", rest)

        class Picture(Tool):
            name = "foto_teste"
            parameters = {"type": "object", "properties": {}}

            def run(self, arguments):
                return {"mensagem": "ok", "_imagem": ["aaa", "bbb"], "_imagem_nota": "nota das paginas"}

        agent = Agent(Settings(model="gemma4:12b"), None)
        registry = type("Reg", (), {"get": lambda self, name: Picture(), "names": lambda self: ["foto_teste"]})()
        status, returned = agent._handle(
            {"name": "foto_teste", "arguments": {}, "known": True},
            registry,
            lambda preview: "once",
            lambda *_: None,
            lambda: False,
            {},
            {"erros_ferramenta": 0, "redundantes": 0},
        )
        self.assertEqual(status, "success")
        self.assertNotIn("_imagem", returned)
        self.assertEqual(agent._images, ["aaa", "bbb"])
        self.assertIn("nota das paginas", agent._image_note)

    def test_other_binary_still_points_to_ler_binario(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "dados.bin"
            target.write_bytes(b"MZ\x00\x00resto")
            with self.assertRaisesRegex(ValueError, "ler_binario"):
                ReadFile(Settings()).run({"caminho": str(target)})


class ProjectRulesTests(unittest.TestCase):
    def test_rules_file_of_the_cited_folder_enters_the_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "meu projeto"
            (root / "src").mkdir(parents=True)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("Use tabs.\nVerificar: py -3 -m pytest\n", encoding="utf-8")
            text = project_rules(f"Corrija {root / 'src' / 'app.py'} por favor")
        self.assertIn("Use tabs.", text)
        self.assertIn("py -3 -m pytest", text)
        self.assertIn("não autorizam", text)
        self.assertEqual(project_rules("sem caminho nenhum"), "")


if __name__ == "__main__":
    unittest.main()
