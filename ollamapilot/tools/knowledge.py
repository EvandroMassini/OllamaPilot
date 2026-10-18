from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ollamapilot.config import Settings
from ollamapilot.ollama_client import OllamaClient
from ollamapilot.paths import KNOWLEDGE
from ollamapilot.tools.base import Tool, int_prop, schema, text_prop, tool

_INDEX = KNOWLEDGE / "indice.jsonl"


@tool
class SearchDocuments(Tool):
    name = "buscar_documentos"
    group = "biblioteca"
    order = 170
    description = (
        "Busca por significado nos documentos que o usuário indexou na Biblioteca. Use quando a pergunta for sobre esses documentos. "
        "Retorno: {trechos: [{arquivo, trecho, similaridade}]}. Cite o arquivo; se nada for relevante, diga isso."
    )
    parameters = schema(
        {
            "consulta": text_prop("O que procurar na biblioteca."),
            "limite": int_prop("Opcional. Quantidade de trechos, de 1 a 8. Padrão 5."),
        },
        ["consulta"],
    )

    def __init__(self, settings: Settings, client: OllamaClient) -> None:
        self.settings = settings
        self.client = client

    def run(self, arguments: dict) -> dict:
        query = str(arguments.get("consulta") or "").strip()
        if not query:
            raise ValueError("Informe a consulta.")
        rows = load_index()
        if not rows:
            return {"trechos": [], "mensagem": "A biblioteca está vazia."}
        limit = int(arguments.get("limite") or 5)
        limit = min(max(limit, 1), 8)
        offline = ""
        vector = None
        try:
            vector = self.client.embed([query])[0]
        except Exception:
            model = self.settings.embed_model or "embeddinggemma"
            offline = f"A busca vetorial está desligada. Falta o modelo {model} no servidor."
        scored = []
        words = [part.lower() for part in query.split() if len(part) > 1]
        for row in rows:
            if vector:
                score = cosine(vector, row.get("vector") or [])
            else:
                text = str(row.get("text") or "").lower()
                score = float(sum(1 for word in words if word in text))
            if score <= 0:
                continue
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        snippets = []
        for score, row in scored[:limit]:
            snippets.append(
                {
                    "arquivo": row.get("source"),
                    "trecho": row.get("text"),
                    "similaridade": round(score, 4),
                }
            )
        result = {"trechos": snippets}
        if offline:
            result["busca_vetorial"] = False
            result["mensagem"] = offline
        return result


def load_index() -> list[dict]:
    if not _INDEX.is_file():
        return []
    rows = []
    for line in _INDEX.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def sources() -> list[str]:
    names = []
    seen = set()
    for row in load_index():
        name = str(row.get("source") or "")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def clear_index() -> None:
    if _INDEX.exists():
        _INDEX.unlink()


def index_files(paths: list[Path], settings: Settings, client: OllamaClient, on_status) -> dict:
    """Indexa os arquivos legíveis. Um arquivo com problema entra em ignorados sem parar os demais."""
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    chunks = []
    skipped: list[str] = []
    for path in paths:
        try:
            text = _read_document(path)
        except (OSError, ValueError) as error:
            skipped.append(str(error))
            continue
        for piece in split_text(text, 1200, 150):
            chunks.append({"source": path.name, "text": piece})
    if not chunks:
        return {"trechos": 0, "ignorados": skipped}
    vectors: list[list[float]] = []
    batch = 16
    for start in range(0, len(chunks), batch):
        group = chunks[start : start + batch]
        on_status(f"Indexando {start + len(group)} de {len(chunks)} trechos…")
        vectors.extend(client.embed([item["text"] for item in group]))
    with _INDEX.open("a", encoding="utf-8") as handle:
        for item, vector in zip(chunks, vectors):
            item["vector"] = vector
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return {"trechos": len(chunks), "ignorados": skipped}


def split_text(text: str, size: int, overlap: int) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    pieces = []
    start = 0
    while start < len(cleaned):
        pieces.append(cleaned[start : start + size])
        if start + size >= len(cleaned):
            break
        start += max(1, size - overlap)
    return pieces


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def _read_document(path: Path) -> str:
    from ollamapilot.attachments import extract_document_text

    return extract_document_text(path)
