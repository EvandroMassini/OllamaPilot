"""Aceites da versão de alto nível. Modelo simulado, pasta temporária, sem Ollama e sem janela."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("OLLAMAPILOT_VAULT", "memory")

from ollamapilot.agent import Agent, groups_for
from ollamapilot.attachments import read_attachment
from ollamapilot.config import Settings
from ollamapilot.ollama_client import OllamaClient
from ollamapilot.project_index import find_symbols, index_project
from ollamapilot.tools.desktop import DriveWindow
from ollamapilot.tools.filesystem import ReadFile
from ollamapilot.tools.gittools import GitCommit, GitStatus
from ollamapilot.tools.project import SearchProject


def _step(content="", calls=None):
    return {"content": content, "tool_calls": calls or []}


def _call(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


class Script:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0
        self.seen = []

    def capabilities(self):
        return {}

    def chat(self, messages, tools, on_event, cancel):
        self.seen.append(messages)
        step = self.steps[self.calls]
        self.calls += 1
        if step.get("content"):
            on_event("content", step["content"])
        return step


def _turn(agent, text, approve=None):
    return agent.turn(text, [], lambda *_: None, approve or (lambda _preview: "once"), lambda: False)


class WorkspaceGrantTests(unittest.TestCase):
    def test_outside_write_asks_again_and_inside_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory() as outside:
            root = Path(inside)
            target = root / "dentro.txt"
            foreign = Path(outside) / "fora.txt"
            client = Script(
                [
                    _step(calls=[_call("gravar_arquivo", caminho=str(target), conteudo="um")]),
                    _step("primeiro"),
                    _step(calls=[_call("gravar_arquivo", caminho=str(foreign), conteudo="dois")]),
                    _step("segundo"),
                    _step(calls=[_call("gravar_arquivo", caminho=str(root / "de novo.txt"), conteudo="tres")]),
                    _step("terceiro"),
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            agent.workspace = root
            cards = []

            def approve(preview):
                cards.append(preview)
                return "chat"

            _turn(agent, "grave o primeiro", approve)
            _turn(agent, "grave fora", approve)
            _turn(agent, "grave de novo", approve)
            self.assertEqual(len(cards), 2)
            self.assertTrue(cards[0]["dentro"])
            self.assertEqual(cards[0]["raiz"], str(root))
            self.assertFalse(cards[1]["dentro"])
            self.assertIn("caminho:", " ".join(agent.chat_grants))
            self.assertEqual(target.read_text(encoding="utf-8"), "um")
            self.assertEqual(foreign.read_text(encoding="utf-8"), "dois")
            self.assertEqual((root / "de novo.txt").read_text(encoding="utf-8"), "tres")


class HeadlessTests(unittest.TestCase):
    def test_message_mode_writes_without_qt(self) -> None:
        from ollamapilot import cli

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "piloto.txt"
            box = {"path": str(target), "text": "piloto"}

            class Fake:
                def __init__(self, settings):
                    self.n = 0

                def capabilities(self):
                    return {}

                def chat(self, messages, tools, on_event, cancel):
                    self.n += 1
                    if self.n == 1:
                        return _step(calls=[_call("gravar_arquivo", caminho=box["path"], conteudo=box["text"])])
                    return _step("Arquivo criado.")

            out, err = StringIO(), StringIO()
            with patch("ollamapilot.cli.OllamaClient", Fake), patch("sys.stdout", out), patch("sys.stderr", err):
                code = cli.main(["--mensagem", "grave", "--pasta", folder, "--modelo", "teste", "--aprovar", "workspace"])
            self.assertEqual(code, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "piloto")
            self.assertIn("Arquivo criado.", out.getvalue())
            self.assertNotIn("PySide6", cli.__file__)

            foreign = Path(tempfile.mkdtemp()) / "fora.txt"
            box["path"] = str(foreign)
            box["text"] = "nao"
            out2, err2 = StringIO(), StringIO()
            with patch("ollamapilot.cli.OllamaClient", Fake), patch("sys.stdout", out2), patch("sys.stderr", err2):
                code = cli.main(["--mensagem", "grave fora", "--pasta", folder, "--modelo", "teste", "--aprovar", "workspace"])
            self.assertEqual(code, 0)
            self.assertFalse(foreign.exists())
            self.assertIn("Recusado sem cartão", err2.getvalue())


class ContextTests(unittest.TestCase):
    def test_legacy_ceiling_rises_and_small_model_stays_capped(self) -> None:
        sent = {}

        class Response:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_lines(self):
                yield json.dumps({"message": {"content": "oi"}, "done": True})

            def close(self):
                pass

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, method, url, json=None, headers=None):
                sent.clear()
                sent.update(json or {})
                return Response()

        with tempfile.TemporaryDirectory() as folder:
            settings_file = Path(folder) / "settings.json"
            with patch("ollamapilot.config.SETTINGS_FILE", settings_file), patch("ollamapilot.ollama_client.httpx.Client", Client):
                wide = Settings(model="m", num_ctx=32768)
                client = OllamaClient(wide)
                client._caps[(False, client._base(), "m")] = {"conhecido": True, "contexto": 131072}
                client.chat([{"role": "user", "content": "oi"}], [], lambda *_: None, lambda: False)
                self.assertEqual(sent["options"]["num_ctx"], 65536)
                self.assertEqual(wide.num_ctx, 65536)

                small = Settings(model="m", num_ctx=32768)
                client.settings = small
                client._caps[(False, client._base(), "m")] = {"conhecido": True, "contexto": 8192}
                client.chat([{"role": "user", "content": "oi"}], [], lambda *_: None, lambda: False)
                self.assertEqual(sent["options"]["num_ctx"], 8192)
                self.assertEqual(small.num_ctx, 32768)


class GroupTests(unittest.TestCase):
    def test_words_match_on_boundaries(self) -> None:
        path = r"E:\Projetos com IA\OllamaPilot\leia.txt"
        self.assertNotIn("projeto", groups_for(path))
        self.assertNotIn("web", groups_for(path))
        self.assertIn("projeto", groups_for("veja o projeto"))
        self.assertIn("web", groups_for("consulte a API"))
        self.assertIn("terminal", groups_for("git status"))
        self.assertNotIn("terminal", groups_for("digitação rápida"))


class StallTests(unittest.TestCase):
    def test_repeated_write_keeps_the_success(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nota.txt"
            write = _call("gravar_arquivo", caminho=str(target), conteudo="ok")
            client = Script([_step(calls=[write]), _step(calls=[write]), _step(calls=[write]), _step("Fechei.")])
            agent = Agent(Settings(model="teste"), client)
            agent.workspace = Path(folder)
            answer = _turn(agent, "grave a nota")
            blob = "\n".join(str(item.get("content") or "") for item in agent.transcript)
            self.assertEqual(target.read_text(encoding="utf-8"), "ok")
            self.assertIn(str(target), blob)
            self.assertIn("sucesso", blob)
            self.assertEqual(answer, "Fechei.")


class VerifyTests(unittest.TestCase):
    def test_code_patch_is_noted_and_text_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "conta.py"
            source.write_text("valor = 1\n", encoding="utf-8")
            patch = _call(
                "aplicar_patch",
                caminho=str(source),
                blocos=[{"buscar": "valor = 1", "substituir": "valor = 2"}],
            )
            client = Script([_step(calls=[patch]), _step("Pronto.")])
            agent = Agent(Settings(model="teste"), client)
            agent.workspace = root
            _turn(agent, "aplique o patch")
            roles = [(item.get("role"), str(item.get("content") or "")) for item in agent.transcript]
            notes = [text for role, text in roles if "A alteração está no disco." in text]
            self.assertEqual(len(notes), 1)
            final = next(index for index, (role, text) in enumerate(roles) if role == "assistant" and text == "Pronto.")
            noted = next(index for index, (_role, text) in enumerate(roles) if "A alteração está no disco." in text)
            self.assertLess(noted, final)
            self.assertIn("valor = 2", source.read_text(encoding="utf-8"))

            plain = root / "nota.txt"
            client = Script(
                [
                    _step(calls=[_call("gravar_arquivo", caminho=str(plain), conteudo="texto")]),
                    _step("Gravei."),
                ]
            )
            agent = Agent(Settings(model="teste"), client)
            agent.workspace = root
            _turn(agent, "grave o txt")
            blob = "\n".join(str(item.get("content") or "") for item in agent.transcript)
            self.assertNotIn("A alteração está no disco.", blob)


class GitTests(unittest.TestCase):
    def test_status_commit_and_force(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._git(root, ["init"])
            self._git(root, ["config", "user.email", "teste@example.com"])
            self._git(root, ["config", "user.name", "Teste"])
            (root / "nota.txt").write_text("oi\n", encoding="utf-8")
            ctx = type("Ctx", (), {})()
            ctx.agent = type("Agent", (), {"workspace": root})()
            status = GitStatus()
            status.ctx = ctx
            listed = status.run({})
            self.assertIn("nota.txt", listed["stdout"])
            commit = GitCommit()
            commit.ctx = ctx
            done = commit.run({"mensagem": "registra a nota", "caminhos": ["nota.txt"]})
            self.assertTrue(done.get("rodou"))
            log = self._git(root, ["log", "--oneline"])
            self.assertIn("registra a nota", log.stdout)
            refused = commit.run({"mensagem": "nao use --force", "caminhos": ["nota.txt"]})
            self.assertFalse(refused.get("rodou"))
            refused_arg = commit.run({"mensagem": "mais uma", "caminhos": ["--force"]})
            self.assertFalse(refused_arg.get("rodou"))
            again = self._git(root, ["log", "--oneline"])
            self.assertEqual(log.stdout, again.stdout)

    def _git(self, root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )


class DocumentTests(unittest.TestCase):
    def test_xlsx_keeps_formula_and_numbers(self) -> None:
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "conta.xlsx"
            book = Workbook()
            sheet = book.active
            sheet["B2"] = 400
            sheet["B3"] = 500
            sheet["B4"] = 600
            sheet["B5"] = "=SUM(B2:B4)"
            book.save(path)
            loaded = read_attachment(path)
            text = loaded["anexo"]["text"]
            self.assertIn("=SUM(B2:B4)", text)
            self.assertIn("400", text)
            self.assertIn("500", text)
            self.assertIn("600", text)

    def test_blank_pdf_is_raster_or_a_missing_library(self) -> None:
        from fpdf import FPDF

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vazio.pdf"
            pdf = FPDF()
            pdf.add_page()
            pdf.output(str(path))
            tool = ReadFile(Settings(model="teste"))
            seeing = Agent(Settings(model="teste"), Script([]))
            seeing._caps = {"conhecido": True, "visao": True}
            tool.ctx = type("Ctx", (), {"agent": seeing})()
            try:
                result = tool.run({"caminho": str(path)})
            except ValueError as error:
                message = str(error)
                self.assertIn("pypdfium2", message)
                self.assertIn("não foi lido", message)
            else:
                self.assertTrue(result.get("_imagem"))
                blob = json.dumps(result, ensure_ascii=False)
                self.assertNotIn("lorem", blob.lower())

    def test_pptx_returns_slide_text(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "um.pptx"
            xml = (
                '<?xml version="1.0"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                "<p:cSld><p:spTree><a:t>Texto do slide</a:t></p:spTree></p:cSld></p:sld>"
            )
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ppt/slides/slide1.xml", xml)
            loaded = read_attachment(path)
            text = loaded["anexo"]["text"]
            self.assertIn("Texto do slide", text)
            self.assertIn("[slide 1]", text)


class SearchWithoutEmbedTests(unittest.TestCase):
    def test_lexical_hit_survives_a_failed_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "nota.py").write_text("TOKENUNICO = 1\n", encoding="utf-8")

            def embed(_texts):
                raise ConnectionError("model 'embeddinggemma' not found")

            with patch("ollamapilot.project_index.STORAGE", root / "idx"):
                report = index_project(root, embed)
                self.assertFalse(report.get("busca_vetorial"))

                class Client:
                    def embed(self, texts):
                        raise ConnectionError("model 'embeddinggemma' not found")

                found = SearchProject(Settings(model="t", embed_model="embeddinggemma"), Client()).run(
                    {"caminho": str(root), "consulta": "TOKENUNICO"}
                )
            blob = json.dumps(found, ensure_ascii=False)
            self.assertIn("nota.py", blob)
            self.assertIn("desligada", found.get("mensagem", ""))
            self.assertIn("embeddinggemma", found["mensagem"])


class MemoryTaskTests(unittest.TestCase):
    def test_memory_is_visible_to_the_next_agent_and_can_be_forgotten(self) -> None:
        from ollamapilot.prompt import build_prompt
        from ollamapilot.tools.continuity import Forget, Remember

        with tempfile.TemporaryDirectory() as folder:
            with patch("ollamapilot.paths.STORAGE", Path(folder)):
                Remember().run({"texto": "o gato se chama Milo"})
                self.assertIn("Milo", build_prompt(Settings(model="t"), ["lembrar"]))
                client = Script([_step("oi")])
                agent = Agent(Settings(model="t"), client)
                _turn(agent, "olá")
                self.assertIn("Milo", client.seen[0][0]["content"])
                Forget().run({"texto": "Milo"})
                self.assertNotIn("Milo", build_prompt(Settings(model="t"), ["lembrar"]))

    def test_pending_task_is_in_the_internal_note(self) -> None:
        client = Script([_step("Terminei."), _step("Ainda falta.")])
        agent = Agent(Settings(model="t"), client)
        agent.tasks = [{"texto": "publicar a nota", "estado": "pendente"}]
        answer = _turn(agent, "pode encerrar")
        notes = [str(item.get("content") or "") for item in agent.transcript if item.get("interno")]
        self.assertTrue(any("publicar a nota" in note and "pendente" in note for note in notes))
        self.assertEqual(answer, "Ainda falta.")


class SymbolTests(unittest.TestCase):
    def test_symbol_search_returns_file_and_line(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "frete.py").write_text("x = 1\ndef calcular_frete():\n    return 1\n", encoding="utf-8")
            rows = find_symbols(root, "calcular_frete")
            self.assertTrue(any(row["arquivo"].endswith("frete.py") and row["linha"] == 2 for row in rows))

    def test_click_preview_shows_window_point_and_button(self) -> None:
        preview = DriveWindow(Settings(model="t")).preview(
            {"titulo": "Bloco de notas", "acoes": [{"acao": "clicar", "nome": "OK", "x": 12, "y": 40}]}
        )
        self.assertIn("Bloco de notas", preview["detail"])
        self.assertIn("ponto (12, 40)", preview["detail"])
        self.assertIn("OK", preview["detail"])
