from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ollamapilot.config import Settings
from ollamapilot.paths import GHIDRA_SCRIPTS
from ollamapilot.process_run import run_process
from ollamapilot.tools.base import Tool, int_prop, schema, text_prop, tool
from ollamapilot.tools.paths import resolve_path


@tool
class AnalyzeWithGhidra(Tool):
    name = "ghidra_analisar"
    group = "reverso"
    order = 120
    scope_label = "analisar arquivos com o Ghidra"
    description = (
        "Analisa executável ou DLL no Ghidra (analyzeHeadless), sem executá-lo; pode levar minutos e pede autorização. "
        "Use depois de ler_binario e da confirmação de direito do usuário. "
        "Retorno enxuto: formato, imports agrupados por biblioteca e textos de comportamento do binário inteiro (SQL, telas, arquivos). "
        "Não devolve o pseudocódigo das primeiras funções. Para uma função, repita com nome_funcao. Serve para entender, não para copiar."
    )
    sensitive = True
    parameters = schema(
        {
            "caminho": text_prop("Caminho absoluto do executável ou biblioteca a analisar."),
            "max_funcoes": int_prop("Quantidade máxima de funções no relatório. Padrão 15, teto 40."),
            "nome_funcao": text_prop("Se preenchido, descompila só a função com esse nome."),
        },
        ["caminho"],
    )

    def __init__(self, settings: Settings, cancel) -> None:
        self.settings = settings
        self.cancel = cancel

    def preview(self, arguments: dict) -> dict:
        binary = resolve_path(arguments.get("caminho"))
        home = self._home()
        detail = (
            f"Arquivo: {binary}\n"
            f"Ghidra: {home}\n\n"
            "O analyzeHeadless vai importar o arquivo, analisar e gravar um relatório JSON. "
            "O programa analisado não é executado."
        )
        return {"title": "Analisar com o Ghidra", "summary": binary.name, "detail": detail}

    def run(self, arguments: dict) -> dict:
        binary = resolve_path(arguments.get("caminho"))
        if not binary.is_file():
            raise ValueError("O arquivo para análise não existe.")
        home = self._home()
        bat = home / "support" / "analyzeHeadless.bat"
        if not bat.is_file():
            raise ValueError(f"Não encontrei {bat}. Em Configurações, aponte a pasta raiz do Ghidra.")
        script = _report_script()
        max_funcs = int(arguments.get("max_funcoes") or 15)
        max_funcs = min(max(max_funcs, 1), 40)
        only = str(arguments.get("nome_funcao") or "").strip() or "-"
        if any(char in only for char in "\r\n\x00\"%"):
            raise ValueError("O nome da função tem um caractere que o Ghidra não recebe por aqui.")
        with tempfile.TemporaryDirectory(prefix="ollamapilot-ghidra-") as temp:
            project = Path(temp) / "projeto"
            project.mkdir()
            report = Path(temp) / "relatorio.json"
            log = Path(temp) / "ghidra.log"
            launcher = Path(temp) / "rodar.bat"
            launcher.write_text(
                build_bat(bat, project, binary, script, report, log, max_funcs, only),
                encoding="cp1252",
                errors="replace",
            )
            result = run_process(
                [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(launcher)],
                temp,
                self.settings.ghidra_timeout,
                cancel=self.cancel,
                output_limit=40_000,
            )
            if result.get("cancelled"):
                return {"cancelado": True, "erro": "A análise foi interrompida; o Ghidra foi encerrado."}
            if result.get("timed_out"):
                raise ValueError(
                    f"O Ghidra não terminou em {self.settings.ghidra_timeout} s e foi encerrado. "
                    "Aumente o tempo do Ghidra em Configurações ou peça uma função específica."
                )
            if not report.is_file():
                log_text = ""
                if log.is_file():
                    log_text = log.read_text(encoding="utf-8", errors="replace")
                detail = failure_detail(result.get("stdout") or "", result.get("stderr") or "", log_text)
                raise ValueError(
                    "O Ghidra terminou sem gravar o relatório. "
                    f"Código {result.get('exit_code')}. {detail}".strip()
                )
            try:
                payload = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(f"O relatório do Ghidra está ilegível: {error}. Código {result.get('exit_code')}.") from error
            if not isinstance(payload, dict):
                raise ValueError("O relatório do Ghidra não tem o formato esperado.")
        saved = _store_report(binary, payload)
        summary = _summarize(payload)
        summary["caminho_relatorio"] = str(saved)
        summary["arquivo"] = str(binary)
        summary["exit_code"] = result.get("exit_code")
        summary["duracao_ms"] = result.get("duracao_ms")
        return summary

    def _home(self) -> Path:
        raw = self.settings.ghidra_home.strip()
        if not raw:
            raise ValueError("Configure a pasta do Ghidra em Configurações.")
        home = Path(raw)
        if not home.is_dir():
            raise ValueError("A pasta do Ghidra configurada não existe.")
        return home


def _report_script() -> Path:
    # O Ghidra 12 headless não tem Python. O .java compila dentro do analyzeHeadless;
    # o .py só entra se o Java não estiver na instalação.
    java = GHIDRA_SCRIPTS / "ExportarRelatorio.java"
    if java.is_file():
        return java
    python = GHIDRA_SCRIPTS / "ExportarRelatorio.py"
    if python.is_file():
        return python
    raise ValueError("O script de relatório do OllamaPilot não está na instalação.")


def failure_detail(stdout: str, stderr: str, log: str) -> str:
    """A causa útil costuma estar no meio da saída. O final do stderr é aviso do Java."""
    blob = "\n".join(part for part in (log, stdout, stderr) if part)
    markers = ("SCRIPT ERROR", "Python is not available", "Abort due to", "GhidraScriptLoadException")
    for line in blob.splitlines():
        if any(marker in line for marker in markers):
            return line.strip()[:700]
    tail = (stderr or stdout or log or "").strip()
    return tail[-800:]


def build_bat(bat: Path, project: Path, binary: Path, script: Path, report: Path, log: Path, max_funcs: int, only: str) -> str:
    args = [
        str(bat),
        str(project),
        "OllamaPilotAnalise",
        "-import",
        str(binary),
        "-analysisTimeoutPerFile",
        "600",
        "-scriptPath",
        str(script.parent),
        "-postScript",
        script.name,
        str(report),
        str(max_funcs),
        only,
        "-log",
        str(log),
        "-deleteProject",
    ]
    quoted = " ".join(_quote(part) for part in args)
    return "@echo off\r\ncall " + quoted + "\r\n"


def _quote(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


def _store_report(binary: Path, payload: dict) -> Path:
    from ollamapilot.paths import LOGS

    LOGS.mkdir(parents=True, exist_ok=True)
    target = LOGS / f"ghidra-{binary.stem}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


_NOISE = (
    "bogus ", "huffman", "lclstrconsts", "rtlconsts", "sysconst", "dbconst", "abresstring", "jpeg",
    "information_schema", "postgresql sql sample", "pg_database", "ingres example", "mysql sample",
    "sample source", "empty row", "cannot insert file", "duplicates stored name",
    "dataset not in edit", "not in edit or insert", "zeos",
)
_SQL = ("select ", "insert ", "update ", "delete ")
_BEHAVIOR = (
    "frm", "tform", ".fdb", ".bkp", ".jpg", "senha", "cadastro", "estoque", "fornecedor",
    "http://", "https://", "backup", "sysdba", "firebird", "informe ", "licenc", "acessou",
    "cnpj", "servidor", "logomarca", "validade", "lote", "sp_saida", "encerrar", "reabrir",
    "gwbminformatica", "status=2", "hostname",
)
_SUMMARY_BUDGET = 20_000


def _text_of(item) -> str:
    if isinstance(item, dict):
        return str(item.get("texto") or item.get("nome") or "").strip().strip('"')
    return str(item or "").strip().strip('"')


def _is_noise(text: str) -> bool:
    low = text.lower()
    return any(token in low for token in _NOISE)


def _rank(text: str) -> tuple:
    # 0 é SQL, 1 é tela, arquivo ou mensagem, 2 é o resto. O SQL vem primeiro na ordenação,
    # mas _fit_texts coloca o grupo 1 no JSON antes dele: o corte de 20 mil caracteres
    # fica no SQL genérico, não no nome da tela.
    low = text.lower()
    if any(token in low for token in _SQL):
        return (0, text)
    if any(token in low for token in _BEHAVIOR):
        return (1, text)
    return (2, text)


def _behavior_texts(payload: dict) -> list[str]:
    raw = payload.get("sinais") or payload.get("strings") or []
    texts = []
    seen = set()
    for item in raw:
        text = _text_of(item)
        if len(text) < 6 or text in seen or _is_noise(text):  # 6 caracteres preserva SYSDBA
            continue
        seen.add(text)
        texts.append(text)
    texts.sort(key=_rank)
    if any(_rank(text)[0] < 2 for text in texts):
        texts = [text for text in texts if _rank(text)[0] < 2]
    return texts


def _import_summary(imports) -> list[dict]:
    if isinstance(imports, dict):
        rows = []
        for item in imports.get("bibliotecas") or []:
            if not isinstance(item, dict):
                continue
            rows.append({
                "biblioteca": item.get("biblioteca"),
                "quantidade": item.get("quantidade"),
                "amostra": (item.get("amostra") or [])[:8],
            })
        return rows
    counts: dict[str, int] = {}
    samples: dict[str, list] = {}
    order: list[str] = []
    for item in imports or []:
        if not isinstance(item, dict):
            continue
        library = str(item.get("biblioteca") or "?")
        if library not in counts:
            counts[library] = 0
            samples[library] = []
            order.append(library)
        counts[library] += 1
        if len(samples[library]) < 8 and item.get("nome"):
            samples[library].append(item.get("nome"))
    return [{"biblioteca": library, "quantidade": counts[library], "amostra": samples[library]} for library in order]


def _function_summary(functions) -> list[dict]:
    rows = []
    items = [item for item in functions or [] if isinstance(item, dict)]
    for item in items[:20]:
        row = {"nome": item.get("nome"), "entrada": item.get("entrada")}
        code = str(item.get("pseudocodigo") or "")
        # A primeira análise não pede função. Pseudocódigo só volta quando a lista
        # tem no máximo três funções; uma lista grande seria o startup do compilador.
        if code and len(items) <= 3:
            row["pseudocodigo"] = code[:1500]
        rows.append(row)
    return rows


def _screen_key(text: str) -> tuple:
    low = text.lower()
    important = (
        "acesso", "senha", "sysdba", ".fdb", "http", "licenc", "cnpj", "logomarca",
        "sp_saida", "encerrar", "reabrir", "validade", "backup", "servidor",
    )
    return (0 if any(token in low for token in important) else 1, low)


def _fit_texts(summary: dict, texts: list[str]) -> list[str]:
    """Telas, arquivos e mensagens do programa entram antes do SQL."""
    screens = sorted((text for text in texts if _rank(text)[0] == 1), key=_screen_key)
    queries = [text for text in texts if _rank(text)[0] == 0]
    others = [text for text in texts if _rank(text)[0] > 1]
    chosen: list[str] = []
    for text in screens:
        summary["textos"] = chosen + [text]
        if len(json.dumps(summary, ensure_ascii=False)) > _SUMMARY_BUDGET:
            summary["textos"] = chosen
            return chosen
        chosen.append(text)
    for text in queries + others:
        summary["textos"] = chosen + [text]
        if len(json.dumps(summary, ensure_ascii=False)) > _SUMMARY_BUDGET:
            break
        chosen.append(text)
    summary["textos"] = chosen
    return chosen


def _summarize(payload: dict) -> dict:
    summary = {
        "programa": payload.get("programa"),
        "linguagem": payload.get("linguagem"),
        "compilador": payload.get("compilador"),
        "formato": payload.get("formato"),
        "funcoes_total": payload.get("funcoes_total"),
        "imports": _import_summary(payload.get("imports")),
        "blocos": payload.get("blocos") or [],
        "textos": [],
        "funcoes": _function_summary(payload.get("funcoes")),
        "observacao": (
            "textos são do programa, já sem exemplo de biblioteca. "
            "Se aparecer .fdb, SYSDBA ou Firebird, o banco é esse; não descreva outro motor. "
            "Inclua login, configuração (empresa, documento, servidor, logomarca), lote, validade, "
            "a procedure de saída, encerrar e reabrir, e o endereço de licença, quando esses textos estiverem aqui. "
            "Mensagem em inglês de componente não é regra de negócio. "
            "A primeira chamada não traz pseudocódigo. O JSON completo fica em caminho_relatorio."
        ),
        "uso": payload.get("uso"),
    }
    _fit_texts(summary, _behavior_texts(payload))
    return summary
