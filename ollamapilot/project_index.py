"""Índice local de um projeto: texto, símbolos e vetores no SQLite."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import sqlite3
import struct
from array import array
from pathlib import Path

from ollamapilot.paths import STORAGE
from ollamapilot.tools.paths import is_secret

_SKIP_DIRS = {
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    "storage",
    ".idea",
    ".vs",
}
_EXTENSIONS = {
    ".py",
    ".php",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".html",
    ".htm",
    ".md",
    ".txt",
    ".json",
    ".xml",
    ".sql",
    ".bat",
    ".ps1",
    ".cs",
    ".java",
    ".go",
    ".rs",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".vue",
    ".c",
    ".h",
    ".cpp",
    ".rb",
    ".kt",
}
_CODE_SKIP = {".txt", ".md"}
_MAX_FILES = 4000
_MAX_FILE_BYTES = 500_000
_WINDOW = 60
_OVERLAP = 10


def project_db(root: Path) -> Path:
    digest = hashlib.sha256(str(root).lower().encode("utf-8")).hexdigest()[:16]
    folder = STORAGE / "projetos" / digest
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "indice.sqlite"


def is_code_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in _EXTENSIONS and suffix not in _CODE_SKIP


def index_project(root: Path, embed, on_status=None) -> dict:
    """Reindexa só o que mudou. embed recebe uma lista de textos e devolve os vetores.

    Se o modelo de embeddings falhar, o índice de texto e símbolo continua e a busca vetorial fica desligada.
    """
    embed = _guard_embed(embed)
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Informe a pasta raiz do projeto.")
    rules = _ignore_rules(root)
    conn = _connect(project_db(root))
    try:
        known = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT path, mtime, size FROM files")}
        seen: set[str] = set()
        pending: list[tuple[str, list[dict]]] = []
        files = 0
        for path in _walk(root, rules):
            files += 1
            if files > _MAX_FILES:
                break
            key = str(path)
            stat = path.stat()
            seen.add(key)
            if known.get(key) == (stat.st_mtime, stat.st_size):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            chunks = _chunks(path, root, text)
            pending.append((key, chunks))
            _replace_file(conn, key, stat.st_mtime, stat.st_size, [])
        removed = [key for key in known if key not in seen]
        for key in removed:
            _replace_file(conn, key, 0, 0, [])
            conn.execute("DELETE FROM files WHERE path = ?", (key,))
        indexed = 0
        batch: list[tuple[str, dict]] = []
        for key, chunks in pending:
            for chunk in chunks:
                batch.append((key, chunk))
                if len(batch) >= 16:
                    indexed += _embed_batch(conn, embed, batch)
                    batch = []
                    if on_status:
                        on_status(f"Indexando {indexed} trechos…")
        if batch:
            indexed += _embed_batch(conn, embed, batch)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        result = {"raiz": str(root), "arquivos": len(seen), "trechos_novos": indexed, "trechos": total, "removidos": len(removed)}
        state = getattr(embed, "state", None)
        if isinstance(state, dict) and not state.get("ok", True):
            result["busca_vetorial"] = False
            result["mensagem"] = state.get("mensagem") or "A busca vetorial está desligada."
        else:
            result["busca_vetorial"] = True
        return result
    finally:
        conn.close()


def search_project(root: Path, query: str, vector: list[float] | None, limit: int = 6) -> list[dict]:
    path = project_db(root)
    if not path.is_file():
        return []
    conn = _connect(path)
    try:
        words = [part for part in "".join(char if char.isalnum() else " " for char in query).split() if len(part) > 1][:8]
        lexical: list[int] = []
        # FTS5 primeiro. Se o índice de texto não existir ou a consulta falhar, cai no LIKE.
        # O embedding, quando vem, só reordena estes candidatos; não varre a pasta de novo.
        if words and _has_fts(conn):
            match = " OR ".join('"' + word.replace('"', "") + '"' for word in words)
            try:
                lexical = [row[0] for row in conn.execute(
                    "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 40",
                    (match,),
                )]
            except sqlite3.OperationalError:
                lexical = []
        if lexical:
            marks = ",".join("?" for _ in lexical)
            rows = conn.execute(
                f"SELECT id, path, start, end, symbol, text, vector FROM chunks WHERE id IN ({marks})",
                lexical,
            ).fetchall()
        elif words:
            like = f"%{words[0]}%"
            rows = conn.execute(
                "SELECT id, path, start, end, symbol, text, vector FROM chunks "
                "WHERE symbol LIKE ? OR text LIKE ? LIMIT 40",
                (like, like),
            ).fetchall()
        else:
            rows = []
    finally:
        conn.close()
    if not rows:
        return []
    by_id = {row[0]: row for row in rows}
    lexical_rank = {chunk_id: index for index, chunk_id in enumerate(lexical)}
    vector_rank: dict[int, int] = {}
    if vector:
        scored = []
        for row in rows:
            score = _cosine(vector, _unpack(row[6]))
            if score > 0:
                scored.append((score, row[0]))
        scored.sort(reverse=True)
        vector_rank = {chunk_id: index for index, (_score, chunk_id) in enumerate(scored[:80])}
    candidates = set(lexical_rank) | set(vector_rank)
    if not candidates:
        return []
    fused = []
    for chunk_id in candidates:
        score = 0.0
        if chunk_id in lexical_rank:
            score += 1.0 / (10 + lexical_rank[chunk_id])
        if chunk_id in vector_rank:
            score += 1.0 / (10 + vector_rank[chunk_id])
        fused.append((score, chunk_id))
    fused.sort(reverse=True)
    found = []
    for _score, chunk_id in fused[:limit]:
        row = by_id.get(chunk_id)
        if row is None:
            continue
        found.append(
            {
                "arquivo": row[1],
                "inicio": row[2],
                "fim": row[3],
                "simbolo": row[4],
                "trecho": row[5][:700],
                "nota": round(_score, 4),
            }
        )
    return found


def map_project(root: Path, limit: int = 80) -> dict:
    path = project_db(root)
    if not path.is_file():
        return {"raiz": str(root), "arquivos": [], "mensagem": "O projeto ainda não foi indexado."}
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT path, symbol, start FROM chunks WHERE symbol != '' ORDER BY path, start LIMIT 400"
        ).fetchall()
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    finally:
        conn.close()
    grouped: dict[str, list[str]] = {}
    for file_path, symbol, start in rows:
        grouped.setdefault(file_path, [])
        if len(grouped[file_path]) < 12:
            grouped[file_path].append(f"{symbol}:{start}")
    shown = []
    for file_path, symbols in list(grouped.items())[:limit]:
        shown.append({"arquivo": file_path, "simbolos": symbols})
    return {"raiz": str(root), "arquivos_indexados": files, "arquivos": shown}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks ("
        "id INTEGER PRIMARY KEY, path TEXT, start INTEGER, end INTEGER, symbol TEXT, text TEXT, vector BLOB)"
    )
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, path, symbol, chunk_id UNINDEXED)"
        )
    except sqlite3.OperationalError:
        pass
    return conn


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'").fetchone()
    return row is not None


def _replace_file(conn, key: str, mtime: float, size: int, chunks: list[dict]) -> None:
    identifiers = [row[0] for row in conn.execute("SELECT id FROM chunks WHERE path = ?", (key,))]
    if identifiers and _has_fts(conn):
        marks = ",".join("?" for _ in identifiers)
        conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({marks})", identifiers)
    conn.execute("DELETE FROM chunks WHERE path = ?", (key,))
    if size:
        conn.execute(
            "INSERT INTO files (path, mtime, size) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, size = excluded.size",
            (key, mtime, size),
        )


def _embed_batch(conn, embed, batch: list[tuple[str, dict]]) -> int:
    vectors = embed([item[1]["text"] for item in batch])
    if len(vectors) != len(batch):
        raise ValueError("O modelo de embeddings devolveu uma quantidade diferente de trechos.")
    for (key, chunk), vector in zip(batch, vectors):
        cursor = conn.execute(
            "INSERT INTO chunks (path, start, end, symbol, text, vector) VALUES (?, ?, ?, ?, ?, ?)",
            (key, chunk["start"], chunk["end"], chunk["symbol"], chunk["text"], _pack(vector)),
        )
        if _has_fts(conn):
            conn.execute(
                "INSERT INTO chunks_fts (text, path, symbol, chunk_id) VALUES (?, ?, ?, ?)",
                (chunk["text"], key, chunk["symbol"], cursor.lastrowid),
            )
    conn.commit()
    return len(batch)


def _walk(root: Path, rules: list[str]):
    count = 0
    for folder, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.startswith(".")]
        for name in filenames:
            path = Path(folder) / name
            if path.suffix.lower() not in _EXTENSIONS:
                continue
            if is_secret(path):
                continue
            relative = path.relative_to(root).as_posix()
            if _ignored(relative, rules):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES or path.stat().st_size == 0:
                    continue
            except OSError:
                continue
            count += 1
            yield path
            if count >= _MAX_FILES:
                return


def _ignore_rules(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.is_file():
        return []
    rules = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        rules.append(item)
    return rules


def _ignored(relative: str, rules: list[str]) -> bool:
    import fnmatch

    ignored = False
    name = relative.split("/")[-1]
    for rule in rules:
        negative = rule.startswith("!")
        body = rule[1:] if negative else rule
        body = body[1:] if body.startswith("/") else body
        pattern = body.replace("**/", "*").replace("**", "*").rstrip("/")
        matched = fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, "*/" + pattern)
        if matched:
            ignored = not negative
    return ignored


def find_symbols(root: Path, name: str, limit: int = 30) -> list[dict]:
    """Arquivo e linha de um símbolo, sem embedding. Usa o índice se existir e também a varredura da pasta."""
    needle = name.strip().lower()
    if not needle:
        return []
    found: list[dict] = []
    seen: set[tuple[str, int, str]] = set()

    def add(file_path: str, line: int, symbol: str) -> None:
        key = (file_path, int(line), symbol)
        if key in seen or needle not in symbol.lower():
            return
        seen.add(key)
        found.append({"arquivo": file_path, "linha": int(line), "simbolo": symbol})

    db = project_db(root)
    if db.is_file():
        conn = _connect(db)
        try:
            rows = conn.execute(
                "SELECT path, start, symbol FROM chunks WHERE symbol != '' AND lower(symbol) LIKE ? LIMIT ?",
                (f"%{needle}%", max(limit, 1)),
            ).fetchall()
        finally:
            conn.close()
        for file_path, start, symbol in rows:
            add(str(file_path), int(start), str(symbol))
    if len(found) >= limit:
        return found[:limit]
    rules = _ignore_rules(root)
    for path in _walk(root, rules):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for item in _symbol_entries(path, text):
            add(str(path), int(item["start"]), str(item["symbol"]))
            if len(found) >= limit:
                return found
    return found


def _chunks(path: Path, root: Path, text: str) -> list[dict]:
    symbols = _symbol_entries(path, text)
    if path.suffix.lower() == ".py" and symbols:
        return symbols
    return _windows(text) + [item for item in symbols if item["symbol"]]


def _symbol_entries(path: Path, text: str) -> list[dict]:
    if path.suffix.lower() == ".py":
        return _python_chunks(text)
    return _regex_symbols(text)


def _python_chunks(text: str) -> list[dict]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    pieces = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", None) or start
        pieces.append({"start": start, "end": end, "symbol": node.name, "text": "\n".join(lines[start - 1 : end])[:4000]})
    return pieces


_SYMBOL_PATTERNS = (
    re.compile(r"(?m)^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)"),
    re.compile(r"(?m)^[ \t]*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_]\w*)"),
    re.compile(r"(?m)^[ \t]*(?:public|private|protected)?\s*(?:static\s+)?function\s+([A-Za-z_]\w*)"),
    re.compile(r"(?m)^[ \t]*def\s+([A-Za-z_]\w*)"),
    re.compile(r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
    re.compile(r"(?m)^[ \t]*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    re.compile(r"(?m)^[ \t]+(?:public|private|protected)?\s*(?:static\s+)?(?:async\s+)?([A-Za-z_]\w*)\s*\([^;]*\)\s*\{"),
)
_NOT_SYMBOL = {"if", "for", "while", "switch", "catch", "foreach", "return"}


def _regex_symbols(text: str) -> list[dict]:
    lines = text.splitlines()
    pieces = []
    seen: set[tuple[int, str]] = set()
    for pattern in _SYMBOL_PATTERNS:
        for match in pattern.finditer(text):
            name = next((group for group in match.groups() if group), "")
            if not name or name.lower() in _NOT_SYMBOL:
                continue
            line = text.count("\n", 0, match.start()) + 1
            key = (line, name)
            if key in seen:
                continue
            seen.add(key)
            pieces.append({"start": line, "end": line, "symbol": name, "text": lines[line - 1][:400] if line <= len(lines) else name})
    return pieces


def _guard_embed(embed):
    state = {"ok": True, "mensagem": ""}

    def wrapped(texts: list[str]):
        if not state["ok"]:
            return [[] for _ in texts]
        try:
            vectors = embed(texts)
        except Exception:
            state["ok"] = False
            state["mensagem"] = "A busca vetorial está desligada."
            return [[] for _ in texts]
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            state["ok"] = False
            state["mensagem"] = "A busca vetorial está desligada."
            return [[] for _ in texts]
        return vectors

    wrapped.state = state
    return wrapped


def _windows(text: str) -> list[dict]:
    lines = text.splitlines()
    if not lines:
        return []
    pieces = []
    start = 0
    while start < len(lines):
        end = min(len(lines), start + _WINDOW)
        pieces.append(
            {
                "start": start + 1,
                "end": end,
                "symbol": "",
                "text": "\n".join(lines[start:end])[:4000],
            }
        )
        if end >= len(lines):
            break
        start += max(1, _WINDOW - _OVERLAP)
    return pieces


def _pack(vector: list[float]) -> bytes:
    values = array("f", (float(item) for item in vector))
    return struct.pack("<I", len(values)) + values.tobytes()


def _unpack(blob: bytes | None) -> list[float]:
    if not blob or len(blob) < 4:
        return []
    count = struct.unpack("<I", blob[:4])[0]
    values = array("f")
    raw = blob[4:]
    if len(raw) != count * 4:
        return []
    values.frombytes(raw)
    return list(values)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)
