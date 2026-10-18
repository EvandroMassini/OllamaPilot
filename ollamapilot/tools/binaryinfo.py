from __future__ import annotations

import hashlib
import math
import re
import zipfile
from pathlib import Path

from ollamapilot.tools.base import Tool, schema, text_prop, tool
from ollamapilot.tools.paths import is_secret, resolve_path

_PRINTABLE = re.compile(rb"[\x20-\x7e]{6,}")
_SAMPLE = 4 * 1024 * 1024


@tool
class ReadBinary(Tool):
    name = "ler_binario"
    group = "reverso"
    order = 30
    description = (
        "Inspeciona um arquivo binário sem executá-lo: tamanho, sha256, tipo e até 120 strings; "
        "se for PE, máquina, seções com entropia, imports e exports. Use para mapear um executável antes do Ghidra. "
        "Não use para PDF, DOCX ou XLSX: esses têm texto e se leem com ler_arquivo. "
        "Retorno: {caminho, bytes, sha256, tipo, strings, secoes, imports, exports, observacao}."
    )
    parameters = schema({"caminho": text_prop("Caminho absoluto do arquivo, como E:\\app\\programa.exe.")}, ["caminho"])

    def needs_approval(self, arguments: dict) -> bool:
        return is_secret(resolve_path(arguments.get("caminho")))

    def preview(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        return {"title": "Ler arquivo sensível", "summary": str(path), "detail": str(path)}

    def run(self, arguments: dict) -> dict:
        path = resolve_path(arguments.get("caminho"))
        if not path.is_file():
            raise ValueError("Informe um arquivo que exista.")
        size = path.stat().st_size
        digest = hashlib.sha256()
        head = b""
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                if len(head) < _SAMPLE:
                    head += block[: _SAMPLE - len(head)]
        report = {
            "caminho": str(path),
            "bytes": size,
            "sha256": digest.hexdigest(),
            "magic_hex": head[:16].hex(" "),
            "strings": _strings(head),
        }
        if head.startswith(b"MZ"):
            report.update(_pe(path))
        elif head.startswith(b"\x7fELF"):
            report["tipo"] = "ELF"
        elif head.startswith(b"PK") and zipfile.is_zipfile(path):
            report["tipo"] = "ZIP"
            report["entradas"] = _zip_names(path)
        elif head.startswith(b"%PDF"):
            report["tipo"] = "PDF"
        else:
            report["tipo"] = "binário"
        if path.suffix.lower() in {".pdf", ".docx", ".xlsx", ".xlsm"}:
            report["observacao"] = (
                "Este arquivo tem texto. Chame ler_arquivo com o mesmo caminho; as strings abaixo são metadados, não o conteúdo."
            )
        note = report.get("observacao")
        if isinstance(note, str):
            report["observacao"] = note
        return report


def _strings(data: bytes, limit: int = 120) -> list[str]:
    found = [match.decode("ascii", errors="ignore") for match in _PRINTABLE.findall(data)]
    found.sort(key=len, reverse=True)
    unique: list[str] = []
    seen = set()
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()[:80]


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    total = len(data)
    value = 0.0
    for count in counts:
        if count:
            probability = count / total
            value -= probability * math.log2(probability)
    return round(value, 2)


def _pe(path: Path) -> dict:
    try:
        import pefile
    except ImportError:
        return {"tipo": "PE", "observacao": "Instale pefile para ver imports e seções. O arquivo foi reconhecido como PE."}
    try:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"],
            ]
        )
    except Exception as error:
        return {"tipo": "PE", "observacao": f"O cabeçalho PE não pôde ser lido por completo: {error}"}
    machine = int(getattr(pe.FILE_HEADER, "Machine", 0))
    machines = {0x14C: "x86", 0x8664: "x64", 0xAA64: "ARM64"}
    subsystem = int(getattr(pe.OPTIONAL_HEADER, "Subsystem", 0))
    subsystems = {2: "janela", 3: "console"}
    com = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14]
    dotnet = int(com.VirtualAddress) != 0
    sections = []
    high_entropy = False
    for section in pe.sections:
        name = section.Name.rstrip(b"\x00").decode("ascii", errors="replace")
        sample = section.get_data()[: 256 * 1024]
        entropy = _entropy(sample)
        if entropy >= 7.2 and section.SizeOfRawData > 4096:
            high_entropy = True
        sections.append(
            {
                "nome": name,
                "virtual": int(section.Misc_VirtualSize),
                "raw": int(section.SizeOfRawData),
                "entropia": entropy,
            }
        )
    imports = []
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []:
        library = entry.dll.decode("ascii", errors="replace")
        symbols = []
        for item in entry.imports[:40]:
            if item.name:
                symbols.append(item.name.decode("ascii", errors="replace"))
        imports.append({"biblioteca": library, "simbolos": symbols})
        if len(imports) >= 80:
            break
    exports = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for item in pe.DIRECTORY_ENTRY_EXPORT.symbols[:80]:
            if item.name:
                exports.append(item.name.decode("ascii", errors="replace"))
    pe.close()
    report = {
        "tipo": "PE/.NET" if dotnet else "PE",
        "maquina": machines.get(machine, hex(machine)),
        "subsistema": subsystems.get(subsystem, str(subsystem)),
        "secoes": sections,
        "imports": imports,
        "exports": exports,
    }
    if dotnet:
        report["observacao"] = "O arquivo indica runtime .NET. O Ghidra mostra a estrutura; o comportamento gerenciado pode aparecer só em parte."
    elif high_entropy:
        report["observacao"] = (
            "Uma seção tem entropia alta. A análise estática pode ficar incompleta porque esse trecho está comprimido ou cifrado. "
            "Descreva o que for observável e registre o limite na especificação."
        )
    return report
