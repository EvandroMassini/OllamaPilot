from __future__ import annotations

import difflib
import os
import re
import shutil
import time
from pathlib import Path

from ollamapilot import undo
from ollamapilot.config import Settings
from ollamapilot.paths import write_text_atomic
from ollamapilot.tools.base import Tool, bool_prop, int_prop, schema, text_prop, tool
from ollamapilot.tools.paths import clip, is_secret, resolve_path, risk_note

MAX_DELETE_ITEMS = 200


@tool
class ListDirectory(Tool):
    name = "listar_pasta"
    order = 10
    description = (
        "Lista um nível de uma pasta: nome, tipo (dir/file) e tamanho. Use antes de ler ou gravar quando não souber o que existe. "
        "Retorno: {caminho, entries, total, truncated}; mostra até 200 itens."
    )
    parameters = schema({"caminho": text_prop("Caminho absoluto da pasta, como E:\\projeto.")}, ["caminho"])

    def needs_approval(self, arguments: dict) -> bool:
        return is_secret(resolve_path(arguments.get("caminho")))

    def preview(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        return {"title": "Ler pasta sensível", "summary": str(path), "detail": f"Listar o conteúdo de {path}"}

    def run(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        if not path.is_dir():
            raise ValueError("Informe uma pasta que exista.")
        entries = []
        for entry in os.scandir(path):
            try:
                kind = "dir" if entry.is_dir(follow_symlinks=False) else "file"
                size = None if kind == "dir" else entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            entries.append({"name": entry.name, "type": kind, "size": size})
        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
        truncated = len(entries) > 200
        return {"caminho": str(path), "entries": entries[:200], "total": len(entries), "truncated": truncated}


@tool
class ReadFile(Tool):
    name = "ler_arquivo"
    order = 20
    description = (
        "Lê um arquivo de texto (código, txt, csv, json, html) e também PDF, DOCX, XLSX, PPTX, ODT, EPUB, RTF e XLS, devolvendo o texto. "
        "Planilha com fórmula devolve a fórmula e, se houver, o valor em cache. "
        "Se o PDF não tiver texto, rasteriza até 4 páginas: com visão, anexa as imagens; sem visão, usa OCR se o Tesseract estiver no PATH. "
        "Áudio (wav, mp3, m4a, flac) só segue se o modelo declarar a capacidade audio. "
        "Para executável ou outro binário, use ler_binario. "
        "Lê até o limite das configurações. inicio e fim recortam pelas linhas, começando em 1. "
        "numerar=true prefixa o número da linha; não copie esse prefixo para um patch. "
        "Retorno: {caminho, codificacao, bytes, truncated, conteudo}."
    )
    parameters = schema(
        {
            "caminho": text_prop("Caminho absoluto do arquivo, como E:\\projeto\\main.py."),
            "inicio": int_prop("Opcional. Primeira linha, começando em 1."),
            "fim": int_prop("Opcional. Última linha, inclusive."),
            "numerar": bool_prop("Opcional. Mostra o número de cada linha."),
        },
        ["caminho"],
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def needs_approval(self, arguments: dict) -> bool:
        return is_secret(resolve_path(arguments.get("caminho")))

    def preview(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        return {
            "title": "Ler arquivo sensível",
            "summary": str(path),
            "detail": f"{path}\nO nome indica senha, chave ou credencial. O conteúdo irá para o modelo.",
        }

    def run(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        if not path.is_file():
            raise ValueError("Informe um arquivo que exista.")
        size = path.stat().st_size
        limit = self.settings.max_read_bytes
        suffix = path.suffix.lower()
        if suffix in _AUDIO:
            return self._audio(path)
        if suffix in _DOCUMENTS:
            return self._document(path, limit, arguments)
        with path.open("rb") as handle:
            if b"\x00" in handle.read(8192):
                raise ValueError("O arquivo é binário. Use ler_binario.")
        numbered = bool(arguments.get("numerar"))
        start = arguments.get("inicio")
        end = arguments.get("fim")
        if start not in (None, "", 0) or end not in (None, "", 0):
            return self._range(path, size, limit, start, end, numbered)
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        truncated = len(data) > limit
        if truncated:
            data = data[:limit]
        text, encoding = decode_text(data)
        result = {"caminho": str(path), "codificacao": encoding, "bytes": size, "truncated": truncated}
        if not truncated:
            result["total_linhas"] = len(text.splitlines())
        result["conteudo"] = _numbered(text.splitlines(), 1) if numbered else text
        return result

    def _range(self, path: Path, size: int, limit: int, start, end, numbered: bool) -> dict:
        first = max(1, int(start or 1))
        last = int(end) if end not in (None, "", 0) else None
        if last is not None and last < first:
            raise ValueError("A linha final é anterior à inicial.")
        collected: list[bytes] = []
        used = 0
        seen = 0
        cut = False
        with path.open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                seen = number
                if number < first:
                    continue
                if last is not None and number > last:
                    break
                if used + len(raw) > limit:
                    cut = True
                    break
                collected.append(raw)
                used += len(raw)
        if not collected:
            raise ValueError(f"O arquivo tem {seen} linhas; a linha {first} não existe.")
        text, encoding = decode_text(b"".join(collected))
        lines = text.splitlines()
        return {
            "caminho": str(path),
            "codificacao": encoding,
            "bytes": size,
            "truncated": cut,
            "inicio": first,
            "fim": first + len(lines) - 1,
            "conteudo": _numbered(lines, first) if numbered else "\n".join(lines),
        }

    def _document(self, path: Path, limit: int, arguments: dict) -> dict:
        """PDF, DOCX e XLSX: o texto extraído, com o mesmo recorte por linha dos arquivos de texto."""
        from ollamapilot.attachments import read_attachment

        loaded = read_attachment(path, max(limit, 1))
        if not loaded["ok"]:
            erro = str(loaded.get("erro") or "")
            if path.suffix.lower() == ".pdf" and "não tem texto extraível" in erro:
                return self._scanned_pdf(path, arguments)
            raise ValueError(erro)
        anexo = loaded["anexo"]
        lines = str(anexo.get("text") or "").splitlines()
        numbered = bool(arguments.get("numerar"))
        start = arguments.get("inicio")
        end = arguments.get("fim")
        ranged = start not in (None, "", 0) or end not in (None, "", 0)
        if ranged:
            first = max(1, int(start or 1))
            last = int(end) if end not in (None, "", 0) else len(lines)
            if last < first:
                raise ValueError("A linha final é anterior à inicial.")
            chosen = lines[first - 1 : last]
            if not chosen:
                raise ValueError(f"O texto extraído tem {len(lines)} linhas; a linha {first} não existe.")
            body = _numbered(chosen, first) if numbered else "\n".join(chosen)
        else:
            first = None
            body = _numbered(lines, 1) if numbered else "\n".join(lines)
        result = {
            "caminho": str(path),
            "codificacao": str(anexo.get("tipo") or path.suffix.lower().lstrip(".")),
            "bytes": path.stat().st_size,
            "truncated": bool(anexo.get("truncado")),
            "conteudo": body,
        }
        if anexo.get("paginas"):
            result["paginas"] = anexo["paginas"]
        if ranged:
            result["inicio"] = first
            result["fim"] = first + len(chosen) - 1
        elif not result["truncated"]:
            result["total_linhas"] = len(lines)
        return result

    def _scanned_pdf(self, path: Path, arguments: dict) -> dict:
        """PDF só de imagem: rasteriza se não houver imagem embutida e só então decide visão ou OCR."""
        from pypdf import PdfReader

        from ollamapilot.attachments import PDF_IMAGE_PAGES, pdf_page_images

        total = len(PdfReader(str(path)).pages)
        model = str(getattr(self.settings, "model", "") or "atual")
        vision = _vision_state(self)
        start = arguments.get("inicio")
        end = arguments.get("fim")
        first = max(1, int(start or 1))
        last = int(end) if end not in (None, "", 0) else first + PDF_IMAGE_PAGES - 1
        if last < first:
            raise ValueError("A página final é anterior à inicial.")
        rendered = pdf_page_images(path, first, last)
        images = rendered["imagens"]
        if not images:
            if rendered.get("dependencia"):
                raise ValueError(str(rendered["dependencia"]))
            raise ValueError(
                f"O PDF tem {total} páginas sem texto e não encontrei uma imagem de página para ler. O conteúdo não foi lido."
            )
        numbers = [item["pagina"] for item in images]
        raster_only = all(item.get("raster") for item in images)
        if vision is not True and raster_only:
            return self._pdf_without_vision(path, model, vision, images, numbers, total)
        if vision is not True:
            if vision is None:
                raise ValueError(
                    f"O PDF não tem texto selecionável: as {total} páginas são imagem. "
                    f"As capacidades do modelo {model} não informam visão, então as páginas não foram enviadas. "
                    "Escolha um modelo que declare visão, ou um PDF com texto selecionável."
                )
            raise ValueError(
                f"O PDF não tem texto selecionável: as {total} páginas são imagem. "
                f"O modelo {model} não tem visão e não consegue ler essas páginas. "
                "Escolha um modelo com visão."
            )
        result = {
            "caminho": str(path),
            "codificacao": "pdf-imagem",
            "bytes": path.stat().st_size,
            "paginas": total,
            "paginas_anexadas": numbers,
            "mensagem": (
                f"Não há texto selecionável. O modelo {model} tem visão, então as páginas {numbers[0]} a {numbers[-1]} "
                "foram anexadas como imagem. Leia o que está escrito nelas."
            ),
            "_imagem": [item["base64"] for item in images],
            "_imagem_nota": (
                f"[Páginas {numbers[0]} a {numbers[-1]} de {path.name}, devolvidas por ler_arquivo. "
                "São um resultado, não um pedido novo. Leia o que está escrito nas imagens.]"
            ),
        }
        nxt = numbers[-1] + 1
        if nxt <= total:
            result["proxima_pagina"] = nxt
            result["mensagem"] += f" Ainda há páginas. Chame ler_arquivo de novo com inicio={nxt} antes de concluir."
        return result

    def _pdf_without_vision(self, path: Path, model: str, vision: bool | None, images: list[dict], numbers: list[int], total: int) -> dict:
        from ollamapilot.attachments import ocr_image
        import base64
        import io

        from PIL import Image

        pieces = []
        for item in images:
            try:
                raw = base64.b64decode(item["base64"])
                image = Image.open(io.BytesIO(raw))
                text = ocr_image(image)
            except Exception:
                text = None
            if text:
                pieces.append(f"[página {item['pagina']} OCR]\n{text}")
        if pieces:
            return {
                "caminho": str(path),
                "codificacao": "pdf-ocr",
                "bytes": path.stat().st_size,
                "paginas": total,
                "paginas_anexadas": numbers,
                "origem": "OCR",
                "conteudo": "\n".join(pieces),
                "mensagem": "Texto obtido por OCR. A página não tem texto selecionável.",
            }
        if vision is None:
            raise ValueError(
                f"A página é imagem. As capacidades do modelo {model} não informam visão e não há OCR instalado. "
                "O conteúdo não foi lido."
            )
        raise ValueError(
            f"A página é imagem. O modelo {model} não tem visão e não há OCR instalado. O conteúdo não foi lido."
        )

    def _audio(self, path: Path) -> dict:
        import base64

        from ollamapilot.attachments import MAX_BYTES

        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(f"{path.name} passa de 10 MB e não foi lido.")
        caps = getattr(getattr(self, "ctx", None), "agent", None)
        known = getattr(caps, "_caps", None) or {}
        model = str(getattr(self.settings, "model", "") or "atual")
        if not (known.get("conhecido") and known.get("audio")):
            raise ValueError(
                f"O arquivo é áudio ({path.suffix.lower()}). O modelo {model} não declara a capacidade de áudio, "
                "então não há transcrição. O conteúdo não foi inventado."
            )
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "caminho": str(path),
            "formato": "audios",
            "bytes": size,
            "mensagem": (
                "O modelo declara áudio. O arquivo foi enviado em base64 no campo audios, "
                "que é o formato do chat do Ollama para a capacidade audio."
            ),
            "_audio": encoded,
            "_audio_nota": f"[Áudio {path.name} devolvido por ler_arquivo, no formato audios. Transcreva só o que o modelo ouvir.]",
        }


def _vision_state(tool) -> bool | None:
    """True se o modelo declara visão, False se declara que não tem, None se isso não foi informado."""
    agent = getattr(getattr(tool, "ctx", None), "agent", None)
    caps = getattr(agent, "_caps", None) or {}
    if not caps.get("conhecido"):
        return None
    return bool(caps.get("visao"))


_DOCUMENTS = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".odt", ".epub", ".rtf", ".xls"}
_AUDIO = {".wav", ".mp3", ".m4a", ".flac"}


@tool
class WriteFile(Tool):
    name = "gravar_arquivo"
    order = 50
    mutates = True
    scope_label = "gravar, editar, criar pasta e desfazer arquivos"
    description = (
        "Cria ou substitui por inteiro um arquivo de texto no caminho absoluto pedido, criando as pastas que faltarem. "
        "Não gera PDF, DOCX, XLSX nem ZIP: para esses formatos use criar_arquivo com caminho e documento. "
        "Use para arquivo novo. Para mudar um trecho de um arquivo existente, use aplicar_patch ou editar_arquivo. "
        "Recusa regravação que encurta demais o arquivo ou que traz reticências no lugar do código. "
        "Pede autorização e guarda a versão anterior para desfazer_alteracao. Retorno: {caminho, bytes, criado}."
    )
    sensitive = True
    grant_key = "arquivos"
    parameters = schema(
        {
            "caminho": text_prop("Caminho absoluto do arquivo, como E:\\site\\index.html."),
            "conteudo": text_prop("Conteúdo completo do arquivo."),
        },
        ["caminho", "conteudo"],
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        content = str(arguments.get("conteudo") or "")
        self._check_size(content)
        if path.is_dir():
            raise ValueError("Esse caminho é uma pasta. Informe o arquivo.")
        _refuse_office(path)
        replacing = path.is_file()
        action = "Substituir" if replacing else "Criar"
        if replacing and path.stat().st_size > undo.MAX_FILE_BYTES:
            raise ValueError(_NO_UNDO)
        if replacing:
            old = _read_for_diff(path, self.settings.max_write_bytes)
            body = _diff(old, content, path) if old is not None else "(o arquivo atual é binário ou grande demais para comparar)"
            body = f"Tamanho atual: {path.stat().st_size} bytes. A versão atual fica guardada para desfazer.\n\n{body}"
        else:
            body, _ = clip(content, 3500)
        detail = f"{path}\n\n{body}"
        note = risk_note(path)
        if note:
            detail = note + "\n\n" + detail
        return {"title": f"{action} arquivo", "summary": f"{action} {path} ({len(content)} caracteres)", "detail": detail}

    def run(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        _refuse_office(path)
        content = str(arguments.get("conteudo") or "")
        self._check_size(content)
        if path.is_dir():
            raise ValueError("Esse caminho é uma pasta. Informe o arquivo.")
        if path.is_file():
            previous = _read_for_diff(path, self.settings.max_write_bytes)
            if previous is not None and _too_short(previous, content):
                raise ValueError(
                    "O conteúdo novo omite boa parte do arquivo existente. "
                    "Use aplicar_patch ou editar_arquivo com o trecho exato, em vez de regravar o arquivo."
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        backup = _keep_undo(self, path, "gravar_arquivo")
        write_text_atomic(path, content)
        return {"caminho": str(path), "bytes": path.stat().st_size, "criado": created, "pode_desfazer": backup is not None}

    def _check_size(self, content: str) -> None:
        if len(content.encode("utf-8")) > self.settings.max_write_bytes:
            raise ValueError(f"O conteúdo excede {self.settings.max_write_bytes} bytes.")


@tool
class MakeDirectory(Tool):
    name = "criar_pasta"
    order = 40
    mutates = True
    scope_label = "gravar, editar, criar pasta e desfazer arquivos"
    description = (
        "Cria uma pasta e as intermediárias no caminho absoluto pedido. gravar_arquivo já cria as pastas do arquivo; "
        "use esta só para pasta vazia. Retorno: {caminho, ja_existia}."
    )
    sensitive = True
    grant_key = "arquivos"
    parameters = schema({"caminho": text_prop("Caminho absoluto da pasta.")}, ["caminho"])

    def needs_approval(self, arguments: dict) -> bool:
        return not resolve_path(arguments.get("caminho")).is_dir()

    def preview(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        if path.is_file():
            raise ValueError("Já existe um arquivo nesse caminho.")
        detail = str(path)
        note = risk_note(path)
        if note:
            detail = note + "\n" + detail
        return {"title": "Criar pasta", "summary": str(path), "detail": detail}

    def run(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        if path.is_file():
            raise ValueError("Já existe um arquivo nesse caminho.")
        existed = path.is_dir()
        path.mkdir(parents=True, exist_ok=True)
        return {"caminho": str(path), "ja_existia": existed}


@tool
class EditFile(Tool):
    name = "editar_arquivo"
    order = 60
    mutates = True
    scope_label = "gravar, editar, criar pasta e desfazer arquivos"
    description = (
        "Troca um trecho exato de um arquivo de texto UTF-8. O trecho buscado precisa aparecer uma única vez; "
        "leia o arquivo antes e copie o trecho com algumas linhas de contexto. Pede autorização, mostra o diff e guarda a versão anterior. "
        "Retorno: {caminho, bytes, linha}."
    )
    sensitive = True
    grant_key = "arquivos"
    parameters = schema(
        {
            "caminho": text_prop("Caminho absoluto do arquivo."),
            "buscar": text_prop("Trecho atual, copiado do arquivo e único nele."),
            "substituir": text_prop("Trecho novo que entra no lugar."),
        },
        ["caminho", "buscar", "substituir"],
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        content, search, replace = self._check(path, arguments)
        updated = content.replace(search, replace, 1)
        return {
            "title": "Editar arquivo",
            "summary": f"{path} (linha {_line_of(content, search)})",
            "detail": f"{path}\n\n{_diff(content, updated, path)}",
        }

    def run(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        content, search, replace = self._check(path, arguments)
        updated = content.replace(search, replace, 1)
        if len(updated.encode("utf-8")) > self.settings.max_write_bytes:
            raise ValueError("O arquivo editado excede o limite.")
        backup = _keep_undo(self, path, "editar_arquivo")
        write_text_atomic(path, updated)
        return {
            "caminho": str(path),
            "bytes": path.stat().st_size,
            "linha": _line_of(content, search),
            "pode_desfazer": backup is not None,
        }

    def _check(self, path, arguments: dict) -> tuple[str, str, str]:
        search = str(arguments.get("buscar") or "")
        replace = str(arguments.get("substituir") or "")
        if not path.is_file():
            raise ValueError("O arquivo não existe.")
        if not search:
            raise ValueError("Informe o trecho que será substituído.")
        if search == replace:
            raise ValueError("O trecho novo é igual ao atual.")
        data = path.read_bytes()
        if len(data) > self.settings.max_write_bytes:
            raise ValueError("O arquivo é grande demais para editar por aqui.")
        if b"\x00" in data:
            raise ValueError("O arquivo é binário e não pode ser editado.")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("O arquivo não está em UTF-8.") from error
        count = content.count(search)
        if count == 0 and "\r\n" in content and "\r\n" not in search and "\n" in search:
            search = search.replace("\n", "\r\n")
            replace = replace.replace("\r\n", "\n").replace("\n", "\r\n")
            count = content.count(search)
        if count == 0:
            raise ValueError("O trecho não foi encontrado no arquivo. Leia o arquivo de novo e copie o trecho exato.")
        if count > 1:
            raise ValueError(f"O trecho aparece {count} vezes. Envie um trecho maior, que seja único.")
        return content, search, replace


@tool
class DeletePath(Tool):
    name = "excluir"
    order = 80
    mutates = True
    scope_label = "excluir arquivos e pastas"
    description = (
        "Apaga um arquivo ou uma pasta vazia. Pasta com conteúdo só com recursivo=true e até 200 itens. "
        "Use só quando o usuário pedir para apagar aquele caminho. Arquivo apagado pode ser restaurado com desfazer_alteracao; pasta recursiva não. "
        "Retorno: {caminho, apagado, itens}."
    )
    sensitive = True
    parameters = schema(
        {
            "caminho": text_prop("Caminho absoluto do arquivo ou da pasta."),
            "recursivo": bool_prop("Apaga a pasta e todo o conteúdo. Só até 200 itens. Padrão false."),
        },
        ["caminho"],
    )

    def preview(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        recursive = bool(arguments.get("recursivo"))
        _guard_delete(path)
        if path.is_file():
            stat = path.stat()
            changed = time.strftime("%d/%m/%Y %H:%M", time.localtime(stat.st_mtime))
            detail = f"Arquivo: {path}\nTamanho: {stat.st_size} bytes · modificado em {changed}"
            if stat.st_size > undo.MAX_FILE_BYTES:
                raise ValueError(_NO_UNDO)
            detail += "\nUma cópia fica guardada para desfazer."
        elif recursive:
            count = _count_items(path)
            detail = f"Pasta: {path}\nA pasta e {count} itens serão apagados. Não poderá ser desfeito."
        else:
            if any(path.iterdir()):
                raise ValueError("A pasta não está vazia. Peça de novo com recursivo, se for isso que o usuário quer apagar.")
            detail = f"Pasta vazia: {path}"
        note = risk_note(path)
        if note:
            detail = note + "\n" + detail
        return {"title": "Excluir", "summary": str(path), "detail": detail}

    def run(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        _guard_delete(path)
        if path.is_file():
            backup = _keep_undo(self, path, "excluir")
            path.unlink()
            return {"caminho": str(path), "apagado": True, "pode_desfazer": backup is not None}
        if bool(arguments.get("recursivo")):
            count = _count_items(path)
            shutil.rmtree(path)
            return {"caminho": str(path), "apagado": True, "itens": count, "pode_desfazer": False}
        try:
            path.rmdir()
        except OSError as error:
            raise ValueError("A pasta não está vazia. Peça de novo com recursivo, se for isso que o usuário quer apagar.") from error
        return {"caminho": str(path), "apagado": True}


@tool
class UndoChange(Tool):
    name = "desfazer_alteracao"
    order = 90
    mutates = True
    scope_label = "gravar, editar, criar pasta e desfazer arquivos"
    description = (
        "Desfaz a última alteração que o OllamaPilot fez num arquivo (gravar_arquivo, editar_arquivo ou excluir de arquivo): "
        "restaura a versão anterior ou apaga o arquivo que ele criou. Use quando o usuário pedir para desfazer ou voltar atrás. "
        "Retorno: {caminho, restaurado|removido, alteracao_desfeita}."
    )
    sensitive = True
    grant_key = "arquivos"
    parameters = schema({"caminho": text_prop("Caminho absoluto do arquivo alterado.")}, ["caminho"])

    def preview(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        entry = undo.latest(path)
        if entry is None:
            raise ValueError("Não há alteração do OllamaPilot guardada para esse arquivo.")
        return {"title": "Desfazer alteração", "summary": str(path), "detail": f"{path}\n\nEsta ação {undo.describe(entry)}."}

    def run(self, arguments: dict) -> dict:
        return undo.restore(resolve_path(arguments.get("caminho")))


class _Transfer(Tool):
    mutates = True
    sensitive = True
    grant_key = "arquivos"
    scope_label = "gravar, editar, criar pasta e desfazer arquivos"
    verb = ""
    parameters = schema(
        {
            "origem": text_prop("Caminho absoluto do arquivo de origem."),
            "destino": text_prop("Caminho absoluto do arquivo de destino, com o nome."),
            "sobrescrever": bool_prop("Opcional. true substitui o destino que já existe."),
        },
        ["origem", "destino"],
    )

    def touched_paths(self, arguments: dict) -> list[Path]:
        found = []
        for key in ("origem", "destino"):
            try:
                found.append(resolve_path(arguments.get(key)))
            except ValueError:
                continue
        return found

    def _paths(self, arguments: dict) -> tuple[Path, Path]:
        source = resolve_path(arguments.get("origem"))
        target = resolve_path(arguments.get("destino"))
        if not source.is_file():
            raise ValueError("A origem precisa ser um arquivo que exista. Pastas não são movidas nem copiadas por aqui.")
        if target.is_dir():
            target = target / source.name
        if os.path.normcase(str(source)) == os.path.normcase(str(target)):
            raise ValueError("Origem e destino são o mesmo arquivo.")
        if target.exists() and not arguments.get("sobrescrever"):
            raise ValueError("O destino já existe. Peça de novo com sobrescrever=true, se for isso que o usuário quer.")
        if source.stat().st_size > undo.MAX_FILE_BYTES:
            raise ValueError(_NO_UNDO)
        return source, target

    def preview(self, arguments: dict) -> dict:
        source, target = self._paths(arguments)
        detail = f"De: {source}\nPara: {target}"
        if target.exists():
            detail += "\nO destino atual será substituído; a versão dele fica guardada para desfazer."
        note = risk_note(target)
        if note:
            detail = note + "\n" + detail
        return {"title": f"{self.verb.capitalize()} arquivo", "summary": f"{source} → {target}", "detail": detail}

    def _remember(self, path: Path, action: str, group: str):
        backup = undo.remember(path, action, group)
        if path.exists() and backup is None:
            raise ValueError(_NO_UNDO)
        return backup


@tool
class MoveFile(_Transfer):
    name = "mover_arquivo"
    order = 82
    verb = "mover"
    description = (
        "Move ou renomeia um arquivo. Pede autorização e guarda o estado anterior; desfazer_plano com o plano devolvido volta tudo. "
        "Retorno: {origem, destino, plano}."
    )

    def run(self, arguments: dict) -> dict:
        source, target = self._paths(arguments)
        group = _transfer_group(self, source, target)
        self._remember(target, "mover_arquivo", group)
        self._remember(source, "mover_arquivo", group)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        return {"origem": str(source), "destino": str(target), "plano": group, "pode_desfazer": True}


@tool
class CopyFile(_Transfer):
    name = "copiar_arquivo"
    order = 84
    verb = "copiar"
    description = (
        "Copia um arquivo para outro caminho. Pede autorização; o destino substituído fica guardado para desfazer. "
        "Retorno: {origem, destino, plano}."
    )

    def touched_paths(self, arguments: dict) -> list[Path]:
        try:
            return [resolve_path(arguments.get("destino"))]
        except ValueError:
            return []

    def run(self, arguments: dict) -> dict:
        source, target = self._paths(arguments)
        group = _transfer_group(self, target)
        self._remember(target, "copiar_arquivo", group)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return {"origem": str(source), "destino": str(target), "plano": group, "pode_desfazer": True}


def _transfer_group(tool, *paths: Path) -> str:
    """O plano aprovado, se cobre todos os caminhos; senão, um grupo próprio para desfazer_plano."""
    import secrets

    plans = {_plan_of(tool, path) for path in paths}
    if len(plans) == 1 and None not in plans:
        return plans.pop()
    return "op-" + secrets.token_hex(4)


def decode_text(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as error:
        if error.start >= len(data) - 3:
            return data[: error.start].decode("utf-8"), "utf-8"
    return data.decode("cp1252", errors="replace"), "cp1252"


def _guard_delete(path: Path) -> None:
    if not path.exists():
        raise ValueError("O caminho não existe.")
    if path.parent == path:
        raise ValueError("A raiz de um disco não pode ser apagada.")
    protected = [os.environ.get("WINDIR", r"C:\Windows"), os.environ.get("USERPROFILE", ""), os.environ.get("PROGRAMFILES", "")]
    for folder in protected:
        if folder and os.path.normcase(str(path)) == os.path.normcase(os.path.abspath(folder)):
            raise ValueError("Essa pasta do sistema não pode ser apagada pelo OllamaPilot.")
    if not path.is_file() and not path.is_dir():
        raise ValueError("Não foi possível apagar esse caminho.")


def _count_items(path: Path) -> int:
    count = 0
    for _ in path.rglob("*"):
        count += 1
        if count > MAX_DELETE_ITEMS:
            raise ValueError(f"A pasta tem mais de {MAX_DELETE_ITEMS} itens. Escolha uma pasta menor.")
    return count


def _read_for_diff(path: Path, limit: int) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return decode_text(data)[0]


def _diff(old: str, new: str, path: Path, limit: int = 6000) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{path.name} (atual)",
            tofile=f"{path.name} (novo)",
            lineterm="",
            n=2,
        )
    )
    if not lines:
        return "(sem diferença de conteúdo)"
    text = "\n".join(lines)
    shown, cut = clip(text, limit)
    return shown + ("\n… diff truncado" if cut else "")


def _line_of(content: str, search: str) -> int:
    index = content.find(search)
    return content.count("\n", 0, index) + 1 if index >= 0 else 0


_PLACEHOLDER = re.compile(
    r"(?i)(\.\.\.|resto do (código|codigo|arquivo)|código (inalterado|omitido)|unchanged|rest of (the )?(file|code))"
)


def _refuse_office(path: Path) -> None:
    if path.suffix.lower() in {".pdf", ".docx", ".xlsx", ".zip"}:
        raise ValueError(
            "PDF, DOCX, XLSX e ZIP não são texto puro. "
            "Chame criar_arquivo com nome, caminho e documento."
        )


def _too_short(old: str, new: str) -> bool:
    old_lines = old.count("\n") + 1
    new_lines = new.count("\n") + 1
    if old_lines < 40:
        return False
    if new_lines < old_lines * 0.6:
        return True
    return bool(new_lines < old_lines and _PLACEHOLDER.search(new))


def _numbered(lines: list[str], first: int) -> str:
    return "\n".join(f"{number:>6}|{line}" for number, line in enumerate(lines, first))


_NO_UNDO = (
    "A alteração foi cancelada porque não há como guardar a versão anterior para desfazer. "
    "O arquivo passa de 20 MB ou a cópia cifrada não pôde ser criada."
)


def _keep_undo(tool, path: Path, action: str):
    backup = undo.remember(path, action, _plan_of(tool, path))
    if path.exists() and backup is None:
        raise ValueError(_NO_UNDO)
    return backup


def _plan_of(tool, path: Path) -> str | None:
    agent = getattr(getattr(tool, "ctx", None), "agent", None)
    plan = getattr(agent, "plan_id", "") if agent is not None else ""
    approved = getattr(agent, "approved_plan", ()) if agent is not None else ()
    if plan and str(path).lower() in approved:
        return plan
    return None
