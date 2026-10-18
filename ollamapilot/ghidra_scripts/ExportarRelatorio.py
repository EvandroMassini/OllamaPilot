# -*- coding: utf-8 -*-
# Relatório de análise estática para o OllamaPilot.
# Roda dentro do Ghidra (Jython ou o Python de script do analyzeHeadless).
# @category OllamaPilot

import json

from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor
from java.io import OutputStreamWriter, FileOutputStream
from java.nio.charset import StandardCharsets


def arg(args, index, default=""):
    try:
        size = len(args)
    except Exception:
        size = args.length
    if index >= size:
        return default
    return str(args[index])


def text_of(value):
    if value is None:
        return ""
    return str(value)


def write_json(path, payload):
    raw = json.dumps(payload, ensure_ascii=True, indent=2)
    stream = FileOutputStream(path)
    writer = OutputStreamWriter(stream, StandardCharsets.UTF_8)
    writer.write(raw)
    writer.close()


def collect_blocks(program):
    blocks = []
    try:
        for block in program.getMemory().getBlocks():
            blocks.append({
                "nome": text_of(block.getName()),
                "inicio": text_of(block.getStart()),
                "tamanho": int(block.getSize()),
                "executavel": bool(block.isExecute()),
            })
    except Exception as error:
        blocks.append({"erro": text_of(error)})
    return blocks


def collect_imports(program):
    imports = []
    try:
        symbols = program.getSymbolTable().getExternalSymbols()
        while symbols.hasNext() and len(imports) < 250:
            symbol = symbols.next()
            imports.append({
                "nome": text_of(symbol.getName()),
                "biblioteca": text_of(symbol.getParentNamespace()),
            })
    except Exception as error:
        imports.append({"erro": text_of(error)})
    return imports


def collect_strings(program):
    found = []
    try:
        data = program.getListing().getDefinedData(True)
        while data.hasNext() and len(found) < 120:
            item = data.next()
            if item.hasStringValue():
                found.append(text_of(item.getDefaultValueRepresentation())[:240])
    except Exception as error:
        found.append(text_of(error))
    return found


def decompile(program, max_funcs, only):
    functions = []
    decompiler = DecompInterface()
    decompiler.openProgram(program)
    monitor = ConsoleTaskMonitor()
    total = 0
    try:
        total = int(program.getFunctionManager().getFunctionCount())
    except Exception:
        total = 0
    iterator = program.getFunctionManager().getFunctions(True)
    used = 0
    chars = 0
    while iterator.hasNext():
        function = iterator.next()
        try:
            if function.isThunk() or function.isExternal():
                continue
        except Exception:
            pass
        name = text_of(function.getName())
        if only and name != only:
            continue
        if not only and used >= max_funcs:
            break
        entry = {
            "nome": name,
            "entrada": text_of(function.getEntryPoint()),
            "assinatura": "",
        }
        try:
            entry["assinatura"] = text_of(function.getSignature().getPrototypeString())
        except Exception:
            pass
        try:
            result = decompiler.decompileFunction(function, 20, monitor)
            if result is not None and result.decompileCompleted():
                code = text_of(result.getDecompiledFunction().getC())
                if len(code) > 4000:
                    code = code[:4000] + "\n/* trecho cortado */"
                if chars + len(code) > 50000:
                    entry["pseudocodigo"] = ""
                    entry["cortado"] = True
                    functions.append(entry)
                    break
                entry["pseudocodigo"] = code
                chars += len(code)
        except Exception as error:
            entry["erro"] = text_of(error)
        functions.append(entry)
        used += 1
        if only:
            break
    try:
        decompiler.dispose()
    except Exception:
        pass
    return total, functions


def main():
    args = getScriptArgs()
    output = arg(args, 0, "")
    if not output:
        print("Informe o caminho do JSON.")
        return
    try:
        max_funcs = int(arg(args, 1, "15"))
    except Exception:
        max_funcs = 15
    if max_funcs < 1:
        max_funcs = 1
    if max_funcs > 40:
        max_funcs = 40
    only = arg(args, 2, "")
    if only == "-":
        only = ""
    program = currentProgram
    language = ""
    compiler = ""
    try:
        language = text_of(program.getLanguage().getLanguageDescription().getLanguageID())
    except Exception:
        pass
    try:
        compiler = text_of(program.getCompilerSpec().getCompilerSpecID())
    except Exception:
        pass
    total, functions = decompile(program, max_funcs, only)
    report = {
        "programa": text_of(program.getName()),
        "executavel": text_of(program.getExecutablePath()),
        "linguagem": language,
        "compilador": compiler,
        "formato": text_of(program.getExecutableFormat()),
        "funcoes_total": total,
        "blocos": collect_blocks(program),
        "imports": collect_imports(program),
        "strings": collect_strings(program),
        "funcoes": functions,
        "uso": (
            "Relatório para entender comportamento. "
            "Escreva uma especificação e um programa novo. "
            "Não copie este pseudocódigo para o projeto novo."
        ),
    }
    write_json(output, report)
    print("Relatório gravado.")


main()
