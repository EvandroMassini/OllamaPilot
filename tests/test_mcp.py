from __future__ import annotations

import json
import os

os.environ.setdefault("OLLAMAPILOT_VAULT", "memory")

import threading
import time
import unittest
from unittest.mock import patch

from ollamapilot import mcp
from ollamapilot.agent import Agent
from ollamapilot.config import Settings
from ollamapilot.tools.registry import build_registry
from tests.test_robustez import RecordingClient, answer, assert_valid, call, run, tools_step

TOOLS = [
    {
        "name": "eco",
        "description": "Devolve o texto recebido.",
        "inputSchema": {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]},
        "annotations": {"readOnlyHint": True},
    },
    {"name": "apagar", "description": "Apaga algo.", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": True}},
    {"name": "lento", "description": "Nunca responde.", "inputSchema": {"type": "object"}},
    {"name": "falha", "description": "Sempre falha.", "inputSchema": {"type": "object"}},
]


class FakeServer:
    """Servidor MCP dentro do processo, ligado por pipes; nenhum programa é iniciado."""

    def __init__(self, spec: dict, crash: bool = False) -> None:
        c2s_r, c2s_w = os.pipe()
        s2c_r, s2c_w = os.pipe()
        self.stdin = os.fdopen(c2s_w, "wb", buffering=0)
        self.stdout = os.fdopen(s2c_r, "rb")
        self.stderr = None
        self.pid = 0
        self._in = os.fdopen(c2s_r, "rb")
        self._out = os.fdopen(s2c_w, "wb", buffering=0)
        self._write = threading.Lock()
        self.code = 0 if crash else None
        self.received: list[dict] = []
        self.spec = spec
        if crash:
            self.kill()
            self.code = 1
        else:
            threading.Thread(target=self._serve, daemon=True).start()

    def _send(self, message: dict) -> None:
        with self._write:
            self._out.write((json.dumps(message) + "\n").encode("utf-8"))

    def _serve(self) -> None:
        try:
            for line in self._in:
                message = json.loads(line)
                self.received.append(message)
                self._handle(message)
        except (OSError, ValueError):
            pass
        self.code = 0
        for stream in (self._in, self._out):
            try:
                stream.close()
            except OSError:
                pass

    def _handle(self, message: dict) -> None:
        method = message.get("method")
        ident = message.get("id")
        if method == "initialize":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "result": {"protocolVersion": mcp.PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "falso"}},
                }
            )
        elif method == "notifications/initialized":
            # Um pedido do servidor ao cliente, que o OllamaPilot precisa recusar.
            self._send({"jsonrpc": "2.0", "id": 900, "method": "sampling/createMessage", "params": {}})
        elif method == "tools/list":
            self._send({"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = message["params"]["name"]
            arguments = message["params"].get("arguments") or {}
            if name == "eco":
                result = {"content": [{"type": "text", "text": f"eco: {arguments.get('texto')}"}]}
            elif name == "falha":
                result = {"content": [{"type": "text", "text": "disco cheio"}], "isError": True}
            elif name == "lento":
                return
            else:
                result = {"content": [{"type": "text", "text": "ok"}]}
            self._send({"jsonrpc": "2.0", "id": ident, "result": result})

    def poll(self):
        return self.code

    def kill(self) -> None:
        self.code = -9
        for stream in (self._in, self._out):
            try:
                stream.close()
            except OSError:
                pass


class Spawner:
    def __init__(self, crash: bool = False) -> None:
        self.started: list[FakeServer] = []
        self.crash = crash

    def __call__(self, spec: dict) -> FakeServer:
        server = FakeServer(spec, crash=self.crash)
        self.started.append(server)
        return server


def write_config(*servers: dict) -> None:
    mcp.config_path().parent.mkdir(parents=True, exist_ok=True)
    mcp.config_path().write_text(json.dumps({"versao": 1, "servidores": list(servers)}), encoding="utf-8")


def server_spec(**extra) -> dict:
    spec = {"nome": "fake", "ativo": True, "comando": "fake-server.exe", "timeout_segundos": 5}
    spec.update(extra)
    return spec


class McpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spawner = Spawner()
        self.manager = mcp.McpManager(spawn=self.spawner)
        self.settings = Settings(model="teste", mcp_enabled=True)

    def tearDown(self) -> None:
        self.manager.shutdown()
        mcp.config_path().unlink(missing_ok=True)

    def tools(self, settings=None, cancel=lambda: False):
        return {tool.name: tool for tool in self.manager.tools(settings or self.settings, cancel)}

    def test_disabled_mcp_starts_nothing(self) -> None:
        write_config(server_spec())
        self.assertEqual(self.tools(Settings(model="teste", mcp_enabled=False)), {})
        self.assertEqual(self.spawner.started, [])

    def test_only_active_servers_start_and_policies_apply(self) -> None:
        write_config(
            server_spec(padrao="perguntar", ferramentas={"eco": "liberada", "apagar": "bloqueada"}),
            server_spec(nome="parado", ativo=False),
        )
        tools = self.tools()
        self.assertEqual(len(self.spawner.started), 1)
        self.assertIn("mcp_fake_eco", tools)
        self.assertNotIn("mcp_fake_apagar", tools)
        self.assertFalse(tools["mcp_fake_eco"].sensitive)
        self.assertTrue(tools["mcp_fake_lento"].sensitive)
        self.assertEqual(tools["mcp_fake_eco"].grant_key, "mcp_fake_eco")
        received = self.spawner.started[0].received
        self.assertEqual(received[0]["method"], "initialize")
        self.assertEqual(received[1]["method"], "notifications/initialized")
        deadline = time.time() + 2
        while time.time() < deadline and not any(item.get("id") == 900 for item in received):
            time.sleep(0.02)
        refusal = next(item for item in received if item.get("id") == 900)
        self.assertEqual(refusal["error"]["code"], -32601)
        self.tools()
        self.assertEqual(len(self.spawner.started), 1, "o servidor vivo é reaproveitado entre rodadas")

    def test_call_results_errors_and_preview(self) -> None:
        write_config(server_spec())
        tools = self.tools()
        result = tools["mcp_fake_eco"].run({"texto": "oi"})
        self.assertEqual(result["texto"], "eco: oi")
        self.assertTrue(result["sucesso"])
        failed = tools["mcp_fake_falha"].run({})
        self.assertFalse(failed["sucesso"])
        self.assertIn("disco cheio", failed["erro"])
        preview = tools["mcp_fake_apagar"].preview({"alvo": "x"})
        self.assertIn("pode apagar", preview["detail"])
        self.assertIn("não foi verificado", preview["detail"])
        self.assertIn('"alvo": "x"', preview["detail"])

    def test_cancel_and_timeout(self) -> None:
        write_config(server_spec())
        flag = threading.Event()
        tools = self.tools(cancel=flag.is_set)
        threading.Timer(0.3, flag.set).start()
        started = time.time()
        result = tools["mcp_fake_lento"].run({})
        self.assertTrue(result["cancelado"])
        self.assertLess(time.time() - started, 3)
        received = self.spawner.started[0].received
        deadline = time.time() + 2
        while time.time() < deadline and not any(item.get("method") == "notifications/cancelled" for item in received):
            time.sleep(0.02)
        self.assertTrue(any(item.get("method") == "notifications/cancelled" for item in received))
        flag.clear()
        server = tools["mcp_fake_lento"].server
        server.spec["timeout_segundos"] = 1
        with self.assertRaises(mcp.McpError):
            tools["mcp_fake_lento"].run({})

    def test_remote_model_hides_tools_unless_allowed(self) -> None:
        write_config(server_spec())
        remote = Settings(model="nuvem", model_remote=True, mcp_enabled=True)
        self.assertEqual(self.tools(remote), {})
        write_config(server_spec(permitir_nuvem=True))
        self.assertIn("mcp_fake_eco", self.tools(remote))

    def test_invalid_entries_are_reported_and_file_untouched(self) -> None:
        write_config(
            {"nome": "com espaço", "comando": "x"},
            {"nome": "rede", "url": "http://exemplo", "comando": "x"},
            {"nome": "sem"},
            server_spec(padrao="talvez"),
        )
        before = mcp.config_path().read_bytes()
        specs, errors = mcp.load_config()
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 4)
        self.assertTrue(any("stdio" in item for item in errors))
        self.assertEqual(mcp.config_path().read_bytes(), before)
        mcp.config_path().write_text("{quebrado", encoding="utf-8")
        self.assertEqual(self.tools(), {})
        self.assertIn("ilegível", self.manager.status()["erros"][0])

    def test_environment_is_filtered(self) -> None:
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "sk-segredo", "PATH": "C:\\bin"}):
            env = mcp.server_environment({"TOKEN_DO_SERVIDOR": "abc"})
        self.assertNotIn("OLLAMA_API_KEY", env)
        self.assertEqual(env["PATH"], "C:\\bin")
        self.assertEqual(env["TOKEN_DO_SERVIDOR"], "abc")

    def test_crashed_server_is_not_retried_until_asked(self) -> None:
        self.manager = mcp.McpManager(spawn=Spawner(crash=True))
        write_config(server_spec())
        self.assertEqual(self.tools(), {})
        self.assertEqual(self.tools(), {})
        self.assertEqual(len(self.manager._spawn.started), 1)
        self.assertEqual(self.manager.status()["servidores"][0]["estado"], "falhou")
        self.manager.reconcile(self.settings, retry=True)
        self.assertEqual(len(self.manager._spawn.started), 2)

    def test_disabling_stops_servers(self) -> None:
        write_config(server_spec())
        self.tools()
        server = self.spawner.started[0]
        self.manager.tools(Settings(model="teste", mcp_enabled=False), lambda: False)
        deadline = time.time() + 3
        while time.time() < deadline and server.poll() is None:
            time.sleep(0.02)
        self.assertIsNotNone(server.poll())

    def test_agent_asks_approval_per_tool_and_keeps_transcript_valid(self) -> None:
        write_config(server_spec())
        client = RecordingClient(
            [
                tools_step(call("mcp_fake_apagar", alvo="a")),
                tools_step(call("mcp_fake_apagar", alvo="b")),
                answer("feito"),
            ]
        )
        agent = Agent(self.settings, client)
        cards: list[dict] = []

        def approve(preview):
            cards.append(preview)
            return "chat"

        with patch.object(mcp, "MANAGER", self.manager):
            result, _events = run(agent, "apague", approve=approve)
            self.assertIn("mcp_fake_eco", build_registry(self.settings, None, lambda: False).names())
        self.assertEqual(result, "feito")
        self.assertEqual(len(cards), 1, "autorizar neste chat cobre a mesma ferramenta MCP")
        self.assertIn("servidor MCP fake", cards[0]["escopo"])
        self.assertEqual(agent.chat_grants, {"mcp_fake_apagar"})
        assert_valid(self, agent.transcript)
        names = [item["function"]["name"] for item in client.requests[0]["tools"]]
        self.assertIn("mcp_fake_apagar", names)
        self.assertIn("dado externo", client.requests[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
