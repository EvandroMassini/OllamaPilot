"""Consulta o índice do projeto antes de abrir arquivos um a um."""

from __future__ import annotations

from ollamapilot.config import Settings
from ollamapilot.ollama_client import OllamaClient
from ollamapilot.project_index import find_symbols, index_project, map_project, search_project
from ollamapilot.tools.base import Tool, int_prop, schema, text_prop, tool
from ollamapilot.tools.paths import resolve_path


def _root(arguments: dict):
    return resolve_path(arguments.get("caminho"))


@tool
class IndexProject(Tool):
    name = "indexar_projeto"
    group = "projeto"
    order = 180
    sensitive = True
    scope_label = "indexar o código de um projeto"
    prompt_hint = (
        "Se o usuário nomeou um arquivo, leia esse arquivo. "
        "Se a pergunta for sobre o projeto e nenhum arquivo foi nomeado, busque no índice; "
        "se o índice não existir, chame indexar_projeto, que pede autorização. "
        "O índice usa o modelo de embeddings local e não envia o projeto para a nuvem."
    )
    description = (
        "Indexa o código de uma pasta neste computador, pulando segredos, dependências e o que o .gitignore ignora. "
        "Pede autorização. A busca seguinte usa esse índice. Retorno: {raiz, arquivos, trechos}."
    )
    parameters = schema({"caminho": text_prop("Pasta raiz do projeto, como E:\\Projetos\\site.")}, ["caminho"])

    def __init__(self, settings: Settings, client: OllamaClient) -> None:
        self.settings = settings
        self.client = client

    def preview(self, arguments: dict) -> dict:
        path = _root(arguments)
        return {
            "title": "Indexar projeto",
            "summary": str(path),
            "detail": (
                f"{path}\n\nLê o código-fonte localmente e grava um índice em storage. "
                "Arquivos de senha e pastas como node_modules ficam de fora. "
                f"Embeddings pelo modelo local {self.settings.embed_model or 'embeddinggemma'}."
            ),
        }

    def run(self, arguments: dict) -> dict:
        root = _root(arguments)

        def embed(texts: list[str]):
            ctx = getattr(self, "ctx", None)
            if ctx is not None and ctx.cancel():
                raise ValueError("A indexação foi interrompida.")
            return self.client.embed(texts)

        report = index_project(root, embed)
        if report.get("busca_vetorial") is False:
            model = self.settings.embed_model or "embeddinggemma"
            report["mensagem"] = f"A busca vetorial está desligada. Falta o modelo {model} no servidor."
            report["modelo"] = model
        return report


@tool
class SearchProject(Tool):
    name = "buscar_no_projeto"
    group = "projeto"
    order = 190
    description = (
        "Busca no índice do projeto por palavras e por significado. Use antes de ler arquivos inteiros. "
        "Se o índice não existir, diga para indexar. Retorno: {trechos: [{arquivo, inicio, fim, simbolo, trecho}]}."
    )
    parameters = schema(
        {
            "caminho": text_prop("Pasta raiz já indexada."),
            "consulta": text_prop("O que procurar: nome de função, arquivo ou assunto."),
            "limite": int_prop("Opcional. De 1 a 8. Padrão 6."),
        },
        ["caminho", "consulta"],
    )

    def __init__(self, settings: Settings, client: OllamaClient) -> None:
        self.settings = settings
        self.client = client

    def run(self, arguments: dict) -> dict:
        root = _root(arguments)
        query = str(arguments.get("consulta") or "").strip()
        if not query:
            raise ValueError("Informe a consulta.")
        limit = min(max(int(arguments.get("limite") or 6), 1), 8)
        vector = None
        offline = ""
        try:
            vector = self.client.embed([query])[0]
        except Exception:
            model = self.settings.embed_model or "embeddinggemma"
            offline = f"A busca vetorial está desligada. Falta o modelo {model} no servidor."
        rows = search_project(root, query, vector, limit)
        if not rows and not offline:
            return {"trechos": [], "mensagem": "Nada no índice. Chame indexar_projeto nessa pasta, se ainda não fez."}
        result = {"trechos": rows}
        if offline:
            result["busca_vetorial"] = False
            result["mensagem"] = offline if rows else offline + " Nada no índice para essa consulta."
        return result


@tool
class MapProject(Tool):
    name = "mapa_do_projeto"
    group = "projeto"
    order = 200
    description = (
        "Lista arquivos indexados e os símbolos de topo (funções e classes Python). "
        "Retorno: {raiz, arquivos: [{arquivo, simbolos}]}."
    )
    parameters = schema({"caminho": text_prop("Pasta raiz já indexada.")}, ["caminho"])

    def run(self, arguments: dict) -> dict:
        return map_project(_root(arguments))


@tool
class FindSymbol(Tool):
    name = "buscar_simbolo"
    group = "projeto"
    order = 205
    description = (
        "Procura uma função, classe ou método pelo nome e devolve arquivo e linha. "
        "Não usa embedding. Retorno: {simbolos: [{arquivo, linha, simbolo}]}."
    )
    parameters = schema(
        {
            "nome": text_prop("Nome, ou parte do nome, da função, classe ou método."),
            "caminho": text_prop("Pasta em que procurar, como E:\\Projetos\\site."),
        },
        ["nome", "caminho"],
    )

    def run(self, arguments: dict) -> dict:
        name = str(arguments.get("nome") or "").strip()
        if not name:
            raise ValueError("Informe o nome do símbolo.")
        rows = find_symbols(_root(arguments), name)
        return {"simbolos": rows, "total": len(rows)}
