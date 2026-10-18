"""Programas que ficam rodando: o agente manda texto ao stdin e lê a saída aos poucos."""

from __future__ import annotations

import atexit
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable

from ollamapilot.process_run import CREATE_NO_WINDOW, child_environment, decode_output, kill_tree

MAX_SESSIONS = 8
BUFFER_BYTES = 1_000_000
READ_LIMIT = 24_000


class SessionError(ValueError):
    pass


class Session:
    def __init__(self, ident: str, command: list[str], cwd: str | None, env: dict[str, str] | None) -> None:
        self.ident = ident
        self.command = command
        self.cwd = cwd
        self.started = time.time()
        self.proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_environment(env),
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._data = bytearray()
        self._dropped = 0
        self._cursor = 0
        self._last_output = time.monotonic()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        while True:
            chunk = self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(4096)
            if not chunk:
                break
            with self._changed:
                self._data += chunk
                if len(self._data) > BUFFER_BYTES:
                    extra = len(self._data) - BUFFER_BYTES
                    del self._data[:extra]
                    self._dropped += extra
                self._last_output = time.monotonic()
                self._changed.notify_all()
        with self._changed:
            self._changed.notify_all()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, text: str) -> None:
        if not self.alive():
            raise SessionError(f"A sessão {self.ident} já terminou (código {self.proc.returncode}).")
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(text.encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as error:
            raise SessionError(f"O programa não aceitou a entrada: {error}") from error

    def close_input(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass

    def read(
        self,
        wait_for: str = "",
        timeout: float = 5.0,
        idle: float = 0.8,
        cancel: Callable[[], bool] | None = None,
    ) -> dict:
        """Saída nova desde a última leitura. Espera o texto pedido, a saída parar por `idle` s, o fim ou o tempo."""
        pattern = None
        if wait_for:
            try:
                pattern = re.compile(wait_for)
            except re.error:
                pattern = re.compile(re.escape(wait_for))
        deadline = time.monotonic() + max(0.0, timeout)
        reason = "tempo"
        with self._changed:
            while True:
                fresh = self._fresh()
                if pattern is not None and pattern.search(decode_output(bytes(fresh))):
                    reason = "texto_encontrado"
                    break
                if not self.alive() and not self._reader.is_alive():
                    reason = "programa_terminou"
                    break
                now = time.monotonic()
                if pattern is None and fresh and now - self._last_output >= idle:
                    reason = "saida_parou"
                    break
                if now >= deadline:
                    break
                if cancel is not None and cancel():
                    reason = "interrompido"
                    break
                self._changed.wait(0.1)
            fresh = bytes(self._fresh())
            self._cursor = self._dropped + len(self._data)
        cut = len(fresh) > READ_LIMIT
        if cut:
            fresh = fresh[-READ_LIMIT:]
        return {
            "sessao": self.ident,
            "saida": decode_output(fresh),
            "truncated": cut,
            "motivo": reason,
            "em_execucao": self.alive(),
            "exit_code": self.proc.returncode,
        }

    def _fresh(self) -> bytearray:
        start = max(0, self._cursor - self._dropped)
        return self._data[start:]

    def stop(self) -> None:
        self.close_input()
        kill_tree(self.proc)

    def describe(self) -> dict:
        return {
            "sessao": self.ident,
            "comando": subprocess.list2cmdline(self.command)[:300],
            "pid": self.proc.pid,
            "em_execucao": self.alive(),
            "exit_code": self.proc.returncode,
            "iniciada_ha_s": int(time.time() - self.started),
        }


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def start(self, command: list[str], cwd: str | None, env: dict[str, str] | None) -> Session:
        with self._lock:
            finished = [key for key, item in self._sessions.items() if not item.alive()]
            for key in finished:
                self._sessions.pop(key, None)
            if len(self._sessions) >= MAX_SESSIONS:
                raise SessionError(f"Já há {MAX_SESSIONS} sessões abertas. Encerre uma com encerrar_sessao.")
            self._counter += 1
            ident = f"s{self._counter}"
            session = Session(ident, command, cwd, env)
            self._sessions[ident] = session
            return session

    def get(self, ident: str) -> Session:
        session = self._sessions.get(str(ident or "").strip())
        if session is None:
            known = ", ".join(self._sessions) or "nenhuma"
            raise SessionError(f"A sessão '{ident}' não existe. Sessões abertas: {known}.")
        return session

    def list(self) -> list[dict]:
        return [item.describe() for item in self._sessions.values()]

    def stop(self, ident: str) -> dict:
        session = self.get(ident)
        session.stop()
        with self._lock:
            self._sessions.pop(session.ident, None)
        return session.describe()

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.stop()
            except Exception:
                pass


MANAGER = SessionManager()
atexit.register(MANAGER.stop_all)
