from __future__ import annotations

import csv
import io
import json
import time
import uuid
import zipfile
from pathlib import Path

from ollamapilot.config import Settings
from ollamapilot.paths import ARTIFACTS
from ollamapilot.tools.base import Tool, schema, text_prop, tool

_EXTENSIONS = {
    "txt",
    "md",
    "csv",
    "json",
    "xml",
    "html",
    "css",
    "js",
    "ts",
    "php",
    "py",
    "sql",
    "java",
    "c",
    "cpp",
    "h",
    "yaml",
    "yml",
    "pdf",
    "docx",
    "xlsx",
    "zip",
}


@tool
class CreateArtifact(Tool):
    name = "criar_arquivo"
    group = "biblioteca"
    order = 210
    mutates = True
    scope_label = "gerar PDF, DOCX, XLSX, ZIP e texto"
    description = (
        "Cria PDF, DOCX, XLSX, ZIP ou texto. Use também quando o pedido vier no prompt e indicar um caminho: "
        "passe esse caminho absoluto em caminho. Não use gravar_arquivo para pdf, docx, xlsx ou zip. "
        "documento para PDF e DOCX: {titulo, blocos:[{tipo: titulo|paragrafo|lista|tabela, texto, nivel, itens, colunas, linhas}]}. "
        "documento para XLSX: {abas:[{nome, colunas, linhas}]} ou conteudo em CSV. "
        "Sem caminho, o arquivo fica em Arquivos gerados por 24 h. Retorno: {caminho, nome, formato}."
    )
    sensitive = True
    grant_key = "biblioteca"
    parameters = schema(
        {
            "nome": text_prop("Nome do arquivo com extensão, como relatorio.pdf."),
            "conteudo": text_prop("Texto puro, CSV da planilha, ou o mesmo JSON de documento em texto."),
            "caminho": text_prop("Opcional. Caminho absoluto pedido pelo usuário, como E:\\saida\\relatorio.pdf."),
            "documento": {
                "type": "object",
                "description": "Estrutura do PDF, DOCX ou XLSX: titulo e blocos, ou abas.",
            },
            "arquivos": {
                "type": "array",
                "description": "Para ZIP: lista de objetos com nome e conteudo ou documento.",
                "items": {"type": "object"},
            },
        },
        ["nome"],
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def grant_key_for(self, arguments: dict) -> str:
        raw = str(arguments.get("caminho") or "").strip()
        if not raw:
            return "biblioteca"
        from ollamapilot.tools.paths import resolve_path
        from ollamapilot.workspace import inside

        path = resolve_path(raw)
        agent = getattr(getattr(self, "ctx", None), "agent", None)
        root = getattr(agent, "workspace", None)
        if root is not None and inside(root, path):
            return "arquivos"
        import os

        return "caminho:" + os.path.normcase(str(path))

    def preview(self, arguments: dict) -> dict:
        name = _safe_name(str(arguments.get("nome") or ""))
        raw = str(arguments.get("caminho") or "").strip()
        kind = _describe(name, arguments.get("conteudo"), arguments.get("documento"))
        if not raw:
            return {
                "title": "Gerar arquivo",
                "summary": name,
                "detail": f"O arquivo {name} será criado na biblioteca do OllamaPilot.\n{kind}",
            }
        from ollamapilot.tools.paths import resolve_path, risk_note

        path = resolve_path(raw)
        if path.is_dir():
            raise ValueError("Esse caminho é uma pasta. Informe o arquivo.")
        note = risk_note(path)
        action = "Substituir" if path.is_file() else "Criar"
        detail = f"{path}\n\n{action} um {path.suffix.lower()} de verdade, não um arquivo de texto com essa extensão.\n{kind}"
        if note:
            detail = note + "\n\n" + detail
        return {"title": f"{action} {path.suffix.lower().lstrip('.')}", "summary": str(path), "detail": detail}

    def run(self, arguments: dict) -> dict:
        name = _safe_name(str(arguments.get("nome") or ""))
        content = arguments.get("conteudo")
        if content is None:
            content = ""
        content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        self._check_size(content)
        documento = arguments.get("documento")
        raw = str(arguments.get("caminho") or "").strip()
        if raw:
            from ollamapilot.tools.paths import resolve_path

            target = resolve_path(raw)
            if target.is_dir():
                raise ValueError("Esse caminho é uma pasta. Informe o arquivo.")
            target.parent.mkdir(parents=True, exist_ok=True)
            where = "no caminho pedido"
        else:
            folder = ARTIFACTS / uuid.uuid4().hex
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            where = "em Arquivos gerados"
        extension = target.suffix.lower().lstrip(".")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.parcial")
        undoable = False
        try:
            _write_document(temporary, extension, content, documento, arguments.get("arquivos"))
            if temporary.stat().st_size > self.settings.max_write_bytes:
                raise ValueError("O arquivo gerado excede o limite.")
            if raw:
                from ollamapilot.tools.filesystem import _keep_undo

                backup = _keep_undo(self, target, "criar_arquivo")
                undoable = backup is not None
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        if not raw:
            meta = {"nome": name, "caminho": str(target), "criado": time.time()}
            (target.parent / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {
            "caminho": str(target),
            "nome": target.name,
            "formato": extension,
            "pode_desfazer": bool(raw and undoable),
            "mensagem": f"Arquivo {extension} disponível {where}.",
        }

    def _check_size(self, content: str) -> None:
        if len(content.encode("utf-8")) > self.settings.max_write_bytes:
            raise ValueError(f"O conteúdo excede {self.settings.max_write_bytes} bytes.")


def list_artifacts() -> list[dict]:
    purge()
    rows = []
    if not ARTIFACTS.is_dir():
        return rows
    for meta_path in ARTIFACTS.glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        path = Path(str(meta.get("caminho") or ""))
        if path.is_file():
            rows.append(meta)
    rows.sort(key=lambda item: item.get("criado") or 0, reverse=True)
    return rows


def purge(ttl_seconds: int = 24 * 60 * 60) -> None:
    if not ARTIFACTS.is_dir():
        return
    limit = time.time() - ttl_seconds
    for meta_path in ARTIFACTS.glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            created = float(meta.get("criado") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if created < limit:
            import shutil

            shutil.rmtree(meta_path.parent, ignore_errors=True)


def _safe_name(name: str) -> str:
    name = Path(name).name.strip()
    if not name or name in {".", ".."} or any(char in name for char in '<>:"|?*'):
        raise ValueError("Nome de arquivo inválido.")
    extension = Path(name).suffix.lower().lstrip(".")
    if extension not in _EXTENSIONS:
        raise ValueError("Extensão não suportada na biblioteca. Para outro tipo, use gravar_arquivo.")
    return name


def _describe(name: str, content, documento) -> str:
    document = _document_from(content, documento)
    if not document:
        return "Conteúdo em texto puro."
    blocks = document.get("blocos") or []
    sheets = document.get("abas") or []
    parts = []
    if document.get("titulo"):
        parts.append(f"Título: {document.get('titulo')}")
    if blocks:
        parts.append(f"{len(blocks)} blocos")
    if sheets:
        parts.append(f"{len(sheets)} abas")
    return ". ".join(parts) or Path(name).suffix.lower()


def _document_from(content, documento) -> dict | None:
    # O modelo pode mandar o objeto em documento ou o mesmo JSON dentro de conteudo.
    if isinstance(documento, dict) and any(documento.get(key) for key in ("blocos", "abas", "titulo")):
        return documento
    text = str(content or "").strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and any(data.get(key) for key in ("blocos", "abas", "titulo")):
        return data
    return None


def _write_document(target: Path, extension: str, content: str, documento, files) -> None:
    document = _document_from(content, documento)
    if extension == "pdf":
        _pdf(target, content, document)
    elif extension == "docx":
        _docx(target, content, document)
    elif extension == "xlsx":
        _xlsx(target, content, document)
    elif extension == "zip":
        _zip(target, files, content)
    else:
        target.write_text(content, encoding="utf-8", newline="\n")


def _pdf(target: Path, content: str, document: dict | None) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    font = Path(r"C:\Windows\Fonts\arial.ttf")
    bold = Path(r"C:\Windows\Fonts\arialbd.ttf")
    unicode_font = font.is_file()
    if unicode_font:
        pdf.add_font("Body", "", str(font))
        if bold.is_file():
            pdf.add_font("Body", "B", str(bold))
        pdf.set_font("Body", size=11)
    else:
        pdf.set_font("Helvetica", size=11)

    def show(text: str) -> str:
        text = str(text or "")
        if unicode_font:
            return text
        return text.encode("latin-1", errors="replace").decode("latin-1")

    if not document:
        pdf.multi_cell(0, 6, show(content))
        pdf.output(str(target))
        return
    title, blocks, _sheets = _parts(document, "pdf")
    if title:
        pdf.set_font("Body" if unicode_font else "Helvetica", "B" if unicode_font else "", 16)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 8, show(title))
        pdf.ln(2)
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Body" if unicode_font else "Helvetica", size=11)
    if not blocks and not title:
        raise ValueError("O PDF precisa de titulo ou de blocos.")
    for block in blocks:
        pdf.set_x(pdf.l_margin)
        _pdf_block(pdf, block, show, unicode_font)
    pdf.output(str(target))


def _pdf_block(pdf, block: dict, show, unicode_font) -> None:
    kind = _kind(block)
    family = "Body" if unicode_font else "Helvetica"
    if kind == "titulo":
        size = {1: 14, 2: 12, 3: 11}.get(_level(block), 12)
        pdf.ln(1)
        pdf.set_font(family, "B" if unicode_font else "", size)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 7, show(block.get("texto")))
        pdf.set_x(pdf.l_margin)
        pdf.set_font(family, size=11)
        return
    if kind == "lista":
        for item in _items(block)[:_MAX_LIST]:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 6, show(f"• {item}"))
        pdf.ln(1)
        pdf.set_x(pdf.l_margin)
        return
    if kind == "tabela":
        columns, rows = _table(block)
        pdf.set_font(family, size=9)
        with pdf.table(first_row_as_headings=True, line_height=6, text_align="LEFT") as table:
            heading = table.row()
            for column in columns:
                heading.cell(show(column))
            for row in rows:
                line = table.row()
                for value in row:
                    line.cell(show(value))
        pdf.set_font(family, size=11)
        pdf.ln(2)
        pdf.set_x(pdf.l_margin)
        return
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 6, show(block.get("texto")))
    pdf.ln(1)


def _docx(target: Path, content: str, document: dict | None) -> None:
    from docx import Document

    book = Document()
    if not document:
        for paragraph in content.splitlines() or [""]:
            book.add_paragraph(paragraph)
        book.save(str(target))
        return
    title, blocks, _sheets = _parts(document, "docx")
    if title:
        book.add_heading(title, level=0)
    if not blocks and not title:
        raise ValueError("O DOCX precisa de titulo ou de blocos.")
    for block in blocks:
        kind = _kind(block)
        if kind == "titulo":
            book.add_heading(str(block.get("texto") or ""), level=_level(block))
        elif kind == "lista":
            for item in _items(block)[:_MAX_LIST]:
                book.add_paragraph(str(item), style="List Bullet")
        elif kind == "tabela":
            columns, rows = _table(block)
            table = book.add_table(rows=1 + len(rows), cols=len(columns))
            table.style = "Table Grid"
            for index, column in enumerate(columns):
                table.rows[0].cells[index].text = str(column)
            for row_index, row in enumerate(rows, 1):
                for col_index, value in enumerate(row):
                    table.rows[row_index].cells[col_index].text = str(value)
            book.add_paragraph("")
        else:
            book.add_paragraph(str(block.get("texto") or ""))
    book.save(str(target))


def _xlsx(target: Path, content: str, document: dict | None) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheets = _sheets(document, content)
    first = True
    for spec in sheets:
        sheet = book.active if first else book.create_sheet()
        first = False
        sheet.title = _sheet_title(spec.get("nome"))
        columns, rows = _table(spec)
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="0F6E56")
        for index, column in enumerate(columns, 1):
            cell = sheet.cell(1, index, column)
            cell.font = header_font
            cell.fill = header_fill
        for row_index, row in enumerate(rows, 2):
            for col_index, value in enumerate(row, 1):
                sheet.cell(row_index, col_index, _cell_value(value))
        for index, column in enumerate(columns, 1):
            width = min(40, max(12, len(str(column)) + 2))
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.freeze_panes = "A2"
        if columns:
            sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{1 + len(rows)}"
    book.save(str(target))


def _zip(target: Path, files, content: str) -> None:
    items = files
    if isinstance(items, str):
        try:
            items = json.loads(items or "[]")
        except json.JSONDecodeError:
            items = None
    if not items:
        try:
            items = json.loads(content or "[]")
        except json.JSONDecodeError as error:
            raise ValueError("Para ZIP, envie a lista em arquivos, com nome e conteudo.") from error
    if not isinstance(items, list) or not items:
        raise ValueError("O ZIP precisa de ao menos um arquivo.")
    if len(items) > 30:
        raise ValueError("O ZIP aceita até 30 arquivos.")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Cada item do ZIP precisa ter nome e conteudo.")
            name = Path(str(item.get("nome") or "")).name
            if not name:
                raise ValueError("Um item do ZIP está sem nome.")
            extension = Path(name).suffix.lower().lstrip(".")
            if extension in {"pdf", "docx", "xlsx"}:
                buffer = target.parent / f".{uuid.uuid4().hex}.{extension}"
                try:
                    _write_document(buffer, extension, str(item.get("conteudo") or ""), item.get("documento"), None)
                    archive.write(buffer, arcname=name)
                finally:
                    buffer.unlink(missing_ok=True)
            else:
                archive.writestr(name, str(item.get("conteudo") or ""))


_MAX_BLOCKS = 200
_MAX_ROWS = 2000
_MAX_COLS = 20
_MAX_LIST = 200


def _parts(document: dict, extension: str) -> tuple[str, list, list]:
    title = str(document.get("titulo") or "").strip()
    blocks = [block for block in (document.get("blocos") or []) if isinstance(block, dict)]
    sheets = [sheet for sheet in (document.get("abas") or []) if isinstance(sheet, dict)]
    if len(blocks) > _MAX_BLOCKS:
        raise ValueError(f"O documento aceita até {_MAX_BLOCKS} blocos.")
    if extension in {"pdf", "docx"} and not blocks and sheets:
        for sheet in sheets[:30]:
            blocks.append({"tipo": "titulo", "texto": sheet.get("nome") or "Planilha", "nivel": 2})
            blocks.append({"tipo": "tabela", "colunas": sheet.get("colunas"), "linhas": sheet.get("linhas")})
    return title, blocks, sheets


def _sheets(document: dict | None, content: str) -> list[dict]:
    if document:
        sheets = [sheet for sheet in (document.get("abas") or []) if isinstance(sheet, dict)]
        if not sheets:
            for block in document.get("blocos") or []:
                if isinstance(block, dict) and _kind(block) == "tabela":
                    sheets.append(
                        {
                            "nome": block.get("nome") or block.get("texto") or "Planilha",
                            "colunas": block.get("colunas"),
                            "linhas": block.get("linhas"),
                        }
                    )
        if sheets:
            if len(sheets) > 30:
                raise ValueError("A planilha aceita até 30 abas.")
            return sheets
    rows = list(csv.reader(io.StringIO(str(content or ""))))
    if not rows:
        raise ValueError("A planilha precisa de abas ou de conteúdo CSV.")
    width = max(len(row) for row in rows)
    columns = rows[0] if rows else []
    if len(columns) < width:
        columns = columns + [f"Coluna {index}" for index in range(len(columns) + 1, width + 1)]
    return [{"nome": "Planilha", "colunas": columns, "linhas": rows[1:]}]


def _kind(block: dict) -> str:
    raw = str(block.get("tipo") or "paragrafo").strip().lower()
    names = {
        "titulo": "titulo",
        "heading": "titulo",
        "paragrafo": "paragrafo",
        "paragraph": "paragrafo",
        "texto": "paragrafo",
        "lista": "lista",
        "list": "lista",
        "tabela": "tabela",
        "table": "tabela",
    }
    return names.get(raw, "paragrafo")


def _level(block: dict) -> int:
    try:
        level = int(block.get("nivel") or 1)
    except (TypeError, ValueError):
        level = 1
    return min(3, max(1, level))


def _items(block: dict) -> list[str]:
    raw = block.get("itens") or block.get("items") or []
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines() if line.strip()]
    if not isinstance(raw, list) or not raw:
        raise ValueError("A lista precisa de itens.")
    return [str(item) for item in raw]


def _table(block: dict) -> tuple[list[str], list[list]]:
    rows = block.get("linhas") or block.get("rows") or []
    if isinstance(rows, str):
        rows = list(csv.reader(io.StringIO(rows)))
    if not isinstance(rows, list):
        raise ValueError("A tabela precisa de linhas.")
    columns = block.get("colunas") or block.get("columns") or []
    if isinstance(columns, str):
        columns = next(csv.reader(io.StringIO(columns)), [])
    if not isinstance(columns, list):
        raise ValueError("A tabela precisa de colunas.")
    columns = [str(item) for item in columns][:_MAX_COLS]
    normalized = []
    for row in rows[:_MAX_ROWS]:
        if isinstance(row, dict):
            values = [row.get(column, "") for column in columns] if columns else list(row.values())
        elif isinstance(row, list):
            values = row
        else:
            values = [row]
        normalized.append(values)
    width = max([len(columns), *[len(row) for row in normalized]], default=0)
    if width == 0:
        raise ValueError("A tabela precisa de colunas ou de linhas.")
    if width > _MAX_COLS:
        raise ValueError(f"A tabela aceita até {_MAX_COLS} colunas.")
    if len(columns) < width:
        columns = columns + [f"Coluna {index}" for index in range(len(columns) + 1, width + 1)]
    rectangular = []
    for row in normalized:
        values = list(row) + [""] * (width - len(row))
        rectangular.append(values[:width])
    return columns, rectangular


def _sheet_title(name) -> str:
    cleaned = "".join(char for char in str(name or "Planilha") if char not in "[]:*?/\\")
    cleaned = cleaned.strip() or "Planilha"
    return cleaned[:31]


def _cell_value(value):
    if isinstance(value, bool) or value is None:
        return "" if value is None else value
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text and text.lstrip("-").isdigit():
        return text
    try:
        if text and "." in text:
            return float(text)
    except ValueError:
        return text
    return text
