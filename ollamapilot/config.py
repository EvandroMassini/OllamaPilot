from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from ollamapilot import secrets as secret_store
from ollamapilot.paths import SETTINGS_FILE, ensure_storage, park_invalid, write_text_atomic

# Abaixo de 4.096 o prompt das ferramentas não entra. O controle sobe até
# 262.144, o contexto que os modelos locais daqui declaram, em passos de 4.096.
CONTEXT_MIN = 4096
CONTEXT_MAX = 262144
CONTEXT_STEP = 4096
LEGACY_CONTEXT = 32768
MODEL_SOURCES = ("local", "online")
PROVIDERS = ("ollama", "openai")
DB_ACCESS = ("desligado", "leitura", "escrita")
TOOL_SELECTIONS = ("auto", "todas")
SECRET_FIELDS = ("cloud_api_key", "web_api_key", "db_password", "openai_api_key")


@dataclass
class Settings:
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = ""
    model_remote: bool = False
    # "local" lista só os modelos baixados; "online" inclui os de ollama.com, se houver API Key.
    model_source: str = "local"
    cloud_api_key: str = ""
    temperature: float = 0.4
    num_ctx: int = 8192
    keep_alive: str = "30m"
    think: str = "auto"
    response_format: str = "text"
    format_schema: str = ""
    seed: int | None = None
    top_k: int = 40
    top_p: float = 0.9
    min_p: float = 0.0
    repeat_penalty: float = 1.1
    num_predict: int = -1
    embed_model: str = "embeddinggemma"
    max_history_messages: int = 40
    max_tool_rounds: int = 16
    command_timeout: int = 120
    ghidra_home: str = ""
    ghidra_timeout: int = 900
    web_enabled: bool = False
    web_api_key: str = ""
    db_enabled: bool = False
    # desligado | leitura | escrita. O nome em db_name é só o banco padrão e pode ficar vazio.
    db_access: str = "desligado"
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = ""
    db_user: str = "postgres"
    db_password: str = ""
    max_read_bytes: int = 200_000
    max_write_bytes: int = 1_000_000
    max_tool_chars: int = 24_000
    chat_idle_timeout: int = 600
    trace_enabled: bool = True
    mcp_enabled: bool = False
    programs: list = field(default_factory=list)
    # "ollama" usa o servidor Ollama (local ou ollama.com); "openai" usa um servidor compatível com a API da OpenAI.
    provider: str = "ollama"
    openai_url: str = "http://127.0.0.1:1234/v1"
    openai_api_key: str = ""
    # "auto" envia só os grupos de ferramentas do assunto do pedido; "todas" envia sempre todas.
    tool_selection: str = "auto"

    @classmethod
    def load(cls) -> "Settings":
        ensure_storage()
        if not SETTINGS_FILE.is_file():
            return cls()
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except OSError:
            return cls()
        except ValueError:
            park_invalid(SETTINGS_FILE)
            return cls()
        if not isinstance(raw, dict):
            park_invalid(SETTINGS_FILE)
            return cls()
        known = cls()
        defaults = asdict(known)
        values = {}
        for key, default in defaults.items():
            if key in raw:
                values[key] = _coerce(default, raw[key])
                if key in _RANGES and values[key] is not None:
                    low, high = _RANGES[key]
                    values[key] = min(max(values[key], low), high)
        if values.get("provider", "ollama") not in PROVIDERS:
            values["provider"] = "ollama"
        if values.get("tool_selection", "auto") not in TOOL_SELECTIONS:
            values["tool_selection"] = "auto"
        if values.get("model_source") not in MODEL_SOURCES:
            # Configuração anterior a esta opção: quem já usava um modelo online continua com a lista online.
            values["model_source"] = "online" if raw.get("model_remote") is True else "local"
        if "db_access" not in raw:
            values["db_access"] = "leitura" if raw.get("db_enabled") is True else "desligado"
        elif values.get("db_access") not in DB_ACCESS:
            values["db_access"] = "desligado"
        store = secret_store.current()
        migrated = False
        for key in SECRET_FIELDS:
            raw_value = raw.get(key)
            if isinstance(raw_value, str) and raw_value.strip():
                store.set(key, raw_value)
                values[key] = raw_value
                migrated = True
            else:
                stored = store.get(key)
                if stored:
                    values[key] = stored
        try:
            loaded = cls(**values)
        except TypeError:
            return known
        if migrated:
            try:
                loaded.save()
            except OSError:
                return loaded
        return loaded

    def save(self) -> None:
        ensure_storage()
        store = secret_store.current()
        for key in SECRET_FIELDS:
            value = str(getattr(self, key) or "")
            if value.strip():
                store.set(key, value)
            else:
                store.delete(key)
        payload = asdict(self)
        for key in SECRET_FIELDS:
            payload[key] = ""
        write_text_atomic(SETTINGS_FILE, json.dumps(payload, ensure_ascii=False, indent=2))

    def online_models(self) -> bool:
        """Modelos online só entram quando o usuário escolheu essa opção e cadastrou a API Key."""
        return self.model_source == "online" and bool(self.cloud_api_key.strip())

    def generation_options(self, *, remote: bool = False) -> dict:
        options = {
            "temperature": float(self.temperature),
            "num_ctx": int(self.num_ctx),
            "top_k": int(self.top_k),
            "top_p": float(self.top_p),
            "min_p": float(self.min_p),
            "repeat_penalty": float(self.repeat_penalty),
        }
        predict = int(self.num_predict)
        if not (remote and predict < 1):
            options["num_predict"] = predict
        if self.seed is not None and str(self.seed) != "":
            options["seed"] = int(self.seed)
        return options


# Faixas aceitas ao ler o JSON. Um valor fora delas vira o limite mais próximo.
_RANGES = {
    "temperature": (0.0, 2.0),
    "num_ctx": (2048, CONTEXT_MAX),
    "top_k": (0, 1000),
    "top_p": (0.0, 1.0),
    "min_p": (0.0, 1.0),
    "repeat_penalty": (0.0, 5.0),
    "num_predict": (-2, 131_072),
    "max_history_messages": (2, 1000),
    "max_tool_rounds": (1, 64),
    "command_timeout": (1, 3600),
    "ghidra_timeout": (30, 7200),
    "db_port": (1, 65535),
    "max_read_bytes": (1000, 20_000_000),
    "max_write_bytes": (1000, 50_000_000),
    "max_tool_chars": (1000, 400_000),
    "chat_idle_timeout": (30, 3600),
}


def db_access_mode(settings) -> str:
    """desligado, leitura ou escrita. Configuração antiga só com db_enabled vira leitura."""
    mode = str(getattr(settings, "db_access", "") or "")
    if mode in DB_ACCESS:
        return mode
    return "leitura" if getattr(settings, "db_enabled", False) else "desligado"


def _coerce(default, value):
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        if isinstance(value, bool):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, list):
        return normalize_programs(value)
    if default is None:
        if value is None or value == "" or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return default


def normalize_programs(value) -> list:
    """Lista de programas cadastrados: nome, caminho e os dois tempos, sem campos estranhos."""
    if not isinstance(value, list):
        return []
    clean = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("nome") or "").strip()
        path = str(item.get("caminho") or "").strip()
        if not name or not path:
            continue
        clean.append(
            {
                "nome": name[:80],
                "caminho": path[:500],
                "tempo_comando": _bound(item.get("tempo_comando"), 120, 1, 3600),
                "tempo_analise": _bound(item.get("tempo_analise"), 900, 30, 7200),
            }
        )
        if len(clean) == 40:
            break
    return clean


def effective_num_ctx(settings: Settings, caps: dict | None, *, persist: bool = False) -> int:
    """num_ctx enviado: o menor entre a configuração e o contexto declarado pelo modelo.

    Configuração ainda no teto antigo (32768), com modelo que declara mais, sobe para 65536 e grava.
    """
    configured = int(getattr(settings, "num_ctx", 8192) or 8192)
    declared = None
    if isinstance(caps, dict):
        raw = caps.get("contexto")
        if isinstance(raw, int) and raw > 0:
            declared = raw
    if configured == LEGACY_CONTEXT and declared is not None and declared > LEGACY_CONTEXT:
        configured = min(65536, declared, CONTEXT_MAX)
        if persist and int(settings.num_ctx) != configured:
            settings.num_ctx = configured
            try:
                settings.save()
            except OSError:
                pass
    limit = configured
    if declared is not None:
        limit = min(limit, declared)
    return max(1, min(int(limit), CONTEXT_MAX))


def _bound(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, low), high)
