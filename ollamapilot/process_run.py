from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Sequence


CREATE_NO_WINDOW = 0x08000000
_PT_LETTERS = set("áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ")
# Símbolos que aparecem quando a página de código errada é usada para ler texto em português.
_ODD = set("‡Æ¡¢£¤¥¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿þÞðÐƒ€‚„…†ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ") | {chr(code) for code in range(0x2500, 0x2580)}


def kill_tree(proc: subprocess.Popen) -> None:
    """Encerra o processo e os filhos. O cmd.exe sozinho deixaria o Java do Ghidra rodando."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.poll() is None:
        proc.kill()


def _oem_codec() -> str:
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


def _score(text: str) -> int:
    return sum(1 for char in text if char in _PT_LETTERS) - 3 * sum(1 for char in text if char in _ODD)


def decode_output(data: bytes) -> str:
    """UTF-8 quando é UTF-8. Senão, a página do console (cp850) ou a do Windows (cp1252), a que der texto legível."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    loose = data.decode("utf-8", errors="replace")
    if loose.count("\ufffd") <= 2:
        return loose
    candidates = []
    for codec in dict.fromkeys((_oem_codec(), "cp850", "cp1252")):
        try:
            text = data.decode(codec, errors="replace")
        except LookupError:
            continue
        candidates.append((_score(text), text))
    return max(candidates, key=lambda item: item[0])[1]


def child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    if extra:
        env.update(extra)
    return env


class OutputCapture:
    """Guarda o começo e o fim da saída. Erros de compilação e de teste costumam estar no fim."""

    def __init__(self, limit: int) -> None:
        self.half = max(1024, limit // 2)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.half:
                del self.tail[: len(self.tail) - self.half]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def text(self) -> str:
        if not self.truncated:
            return decode_output(bytes(self.head + self.tail))
        omitted = self.total - len(self.head) - len(self.tail)
        return (
            decode_output(bytes(self.head))
            + f"\n…[{omitted} bytes do meio omitidos]…\n"
            + decode_output(bytes(self.tail))
        )


def run_process(
    command: Sequence[str],
    cwd: str | None,
    timeout: int,
    cancel: Callable[[], bool] | None = None,
    output_limit: int = 100_000,
    shell: bool = False,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Executa um processo e devolve a saída, com teto de tempo e de tamanho."""
    started = time.perf_counter()
    proc = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
        env=child_environment(env),
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    out = OutputCapture(output_limit)
    err = OutputCapture(output_limit)

    def reader(pipe, capture: OutputCapture) -> None:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                break
            capture.add(chunk)

    def writer() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write((input_text or "").encode("utf-8"))
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    assert proc.stdout is not None and proc.stderr is not None
    threads = [
        threading.Thread(target=reader, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, err), daemon=True),
    ]
    if input_text is not None:
        threads.append(threading.Thread(target=writer, daemon=True))
    for thread in threads:
        thread.start()

    timed_out = False
    cancelled = False
    deadline = time.time() + max(1, timeout)
    while proc.poll() is None:
        if cancel is not None and cancel():
            cancelled = True
            kill_tree(proc)
            break
        if time.time() >= deadline:
            timed_out = True
            kill_tree(proc)
            break
        time.sleep(0.1)
    for thread in threads:
        thread.join(timeout=2)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    code = proc.returncode if proc.returncode is not None else -1
    return {
        "exit_code": code,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "truncated": out.truncated or err.truncated,
        "stdout": out.text(),
        "stderr": err.text(),
        "duracao_ms": int((time.perf_counter() - started) * 1000),
    }
