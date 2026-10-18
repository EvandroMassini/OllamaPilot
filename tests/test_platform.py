from __future__ import annotations

import json
import os

os.environ.setdefault("OLLAMAPILOT_VAULT", "memory")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ollamapilot.agent import Agent, _mutation_blocked, _outgoing
from ollamapilot.config import Settings
from ollamapilot.privacy import for_disk, redact
from ollamapilot.secrets import MemoryStore, current
from ollamapilot.tools.filesystem import WriteFile
from ollamapilot.tools.patching import ApplyPatch
from ollamapilot.tools.planning import ProposePlan
from ollamapilot.tools.registry import build_registry
from ollamapilot import undo
from ollamapilot.project_index import index_project, search_project


class VaultTests(unittest.TestCase):
    def test_settings_keep_secrets_out_of_the_json(self) -> None:
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"cloud_api_key": "chave-da-conta-123", "model": "qwen"}), encoding="utf-8")
            with patch("ollamapilot.config.SETTINGS_FILE", path), patch("ollamapilot.secrets.current", return_value=store):
                loaded = Settings.load()
                saved_text = path.read_text(encoding="utf-8")
                again = Settings.load()
        saved = json.loads(saved_text)
        self.assertEqual(loaded.cloud_api_key, "chave-da-conta-123")
        self.assertEqual(loaded.model, "qwen")
        self.assertEqual(saved["cloud_api_key"], "")
        self.assertNotIn("chave-da-conta-123", saved_text)
        self.assertEqual(again.cloud_api_key, "chave-da-conta-123")


class PrivacyTests(unittest.TestCase):
    def test_patterns_and_secret_files_stay_out_of_the_transcript_on_disk(self) -> None:
        text = redact("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP senha=hunter2 $2b$12$abcdefghijklmnopqrstuv")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abcdefghijklmnopqrstuv", text)
        payload = {
            "display": [{"role": "assistant", "content": "token=abc123456789"}],
            "transcript": [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {"caminho": r"C:\app\.env", "conteudo": "DB_PASSWORD=segredo-longo"},
                        ensure_ascii=False,
                    ),
                }
            ],
        }
        stored = for_disk(payload, ["abc123456789"])
        self.assertIn("segredo-longo", payload["transcript"][0]["content"])
        blob = json.dumps(stored)
        self.assertNotIn("segredo-longo", blob)
        self.assertNotIn("abc123456789", blob)
        self.assertIn("omitido: .env", blob)


class PluginTests(unittest.TestCase):
    def test_new_tools_register_without_the_agent_listing_them(self) -> None:
        names = build_registry(Settings(), None, lambda: False).names()
        for name in ("aplicar_patch", "propor_plano", "desfazer_plano", "indexar_projeto", "buscar_no_projeto", "mapa_do_projeto"):
            self.assertIn(name, names)
        self.assertNotIn("banco_resumo", names)
        self.assertNotIn("pesquisar_web", names)


class PatchAndPlanTests(unittest.TestCase):
    def test_patch_replaces_one_block_and_refuses_a_repeated_one(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "main.py"
            target.write_text("def alfa():\n    return 1\n\ndef beta():\n    return 2\n", encoding="utf-8")
            tool = ApplyPatch(Settings())
            tool.run(
                {
                    "caminho": str(target),
                    "blocos": [{"buscar": "return 1", "substituir": "return 3"}],
                }
            )
            self.assertIn("return 3", target.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                tool.run({"caminho": str(target), "blocos": [{"buscar": "return", "substituir": "devolve"}]})

    def test_full_rewrite_of_a_long_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "pagina.html"
            target.write_text("\n".join(f"<p>{index}</p>" for index in range(50)), encoding="utf-8")
            with self.assertRaises(ValueError):
                WriteFile(Settings()).run({"caminho": str(target), "conteudo": "<p>novo</p>\n"})

    def test_two_paths_wait_for_a_plan(self) -> None:
        agent = Agent(Settings(model="m"), None)
        first = Path("E:/site/a.html")
        second = Path("E:/site/b.html")
        agent.transcript.append({"role": "user", "content": f"Grave {first} e {second}"})
        tool = WriteFile(Settings())
        blocked = _mutation_blocked(agent, tool, {"caminho": str(first)})
        self.assertIn("propor_plano", blocked)
        plan = ProposePlan()
        plan.ctx = type("Ctx", (), {"agent": agent})()
        result = plan.run(
            {
                "objetivo": "duas páginas",
                "passos": [
                    {"caminho": str(first), "acao": "criar"},
                    {"caminho": str(second), "acao": "editar"},
                ],
            }
        )
        self.assertEqual(agent.plan_id, result["plano"])
        self.assertIsNone(_mutation_blocked(agent, tool, {"caminho": str(first)}))
        self.assertIn("plano", _mutation_blocked(agent, tool, {"caminho": r"E:\outro\c.html"}))


class ProjectIndexTests(unittest.TestCase):
    def test_index_skips_secrets_and_finds_a_function(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "app.py").write_text("def calcular_total():\n    return 42\n", encoding="utf-8")
            (root / ".env").write_text("DB_PASSWORD=nao-indexar\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "lib.js").write_text("function oculto(){return 1}\n", encoding="utf-8")

            def embed(texts):
                return [[float(len(text)), 1.0, 0.0] for text in texts]

            with patch("ollamapilot.project_index.STORAGE", root / "idx"):
                report = index_project(root, embed)
                rows = search_project(root, "calcular_total", [float(len("def calcular_total():")), 1.0, 0.0])
        self.assertGreaterEqual(report["arquivos"], 1)
        self.assertTrue(any("calcular_total" in item["simbolo"] for item in rows))
        blob = json.dumps(rows)
        self.assertNotIn("nao-indexar", blob)
        self.assertNotIn("oculto", blob)


class UndoSecretTests(unittest.TestCase):
    def test_secret_backup_is_not_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / ".env"
            target.write_text("DB_PASSWORD=segredo-do-backup\n", encoding="utf-8")
            with patch("ollamapilot.undo.paths.BACKUPS", Path(folder) / "copias"):
                entry = undo.remember(target, "gravar_arquivo")
                if entry is None:
                    copies = list((Path(folder) / "copias").glob("*.bin"))
                    self.assertEqual(copies, [])
                    return
                copy = Path(folder) / "copias" / entry["copia"]
                self.assertNotIn(b"segredo-do-backup", copy.read_bytes())
                self.assertTrue(entry.get("cifrado"))


class OutgoingSecretTests(unittest.TestCase):
    def test_the_model_payload_hides_the_vault_value(self) -> None:
        store = current()
        store.set("cloud_api_key", "chave-super-secreta")
        try:
            message = {"role": "tool", "content": "chave-super-secreta\nDB_PASSWORD=outra-senha"}
            clean = _outgoing(message)
        finally:
            store.delete("cloud_api_key")
        self.assertNotIn("chave-super-secreta", clean["content"])
        self.assertIn("DB_PASSWORD=outra-senha", clean["content"])
        self.assertIn("chave-super-secreta", message["content"])


class UndoRequiredTests(unittest.TestCase):
    def test_existing_file_is_not_rewritten_without_a_backup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app.py"
            target.write_text("original\n", encoding="utf-8")
            tool = WriteFile(Settings())
            with patch("ollamapilot.tools.filesystem.undo.remember", return_value=None):
                with self.assertRaises(ValueError):
                    tool.run({"caminho": str(target), "conteudo": "novo\n"})
            self.assertEqual(target.read_text(encoding="utf-8"), "original\n")

    def test_new_file_is_still_created(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "novo.txt"
            tool = WriteFile(Settings())
            with patch("ollamapilot.tools.filesystem.undo.remember", return_value={"existia": False}):
                result = tool.run({"caminho": str(target), "conteudo": "oi\n"})
            self.assertTrue(result["criado"])
            self.assertEqual(target.read_text(encoding="utf-8"), "oi\n")


class SearchScopeTests(unittest.TestCase):
    def test_search_does_not_rank_a_chunk_outside_the_lexical_hits(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "alfa.py").write_text("def alfa_unica():\n    return 1\n", encoding="utf-8")
            (root / "beta.py").write_text("def outra_coisa():\n    return 2\n", encoding="utf-8")

            def embed(texts):
                vectors = []
                for text in texts:
                    vectors.append([0.0, 1.0] if "alfa_unica" in text else [1.0, 0.0])
                return vectors

            with patch("ollamapilot.project_index.STORAGE", root / "idx"):
                index_project(root, embed)
                rows = search_project(root, "alfa_unica", [1.0, 0.0])
        blob = json.dumps(rows)
        self.assertIn("alfa_unica", blob)
        self.assertNotIn("outra_coisa", blob)


class BatchApprovalTests(unittest.TestCase):
    def test_two_writes_in_one_reply_ask_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = str(Path(folder) / "a.txt")
            second = str(Path(folder) / "b.txt")

            class Client:
                def __init__(self) -> None:
                    self.calls = 0

                def chat(self, messages, tools, on_event, cancel):
                    self.calls += 1
                    if self.calls == 1:
                        return {
                            "content": "",
                            "tool_calls": [
                                {"function": {"name": "gravar_arquivo", "arguments": {"caminho": first, "conteudo": "a"}}},
                                {"function": {"name": "gravar_arquivo", "arguments": {"caminho": second, "conteudo": "b"}}},
                            ],
                        }
                    return {"content": "Pronto.", "tool_calls": []}

            agent = Agent(Settings(model="teste"), Client())
            agent.workspace = Path(folder)
            titles = []

            def approve(preview):
                titles.append(preview["title"])
                return "once"

            answer = agent.turn(f"Grave {first} e {second}", [], lambda *_: None, approve, lambda: False)
            self.assertEqual(answer, "Pronto.")
            self.assertEqual(titles, ["Várias alterações"])
            self.assertEqual(Path(first).read_text(encoding="utf-8"), "a")
            self.assertEqual(Path(second).read_text(encoding="utf-8"), "b")
            self.assertNotIn("propor_plano", json.dumps(agent.transcript, ensure_ascii=False))
