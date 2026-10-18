from __future__ import annotations

import json
import os

os.environ.setdefault("OLLAMAPILOT_VAULT", "memory")

import shutil
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ollamapilot import history, paths
from ollamapilot.config import Settings


def payload(question: str, grants=("arquivos",)) -> dict:
    return {
        "versao": 2,
        "transcript": [{"role": "user", "content": question}, {"role": "assistant", "content": "ok"}],
        "display": [{"role": "user", "content": question}, {"role": "assistant", "content": "ok"}],
        "autorizacoes": list(grants),
        "recusas": [],
    }


def backdate(conversation_id: str, days: int) -> None:
    path = history.folder() / f"{conversation_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["atualizada"] = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    path.write_text(json.dumps(data), encoding="utf-8")


class HistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(history.folder(), ignore_errors=True)

    def test_save_list_load_and_title(self) -> None:
        first = history.new_id()
        self.assertTrue(history.save(first, payload("Como listar a pasta E:\\dados?\n\nAnexos: a.txt (texto)")))
        self.assertFalse(history.save(history.new_id(), {"display": []}), "conversa vazia não entra")
        rows = history.list_conversations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["titulo"], "Como listar a pasta E:\\dados?")
        self.assertEqual(rows[0]["mensagens"], 1)
        created = history.load(first)["criada"]
        history.save(first, payload("Como listar a pasta E:\\dados?"))
        self.assertEqual(history.load(first)["criada"], created, "a data de criação se mantém")

    def test_ids_cannot_escape_the_folder(self) -> None:
        for bad in ("..\\settings", "../x", "C:\\Windows\\win", "20261016-112900-zzzzzz"):
            with self.assertRaises(ValueError):
                history.load(bad)
        self.assertEqual(history.delete(["..\\settings"]), 0)

    def test_delete_by_selection_and_period(self) -> None:
        ids = []
        for days in (1, 10, 40, 400):
            conversation_id = history.new_id()
            while conversation_id in ids:
                conversation_id = history.new_id()
            history.save(conversation_id, payload(f"pedido de {days} dias"))
            backdate(conversation_id, days)
            ids.append(conversation_id)
        rows = history.list_conversations()
        self.assertEqual([row["id"] for row in rows], ids, "da mais recente para a mais antiga")
        older_30 = history.in_period(rows, None, datetime.now() - timedelta(days=30))
        self.assertEqual(set(older_30), {ids[2], ids[3]})
        between = history.in_period(rows, datetime.now() - timedelta(days=15), datetime.now() - timedelta(days=5))
        self.assertEqual(between, [ids[1]])
        self.assertEqual(history.delete(older_30), 2)
        self.assertEqual(history.delete([ids[0]]), 1)
        self.assertEqual([row["id"] for row in history.list_conversations()], [ids[1]])

    def test_pinned_come_first_by_title_and_rename_survives_save(self) -> None:
        zeta = history.new_id()
        alpha = history.new_id()
        middle = history.new_id()
        history.save(zeta, payload("zeta"))
        history.save(alpha, payload("alfa"))
        history.save(middle, payload("meio"))
        history.set_pinned(alpha, True)
        history.set_pinned(zeta, True)
        titles = [row["titulo"] for row in history.list_conversations()]
        self.assertEqual(titles[:2], ["alfa", "zeta"])
        self.assertEqual(titles[2], "meio")
        self.assertEqual(history.rename(alpha, "Beta manual"), "Beta manual")
        history.save(alpha, payload("o pedido original mudou"))
        stored = history.load(alpha)
        self.assertEqual(stored["titulo"], "Beta manual")
        self.assertTrue(stored["fixada"])
        self.assertTrue(stored["titulo_manual"])
        with self.assertRaises(ValueError):
            history.rename(alpha, "   ")

    def test_unreadable_file_is_listed_for_deletion(self) -> None:
        history.folder().mkdir(parents=True, exist_ok=True)
        broken = history.new_id()
        (history.folder() / f"{broken}.json").write_text("{quebrado", encoding="utf-8")
        (history.folder() / "outro-arquivo.json").write_text("{}", encoding="utf-8")
        rows = history.list_conversations()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["legivel"])
        self.assertEqual(history.delete([broken]), 1)


class HistoryWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ["OLLAMAPILOT_SELFTEST"] = "1"
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        shutil.rmtree(history.folder(), ignore_errors=True)
        paths.SESSION_FILE.unlink(missing_ok=True)

    def tearDown(self) -> None:
        paths.SESSION_FILE.unlink(missing_ok=True)

    def _window(self):
        from ollamapilot.ui.main_window import MainWindow

        window = MainWindow(Settings(model="teste"))
        window.client.capabilities = lambda *args, **kwargs: {"conhecido": False}
        return window

    def test_legacy_session_becomes_first_conversation(self) -> None:
        paths.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.SESSION_FILE.write_text(json.dumps(payload("conversa antiga")), encoding="utf-8")
        window = self._window()
        rows = history.list_conversations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], window.conversation_id)
        self.assertEqual(json.loads(paths.SESSION_FILE.read_text(encoding="utf-8"))["conversa_id"], window.conversation_id)
        window.close()

    def test_new_chat_keeps_previous_and_reopen_drops_grants(self) -> None:
        window = self._window()
        window.agent.transcript = payload("primeira")["transcript"]
        window.display = payload("primeira")["display"]
        window.agent.chat_grants = {"executar_programa"}
        window._save_session()
        first = window.conversation_id
        window.new_chat()
        self.assertNotEqual(window.conversation_id, first)
        self.assertEqual(window.display, [])
        self.assertEqual(len(history.list_conversations()), 1, "conversa nova e vazia não aparece na lista")
        self.assertTrue(window.open_conversation(first))
        self.assertEqual(window.conversation_id, first)
        self.assertEqual(window.display[0]["content"], "primeira")
        self.assertEqual(window.agent.chat_grants, set())
        self.assertEqual(json.loads(paths.SESSION_FILE.read_text(encoding="utf-8"))["conversa_id"], first)
        window.close()

    def test_deleting_open_conversation_starts_a_new_one(self) -> None:
        from ollamapilot.ui.dialogs import HistoryDialog
        from PySide6.QtWidgets import QMessageBox

        window = self._window()
        window.display = payload("apagar")["display"]
        window.agent.transcript = payload("apagar")["transcript"]
        window._save_session()
        opened = window.conversation_id
        dialog = HistoryDialog(window)
        self.assertEqual(dialog.table.topLevelItemCount(), 1)
        self.assertIn("(aberta)", dialog.table.topLevelItem(0).text(0))
        dialog.table.topLevelItem(0).setSelected(True)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            dialog._delete_selected()
        self.assertEqual(len(history.list_conversations()), 1, "sem confirmação nada é apagado")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog._delete_selected()
        self.assertEqual(history.list_conversations(), [])
        self.assertNotEqual(window.conversation_id, opened)
        self.assertEqual(window.display, [])
        self.assertEqual(dialog.table.topLevelItemCount(), 0)
        dialog.close()
        window.close()

    def test_rename_and_pin_from_the_dialog(self) -> None:
        from ollamapilot.ui.dialogs import HistoryDialog

        window = self._window()
        window.display = payload("titulo antigo")["display"]
        window.agent.transcript = payload("titulo antigo")["transcript"]
        window._save_session()
        dialog = HistoryDialog(window)
        item = dialog.table.topLevelItem(0)
        item.setSelected(True)
        self.assertEqual(dialog.rename_button.text(), "Mudar o título")
        self.assertEqual(dialog.pin_button.text(), "Fixar")
        with patch.object(HistoryDialog, "_rename", wraps=dialog._rename):
            dialog.history.rename(window.conversation_id, "Estoque")
        dialog._reload()
        self.assertIn("Estoque", dialog.table.topLevelItem(0).text(0))
        dialog.table.topLevelItem(0).setSelected(True)
        dialog._toggle_pin()
        self.assertTrue(dialog.table.topLevelItem(0).text(0).startswith("Fixada · "))
        dialog.table.topLevelItem(0).setSelected(True)
        self.assertEqual(dialog.pin_button.text(), "Desafixar")
        from ollamapilot.ui.bubbles import MessageBubble

        bubble = MessageBubble("assistant", "")
        bubble.set_activity("Executando ghidra_analisar…")
        self.assertFalse(bubble.activity.isHidden())
        self.assertTrue(bubble.activity_mark._timer.isActive())
        self.assertIn("ghidra_analisar", bubble.activity_label.text())
        bubble.stop_activity()
        self.assertTrue(bubble.activity.isHidden())
        dialog.close()
        window.close()


if __name__ == "__main__":
    unittest.main()
