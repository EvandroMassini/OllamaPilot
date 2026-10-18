from __future__ import annotations

import re
from pathlib import Path

from ollamapilot.config import Settings, db_access_mode
from ollamapilot.tools.base import Tool, schema, text_prop, tool

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|call|do|execute|merge)\b",
    re.IGNORECASE,
)
_WRITE_START = re.compile(
    r"(?is)\s*(insert|update|delete|truncate|merge|create|alter|drop|grant|revoke|comment|reindex|vacuum|analyze|refresh|call)\b"
)
_SIDE_EFFECTS = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_sleep\w*|lo_import|lo_export|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|dblink\w*|set_config|nextval|setval|pg_advisory\w*)\s*\(",
    re.IGNORECASE,
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_PSQL_NAME = re.compile(r"(?i)(^|[\\/])(psql|createdb|dropdb|pg_restore)(\.exe)?$")
_PSQL_WORD = re.compile(r"(?i)\b(psql|createdb|dropdb|pg_restore)\b")
_SQL_WRITE = re.compile(r"(?i)\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge)\b")
_READ_ONLY_WRITE = (
    "O PostgreSQL está em somente leitura. Para criar, alterar ou apagar, "
    "escolha Leitura e escrita em Configurações. A escrita não rodou."
)


def _banco_prop() -> dict:
    return text_prop("Opcional. Banco desta chamada. Vazio usa o banco padrão das Configurações.")


class DatabaseTool(Tool):
    group = "banco"

    @classmethod
    def available(cls, ctx) -> bool:
        return db_access_mode(ctx.settings) in {"leitura", "escrita"}

    def needs_approval(self, arguments: dict) -> bool:
        if self.sensitive:
            return True
        return is_other_database(self.settings, arguments)

    def grant_key_for(self, arguments: dict) -> str:
        try:
            banco = _named_or_default(self.settings, arguments).lower()
        except ValueError:
            banco = ""
        if self.name == "banco_executar":
            sql = " ".join(str(arguments.get("sql") or "").split())
            return f"banco_escrita:{banco}:{sql[:180]}"
        if is_other_database(self.settings, arguments):
            return f"banco:{banco}"
        return "banco_leitura"


@tool
class DatabaseSummary(DatabaseTool):
    order = 220
    name = "banco_resumo"
    description = (
        "Lista as tabelas do PostgreSQL com a contagem estimada de linhas. "
        "banco vazio usa o banco padrão; outro nome pede confirmação. "
        "Retorno: {banco, tabelas, lista: [{schemaname, relname, n_live_tup}]}, até 200."
    )
    parameters = schema({"banco": _banco_prop()}, [])

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, arguments: dict) -> dict:
        return _read_preview(self.settings, arguments, "Ver tabelas")

    def run(self, arguments: dict) -> dict:
        banco = target_database(self.settings, arguments)
        sql = (
            "SELECT schemaname, relname, n_live_tup "
            "FROM pg_stat_user_tables ORDER BY n_live_tup DESC NULLS LAST"
        )
        rows = query(self.settings, sql, dbname=banco)
        return {"banco": banco, "tabelas": len(rows), "lista": rows[:200]}


@tool
class DatabaseStructure(DatabaseTool):
    order = 230
    name = "banco_estrutura"
    description = (
        "Mostra colunas e tipos das tabelas. Use antes de escrever um SELECT sobre tabela desconhecida. "
        "banco vazio usa o banco padrão; outro nome pede confirmação. "
        "Retorno: {banco, colunas: [{table_schema, table_name, column_name, data_type}]}, até 400."
    )
    parameters = schema(
        {
            "tabela": text_prop("Opcional. Nome de uma tabela para filtrar."),
            "banco": _banco_prop(),
        },
        [],
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, arguments: dict) -> dict:
        return _read_preview(self.settings, arguments, "Ver estrutura")

    def run(self, arguments: dict) -> dict:
        banco = target_database(self.settings, arguments)
        table = str(arguments.get("tabela") or "").strip()
        sql = (
            "SELECT table_schema, table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
        params = None
        if table:
            if not _IDENT.match(table):
                raise ValueError("Nome de tabela inválido.")
            sql += " AND table_name = %s"
            params = (table,)
        sql += " ORDER BY table_schema, table_name, ordinal_position"
        return {"banco": banco, "colunas": query(self.settings, sql, params, dbname=banco)[:400]}


@tool
class DatabaseQuery(DatabaseTool):
    order = 240
    name = "banco_consultar"
    description = (
        "Executa um único SELECT ou WITH numa transação somente leitura, com limite de 15 s. "
        "banco vazio usa o banco padrão; outro nome pede confirmação. "
        "Exemplo: SELECT nome, total FROM vendas ORDER BY total DESC LIMIT 10. "
        "Retorno: {banco, linhas_devolvidas, linhas (até 200), truncated}."
    )
    parameters = schema(
        {
            "sql": text_prop("Uma consulta SELECT ou WITH, sem ponto e vírgula no meio."),
            "banco": _banco_prop(),
        },
        ["sql"],
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, arguments: dict) -> dict:
        sql = guard_select(str(arguments.get("sql") or ""))
        shown = _read_preview(self.settings, arguments, "Consultar PostgreSQL")
        shown["detail"] += f"\n\n{sql}"
        shown["summary"] = sql[:160]
        return shown

    def run(self, arguments: dict) -> dict:
        banco = target_database(self.settings, arguments)
        sql = guard_select(str(arguments.get("sql") or ""))
        rows = query(self.settings, sql, dbname=banco)
        return {
            "banco": banco,
            "linhas_devolvidas": min(len(rows), 200),
            "linhas": rows[:200],
            "truncated": len(rows) > 200,
        }


@tool
class DatabaseCount(DatabaseTool):
    order = 250
    name = "banco_contar_registros_tabelas"
    description = (
        "Conta exatamente os registros de cada tabela (count(*)), até 80 tabelas; mais lento que banco_resumo. "
        "banco vazio usa o banco padrão; outro nome pede confirmação. "
        "Use só quando o usuário pedir contagem exata. Retorno: {banco, tabelas: [{schema, tabela, registros}], truncated}."
    )
    parameters = schema({"banco": _banco_prop()}, [])

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, arguments: dict) -> dict:
        return _read_preview(self.settings, arguments, "Contar registros")

    def run(self, arguments: dict) -> dict:
        banco = target_database(self.settings, arguments)
        tables = query(
            self.settings,
            "SELECT schemaname, relname FROM pg_stat_user_tables ORDER BY relname",
            dbname=banco,
        )
        counts = []
        for table in tables[:80]:
            schema_name = str(table["schemaname"])
            name = str(table["relname"])
            if not _IDENT.match(schema_name) or not _IDENT.match(name):
                continue
            counted = query(self.settings, f'SELECT count(*) AS total FROM "{schema_name}"."{name}"', dbname=banco)
            total = counted[0]["total"] if counted else None
            counts.append({"schema": schema_name, "tabela": name, "registros": total})
        return {"banco": banco, "tabelas": counts, "truncated": len(tables) > 80}


@tool
class DatabaseExecute(DatabaseTool):
    order = 260
    name = "banco_executar"
    sensitive = True
    scope_label = "gravar no PostgreSQL (criar, alterar ou apagar)"
    description = (
        "Executa um único comando de escrita no PostgreSQL: criar ou alterar tabela, coluna, view, trigger ou function, "
        "ou INSERT, UPDATE, DELETE e DROP. Só existe com Leitura e escrita nas Configurações. "
        "Pede confirmação e mostra o SQL e o banco. A senha não vai no argumento: usa a das Configurações. "
        "Retorno: {banco, comando, linhas_afetadas, linhas}."
    )
    parameters = schema(
        {
            "sql": text_prop("Um comando de escrita. Função pode usar $$ ... $$ com ponto e vírgula dentro."),
            "banco": _banco_prop(),
        },
        ["sql"],
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, arguments: dict) -> dict:
        _require_write(self.settings)
        banco = target_database(self.settings, arguments)
        sql = guard_write(str(arguments.get("sql") or ""))
        return {
            "title": "Gravar no PostgreSQL",
            "summary": f"{banco}: {sql[:140]}",
            "detail": (
                f"Banco: {banco}\n"
                f"Servidor: {self.settings.db_host}:{self.settings.db_port}\n"
                "Isto cria, altera ou apaga dados nesse banco.\n\n"
                f"{sql}"
            ),
        }

    def run(self, arguments: dict) -> dict:
        _require_write(self.settings)
        banco = target_database(self.settings, arguments)
        sql = guard_write(str(arguments.get("sql") or ""))
        return execute_sql(self.settings, sql, banco)


def guard_select(sql: str) -> str:
    text = _one_statement(sql)
    # Textos entre aspas não são comandos: WHERE status = 'delete' é leitura. A transação somente leitura é a trava real.
    cleaned = _mask(text)
    if not re.match(r"(?is)\s*(select|with)\b", cleaned):
        raise ValueError("Somente SELECT ou WITH.")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("Comando de escrita não é permitido.")
    if _SIDE_EFFECTS.search(cleaned):
        raise ValueError("Essa função tem efeito fora da consulta e não é permitida.")
    head = re.split(r"(?i)\bfrom\b", cleaned, maxsplit=1)[0]
    if re.search(r"(?i)\binto\b", head):
        raise ValueError("SELECT INTO cria tabela e não é permitido.")
    return text


def guard_write(sql: str) -> str:
    text = _one_statement(sql)
    cleaned = _mask(text)
    if re.match(r"(?is)\s*(select|with)\b", cleaned):
        raise ValueError("Consulta de leitura usa banco_consultar.")
    if not _WRITE_START.match(cleaned):
        raise ValueError(
            "Comando de escrita não reconhecido. Use CREATE, ALTER, DROP, INSERT, UPDATE, DELETE, TRUNCATE, GRANT ou CALL."
        )
    if _SIDE_EFFECTS.search(cleaned) or re.search(r"(?i)\bcopy\b", cleaned):
        raise ValueError("COPY e funções que leem arquivo ou outra sessão não são permitidas.")
    return text


def refuse_postgres_shell(settings, program: str, text: str) -> None:
    """psql e afins não escrevem enquanto Configurações estiver em somente leitura ou desligado."""
    if db_access_mode(settings) == "escrita":
        return
    blob = f"{program}\n{text}"
    name = Path(str(program or "")).name
    if not (_PSQL_NAME.search(name) or _PSQL_WORD.search(blob)):
        return
    writes = bool(_PSQL_NAME.search(name) and name.lower().startswith(("createdb", "dropdb", "pg_restore")))
    writes = writes or bool(_SQL_WRITE.search(text or ""))
    if writes:
        raise ValueError(_READ_ONLY_WRITE)


def target_database(settings: Settings, arguments: dict) -> str:
    return _named_or_default(settings, arguments)


def is_other_database(settings: Settings, arguments: dict) -> bool:
    asked = str(arguments.get("banco") or "").strip()
    if not asked:
        return False
    default = str(getattr(settings, "db_name", "") or "").strip()
    if not default:
        return True
    return asked.lower() != default.lower()


def query(settings: Settings, sql: str, params=None, *, dbname: str | None = None) -> list[dict]:
    banco = dbname or target_database(settings, {})
    cursor_rows, _affected = _run_sql(settings, sql, banco, params=params, read_only=True, limit_ms=15000)
    return cursor_rows


def execute_sql(settings: Settings, sql: str, dbname: str) -> dict:
    rows, affected = _run_sql(settings, sql, dbname, read_only=False, limit_ms=60000)
    return {
        "banco": dbname,
        "comando": sql.split(None, 1)[0].upper(),
        "linhas_afetadas": affected,
        "linhas": rows[:200],
        "truncated": len(rows) > 200,
    }


def _named_or_default(settings: Settings, arguments: dict) -> str:
    asked = str(arguments.get("banco") or "").strip()
    name = asked or str(getattr(settings, "db_name", "") or "").strip()
    if not name:
        raise ValueError("Informe o banco no argumento banco, ou preencha o banco padrão em Configurações.")
    if not _IDENT.match(name):
        raise ValueError("Nome de banco inválido. Use letras, números e sublinhado.")
    return name


def _require_write(settings: Settings) -> None:
    if db_access_mode(settings) != "escrita":
        raise ValueError(_READ_ONLY_WRITE)


def _read_preview(settings: Settings, arguments: dict, title: str) -> dict:
    banco = target_database(settings, arguments)
    return {
        "title": title,
        "summary": banco,
        "detail": (
            f"Banco: {banco}\n"
            f"Servidor: {settings.db_host}:{settings.db_port}\n"
            "Somente leitura. O banco padrão das Configurações não é este."
        ),
    }


def _run_sql(settings: Settings, sql: str, dbname: str, *, params=None, read_only: bool, limit_ms: int):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise ValueError("Instale psycopg para consultar o PostgreSQL.") from error
    try:
        with psycopg.connect(
            host=settings.db_host,
            port=int(settings.db_port),
            dbname=dbname,
            user=settings.db_user,
            password=settings.db_password,
            connect_timeout=8,
            row_factory=dict_row,
        ) as connection:
            connection.read_only = read_only
            connection.execute(f"SET statement_timeout = {int(limit_ms)}")
            cursor = connection.execute(sql, params)
            if cursor.description:
                fetched = cursor.fetchmany(201)
            else:
                fetched = []
            affected = cursor.rowcount
    except Exception as error:
        raise ValueError(f"O banco recusou a consulta: {error}") from error
    output = []
    for row in fetched:
        output.append({key: _cell(value) for key, value in dict(row).items()})
    return output, affected


def _cell(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _one_statement(sql: str) -> str:
    text = str(sql or "").strip()
    if not text:
        raise ValueError("Informe o comando.")
    if _has_second_statement(text):
        raise ValueError("Envie um único comando.")
    return text.rstrip(";").strip()


def _mask(text: str) -> str:
    # Apaga literais e corpos $$ ... $$ antes de procurar DELETE ou COPY.
    # Sem isso, um SELECT que cita a palavra delete dentro de aspas seria recusado.
    cleaned = re.sub(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$[\s\S]*?\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$[\s\S]*?\$\$", "''", text)
    cleaned = re.sub(r"'(?:[^']|'')*'", "''", cleaned)
    cleaned = re.sub(r'"(?:[^"]|"")*"', '""', cleaned)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"--.*?$", " ", cleaned, flags=re.M)
    return cleaned


def _has_second_statement(text: str) -> bool:
    """Ponto e vírgula no nível de cima, fora de aspas e de $$ ... $$."""
    i = 0
    n = len(text)
    saw = False
    while i < n:
        char = text[i]
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if char == "'":
            i += 1
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if char == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 1
            i += 1
            continue
        if char == "$":
            match = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", text[i:])
            if match:
                closer = "$" + (match.group(1) or "") + "$"
                end = text.find(closer, i + len(match.group(0)))
                i = n if end < 0 else end + len(closer)
                continue
        if char == ";":
            if text[i + 1 :].strip():
                return True
        i += 1
    return False
