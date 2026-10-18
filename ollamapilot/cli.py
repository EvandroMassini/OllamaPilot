"""Um turno sem a janela: py -3 -m ollamapilot --mensagem \"...\"."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ollamapilot.agent import Agent
from ollamapilot.attachments import read_attachment
from ollamapilot.config import Settings
from ollamapilot.history import load as load_conversation
from ollamapilot.ollama_client import OllamaClient
from ollamapilot.paths import ensure_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ollamapilot")
    parser.add_argument("--mensagem", required=True)
    parser.add_argument("--pasta", default="")
    parser.add_argument("--modelo", default="")
    parser.add_argument("--aprovar", default="")
    parser.add_argument("--anexo", action="append", default=[])
    parser.add_argument("--conversa", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.aprovar and args.aprovar != "workspace":
        print("O valor de --aprovar aceito neste modo é workspace.", file=sys.stderr)
        return 2
    ensure_storage()
    settings = Settings.load()
    if args.modelo.strip():
        settings.model = args.modelo.strip()
    if not settings.model.strip():
        print("Escolha um modelo com --modelo.", file=sys.stderr)
        return 2
    client = OllamaClient(settings)
    agent = Agent(settings, client)
    if args.conversa:
        try:
            _load_conversation(agent, Path(args.conversa))
        except (OSError, ValueError) as error:
            print(f"Não foi possível abrir a conversa: {error}", file=sys.stderr)
            return 2
    if args.pasta.strip():
        agent.workspace = Path(args.pasta)
    if args.aprovar == "workspace":
        if agent.workspace is None:
            print("Informe --pasta para aprovar a pasta de trabalho.", file=sys.stderr)
            return 2
        agent.chat_grants.add("arquivos")
    attachments = []
    for raw in args.anexo:
        loaded = read_attachment(Path(raw))
        if not loaded.get("ok"):
            print(str(loaded.get("erro") or raw), file=sys.stderr)
            return 2
        attachments.append(loaded["anexo"])

    def approve(preview: dict):
        print(_card_text(preview), file=sys.stderr)
        return False

    try:
        answer = agent.turn(args.mensagem, attachments, lambda *_: None, approve, lambda: False)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    sys.stdout.write(answer)
    if answer and not answer.endswith("\n"):
        sys.stdout.write("\n")
    if args.json:
        trace = agent.last_trace or {}
        payload = {
            "resposta": answer,
            "passos": trace.get("passos") or [],
            "desfecho": trace.get("desfecho") or "",
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0


def _card_text(preview: dict) -> str:
    title = str(preview.get("title") or "Ação")
    summary = str(preview.get("summary") or "")
    detail = str(preview.get("detail") or "")
    if len(detail) > 1200:
        detail = detail[:1200] + "…"
    return f"Recusado sem cartão: {title} — {summary}\n{detail}".rstrip()


def _load_conversation(agent: Agent, path: Path) -> None:
    if path.suffix.lower() == ".json" and path.is_file() and path.parent.name != "conversas":
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = load_conversation(path.stem)
    if not isinstance(payload, dict):
        raise ValueError("A conversa não tem o formato esperado.")
    transcript = payload.get("transcript")
    if isinstance(transcript, list):
        from ollamapilot.agent import repair_transcript

        agent.transcript = repair_transcript(
            [item for item in transcript if isinstance(item, dict) and isinstance(item.get("role"), str)]
        )
    pasta = payload.get("pasta")
    if isinstance(pasta, str) and pasta.strip():
        agent.workspace = Path(pasta)
    tarefas = payload.get("tarefas")
    if isinstance(tarefas, list):
        agent.tasks = [item for item in tarefas if isinstance(item, dict) and item.get("texto")]


if __name__ == "__main__":
    raise SystemExit(main())
