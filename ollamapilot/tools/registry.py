from __future__ import annotations

from ollamapilot.config import Settings
from ollamapilot.ollama_client import OllamaClient
from ollamapilot.tools.base import Tool, ToolContext, _PROVIDERS

# Grupos sempre enviados ao modelo. Os outros entram quando o pedido fala deles ou já foram usados na conversa.
CORE_GROUPS = frozenset({"arquivos", "programas", "git", "leitura", "continuidade"})


def discover() -> None:
    """Importação escrita por extenso: o executável não inclui módulo carregado só pelo nome."""
    import ollamapilot.tools.artifacts  # noqa: F401
    import ollamapilot.tools.binaryinfo  # noqa: F401
    import ollamapilot.tools.continuity  # noqa: F401
    import ollamapilot.tools.database  # noqa: F401
    import ollamapilot.tools.desktop  # noqa: F401
    import ollamapilot.tools.filesystem  # noqa: F401
    import ollamapilot.tools.gittools  # noqa: F401
    import ollamapilot.tools.ghidra_tool  # noqa: F401
    import ollamapilot.tools.knowledge  # noqa: F401
    import ollamapilot.tools.patching  # noqa: F401
    import ollamapilot.tools.planning  # noqa: F401
    import ollamapilot.tools.programs  # noqa: F401
    import ollamapilot.tools.project  # noqa: F401
    import ollamapilot.tools.search  # noqa: F401
    import ollamapilot.tools.sessions  # noqa: F401
    import ollamapilot.tools.shell  # noqa: F401
    import ollamapilot.tools.webtools  # noqa: F401


class Registry:
    """Todas as ferramentas podem ser chamadas; só as visíveis vão no pedido ao modelo."""

    def __init__(self, tools: list[Tool], visible: set[str] | None = None) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._visible = None if visible is None else {name for name in visible if name in self._tools}

    def _shown(self) -> list[Tool]:
        if self._visible is None:
            return list(self._tools.values())
        return [tool for name, tool in self._tools.items() if name in self._visible]

    def names(self) -> list[str]:
        return [tool.name for tool in self._shown()]

    def all_names(self) -> list[str]:
        return list(self._tools)

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"Ferramenta desconhecida: {name}") from error

    def hints(self) -> list[str]:
        return [tool.prompt_hint for tool in self._shown() if getattr(tool, "prompt_hint", "")]

    def definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._shown()
        ]


def build_registry(settings: Settings, client: OllamaClient, cancel, agent=None, groups: set[str] | None = None) -> Registry:
    """groups None envia todas as ferramentas; senão, só as dos grupos pedidos e as de MCP."""
    discover()
    ctx = ToolContext(settings, client, cancel, agent)
    ordered = sorted(_PROVIDERS.values(), key=lambda cls: (cls.order, cls.name))
    tools = [cls.create(ctx) for cls in ordered if cls.available(ctx)]
    if getattr(settings, "mcp_enabled", False):
        from ollamapilot.mcp import MANAGER

        extra = MANAGER.tools(settings, cancel)
        for item in extra:
            item.ctx = ctx
            item.group = "mcp"
        tools.extend(extra)
    visible = None
    if groups is not None:
        wanted = set(groups) | CORE_GROUPS | {"mcp"}
        visible = {tool.name for tool in tools if getattr(tool, "group", "arquivos") in wanted}
    return Registry(tools, visible)
