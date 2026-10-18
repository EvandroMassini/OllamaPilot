from __future__ import annotations

import ctypes
import json
import os
import time

from ollamapilot.tools.base import Tool, int_prop, schema, text_prop, tool


@tool
class ListProcesses(Tool):
    name = "listar_processos"
    group = "programas"
    order = 130
    description = (
        "Lista processos em execução: pid, nome e executável. Use para saber se um programa está rodando; "
        "filtro opcional pelo nome. Retorno: {processos (até 120), total, truncated}."
    )
    parameters = schema({"filtro": text_prop("Opcional. Trecho do nome do processo, como chrome.")}, [])

    def run(self, arguments: dict) -> dict:
        try:
            import psutil
        except ImportError as error:
            raise ValueError("Instale psutil para listar processos.") from error
        needle = str(arguments.get("filtro") or "").strip().lower()
        rows = []
        for process in psutil.process_iter(["pid", "name", "exe"]):
            info = process.info
            name = info.get("name") or ""
            if needle and needle not in name.lower():
                continue
            rows.append({"pid": info.get("pid"), "nome": name, "executavel": info.get("exe") or ""})
        rows.sort(key=lambda item: str(item["nome"]).lower())
        return {"processos": rows[:120], "total": len(rows), "truncated": len(rows) > 120}


@tool
class ListWindows(Tool):
    name = "listar_janelas"
    group = "janelas"
    repeatable = True
    order = 140
    description = (
        "Lista as janelas visíveis nesta área de trabalho, com título e pid. Use antes de acionar_janela para pegar o título exato. "
        "Retorno: {janelas: [{titulo, pid}]}, até 80."
    )
    parameters = schema({}, [])

    def run(self, arguments: dict) -> dict:
        rows = list_windows()
        return {"janelas": rows, "truncated": len(rows) >= 80}


@tool
class DriveWindow(Tool):
    name = "acionar_janela"
    group = "janelas"
    order = 150
    scope_label = "agir sobre qualquer janela"
    description = (
        "Age sobre uma janela e devolve os textos visíveis. Pede autorização. Ações em ordem, até 24: "
        "teclas, clicar (controle pelo nome ou x e y relativos à janela), digitar (no campo nome), ler, focar. Sem ações, só lê. "
        "Escolha a janela pelo pid devolvido por executar_programa ou listar_janelas, ou por um trecho do título. "
        "Retorno: {janela, acoes, falhas, textos, visor}."
    )
    sensitive = True
    parameters = schema(
        {
            "titulo": text_prop("Trecho do título, como devolvido por listar_janelas."),
            "pid": int_prop("Opcional. Processo dono da janela; tem prioridade sobre o título."),
            "acoes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "acao": {"type": "string", "enum": ["teclas", "digitar", "clicar", "ler", "focar"]},
                        "nome": {"type": "string"},
                        "texto": {"type": "string"},
                        "x": {"type": "integer", "description": "Coordenada horizontal relativa à janela."},
                        "y": {"type": "integer", "description": "Coordenada vertical relativa à janela."},
                    },
                    "required": ["acao"],
                },
            },
        },
        [],
    )

    def preview(self, arguments: dict) -> dict:
        title, pid = _target(arguments)
        actions = coerce_actions(arguments)
        lines = [f"Janela: {title or ''}{f' (pid {pid})' if pid else ''}".strip()]
        if not actions:
            lines.append("Ação: ler os textos visíveis.")
        else:
            for action in actions[:12]:
                if not isinstance(action, dict):
                    continue
                point = ""
                if action.get("x") is not None and action.get("y") is not None:
                    point = f" ponto ({action.get('x')}, {action.get('y')})"
                button = action.get("nome") or ("clique na coordenada" if point else "(janela)")
                lines.append(f"{_action_kind(action)}: {button}{point} {action.get('texto') or ''}".rstrip())
        return {"title": "Usar janela", "summary": title or f"pid {pid}", "detail": "\n".join(lines)}

    def run(self, arguments: dict) -> dict:
        title, pid = _target(arguments)
        try:
            from pywinauto import Desktop
        except ImportError as error:
            raise ValueError("Instale pywinauto para alimentar janelas.") from error
        actions = coerce_actions(arguments)
        window = _wait_window(Desktop(backend="uia"), title, 8, pid)
        if not _identified(window, title, pid):
            raise ValueError("A janela encontrada não é a que o pid ou o título identificou. O clique não foi feito.")
        notes = []
        for action in actions:
            try:
                if not _identified(window, title, pid):
                    raise ValueError("A janela não é a que o pid ou o título identificou. O clique não foi feito.")
                notes.append(_apply(window, action, title, pid))
            except Exception as error:
                notes.append({"acao": str(action.get("acao") or ""), "erro": str(error)})
        texts = _visible_texts(window)
        failed = sum(1 for note in notes if note.get("erro"))
        result = {"janela": _safe_text(window), "acoes": notes, "falhas": failed, "textos": texts}
        display = _display_value(texts)
        if display:
            result["visor"] = display
        return result


def coerce_actions(arguments: dict) -> list:
    """Aceita a lista de ações mesmo quando o modelo acentua o nome ou manda JSON em texto."""
    raw = None
    for key in ("acoes", "ações", "actions"):
        if arguments.get(key) not in (None, ""):
            raw = arguments[key]
            break
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if text[:1] in "[{":
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as error:
                raise ValueError("A lista de ações não é um JSON válido.") from error
        else:
            return [{"acao": "teclas", "texto": text}]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("As ações precisam ser uma lista.")
    if len(raw) > 24:
        raise ValueError("São ações demais para uma chamada. Envie até 24.")
    for index, action in enumerate(raw, start=1):
        if not isinstance(action, dict):
            raise ValueError(f"A ação {index} precisa ser um objeto com acao, nome e texto.")
        kind = _action_kind(action)
        if kind not in _ACTION_KINDS:
            raise ValueError(f"A ação {index} ('{kind}') é desconhecida. Use teclas, digitar, clicar, ler ou focar.")
    return raw


_ACTION_KINDS = {"teclas", "digitar", "clicar", "ler", "focar"}


def _action_kind(action: dict) -> str:
    return str(action.get("acao") or action.get("ação") or action.get("action") or "ler").strip().lower()


_CALC_KEYS = {
    "0": ("Zero",),
    "1": ("Um", "One"),
    "2": ("Dois", "Two"),
    "3": ("Três", "Three"),
    "4": ("Quatro", "Four"),
    "5": ("Cinco", "Five"),
    "6": ("Seis", "Six"),
    "7": ("Sete", "Seven"),
    "8": ("Oito", "Eight"),
    "9": ("Nove", "Nine"),
    "+": ("Mais", "Plus"),
    "-": ("Menos", "Minus"),
    "*": ("Multiplicar por", "Multiply by"),
    "x": ("Multiplicar por", "Multiply by"),
    "X": ("Multiplicar por", "Multiply by"),
    "×": ("Multiplicar por", "Multiply by"),
    "/": ("Dividir por", "Divide by"),
    "=": ("Igual a", "Equals"),
    ".": ("Separador decimal", "Decimal separator"),
    ",": ("Separador decimal", "Decimal separator"),
    "%": ("Por cento", "Percent"),
}


def calculator_button_names(text: str) -> list[str] | None:
    """Traduz uma conta simples para os botões da Calculadora. Devolve None se houver outro caractere."""
    raw = text.strip()
    names: list[str] = []
    if raw.upper().startswith("{ESC}"):
        names.append("Limpar")
        raw = raw[5:].strip()
    compact = "".join(raw.split())
    if not compact:
        return names or None
    for char in compact:
        aliases = _CALC_KEYS.get(char)
        if not aliases:
            return None
        names.append(aliases[0])
    return names or None


def to_sendkeys(text: str) -> str:
    """Converte texto comum para a sintaxe do teclado, preservando + e outros sinais."""
    if "{" in text or "}" in text:
        return text
    special = {
        "+": "{+}",
        "^": "{^}",
        "%": "{%}",
        "~": "{~}",
        "(": "{(}",
        ")": "{)}",
    }
    return "".join(special.get(char, char) for char in text)


def _safe_text(control) -> str:
    try:
        return str(control.window_text())[:500]
    except Exception as error:
        return f"(não foi possível ler: {error})"


def _target(arguments: dict) -> tuple[str, int | None]:
    title = str(arguments.get("titulo") or "").strip()
    raw = arguments.get("pid")
    pid = None
    if raw not in (None, "", 0):
        try:
            pid = int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("O pid precisa ser um número inteiro.") from error
    if not title and pid is None:
        raise ValueError("Informe o título ou o pid da janela.")
    if pid == os.getpid():
        raise ValueError("Essa é a janela do próprio OllamaPilot.")
    return title, pid


def _window_pid(window) -> int | None:
    try:
        return int(window.process_id())
    except Exception:
        return None


def _wait_window(desktop, title: str, timeout: float, pid: int | None = None):
    """Pelo pid, quando informado; pelo título, nunca a janela do próprio OllamaPilot."""
    deadline = time.time() + timeout
    needle = title.lower()
    own = os.getpid()
    while True:
        fallback = None
        for window in desktop.windows():
            owner = _window_pid(window)
            if owner == own:
                continue
            name = _safe_text(window)
            if pid is not None:
                if owner == pid:
                    if not needle or needle in name.lower():
                        return window
                    fallback = fallback or window
                continue
            if needle in name.lower():
                return window
        if fallback is not None:
            return fallback
        if time.time() >= deadline:
            raise ValueError("Nenhuma janela visível corresponde a esse pid ou título.")
        time.sleep(0.25)


def _focus(window) -> None:
    try:
        window.set_focus()
    except Exception:
        try:
            window.restore()
            window.set_focus()
        except Exception:
            pass


def _descendants(window, control_type: str = ""):
    try:
        if control_type:
            return window.descendants(control_type=control_type)
    except Exception:
        pass
    rows = window.descendants()
    if not control_type:
        return rows
    matched = []
    for row in rows:
        try:
            if row.element_info.control_type == control_type:
                matched.append(row)
        except Exception:
            continue
    return matched


def _control(window, name: str):
    if not name:
        return window
    needle = name.lower()
    fuzzy = None
    for descendant in _descendants(window):
        title = _safe_text(descendant)
        if title == name:
            return descendant
        if needle in title.lower() and fuzzy is None:
            fuzzy = descendant
    if fuzzy is not None:
        return fuzzy
    raise ValueError(f"Controle não encontrado: {name}")


def _type_text(target, text: str) -> str:
    if hasattr(target, "set_edit_text"):
        try:
            target.set_edit_text(text)
            return "campo"
        except Exception:
            pass
    try:
        target.iface_value.SetValue(text)
        return "valor"
    except Exception:
        pass
    _focus(target)
    from pywinauto.keyboard import send_keys

    send_keys(to_sendkeys(text), pause=0.06, with_spaces=True)
    return "teclado"


def _click(target) -> None:
    try:
        target.invoke()
        return
    except Exception:
        pass
    target.click_input()


def _display_value(texts: list[str]) -> str:
    markers = ("exibição é ", "display is ")
    for text in texts:
        lower = text.lower()
        for marker in markers:
            index = lower.find(marker)
            if index >= 0:
                return text[index + len(marker) :].strip()
    return ""


def _force_foreground(window) -> None:
    _foreground_hwnd(int(window.handle))


def _foreground_hwnd(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.ShowWindow(hwnd, 9)
    foreground = user32.GetForegroundWindow()
    if foreground == hwnd:
        return
    current = kernel32.GetCurrentThreadId()
    target = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached: list[int] = []
    try:
        if foreground_thread and foreground_thread != current:
            user32.AttachThreadInput(current, foreground_thread, True)
            attached.append(foreground_thread)
        if target and target not in (current, foreground_thread):
            user32.AttachThreadInput(current, target, True)
            attached.append(target)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for thread in attached:
            user32.AttachThreadInput(current, thread, False)
    if user32.GetForegroundWindow() != hwnd:
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)
        time.sleep(0.05)


def _buttons_by_name(window) -> dict[str, object]:
    found: dict[str, object] = {}
    for button in _descendants(window, "Button"):
        found.setdefault(_safe_text(button), button)
    return found


def _calculator_press(window, text: str) -> list[str] | None:
    title = _safe_text(window).lower()
    if "calcul" not in title or calculator_button_names(text) is None:
        return None
    raw = text.strip()
    planned: list[tuple[str, ...]] = []
    if raw.upper().startswith("{ESC}"):
        planned.append(("Limpar", "Clear"))
        raw = raw[5:].strip()
    for char in "".join(raw.split()):
        aliases = _CALC_KEYS.get(char)
        if not aliases:
            return None
        planned.append(aliases)
    buttons = _buttons_by_name(window)
    targets = []
    for names in planned:
        match = next((name for name in names if name in buttons), None)
        if match is None:
            return None
        targets.append((match, buttons[match]))
    pressed = []
    for name, target in targets:
        _click(target)
        pressed.append(name)
        time.sleep(0.12)
    time.sleep(0.25)
    return pressed


def _identified(window, title: str, pid: int | None) -> bool:
    owner = _window_pid(window)
    if pid is not None and owner != pid:
        return False
    if title and title.lower() not in _safe_text(window).lower():
        return False
    return bool(title or pid is not None)


def _click_at(window, x: int, y: int) -> None:
    rect = window.rectangle()
    if x < 0 or y < 0 or rect.left + x > rect.right or rect.top + y > rect.bottom:
        raise ValueError("O ponto está fora da janela identificada. O clique não foi feito.")
    window.click_input(coords=(int(x), int(y)))


def _apply(window, action: dict, title: str = "", pid: int | None = None) -> dict:
    kind = _action_kind(action)
    control_name = str(action.get("nome") or "").strip()
    text = str(action.get("texto") or "")
    if kind in {"teclas", "focar"}:
        _focus(window)
    if kind == "focar":
        return {"acao": kind}
    if kind == "teclas":
        pressed = _calculator_press(window, text)
        if pressed is not None:
            return {"acao": kind, "texto": text, "modo": "botoes", "botoes": pressed}
        _force_foreground(window)
        from pywinauto.keyboard import send_keys

        send_keys(to_sendkeys(text), pause=0.08, with_spaces=True)
        time.sleep(0.35)
        return {"acao": kind, "texto": text, "modo": "teclado"}
    target = _control(window, control_name)
    if kind == "digitar":
        modo = _type_text(target, text)
        time.sleep(0.2)
        return {"acao": kind, "modo": modo}
    if kind == "clicar":
        if action.get("x") is not None and action.get("y") is not None:
            if not _identified(window, title, pid):
                raise ValueError("A janela não é a que o pid ou o título identificou. O clique não foi feito.")
            _click_at(window, int(action["x"]), int(action["y"]))
            time.sleep(0.2)
            return {"acao": kind, "x": int(action["x"]), "y": int(action["y"]), "nome": control_name}
        _click(target)
        time.sleep(0.2)
        return {"acao": kind, "nome": control_name}
    if kind == "ler":
        return {"acao": kind, "nome": control_name or "janela", "texto": _safe_text(target)}
    raise ValueError("Ação desconhecida. Use teclas, digitar, clicar, ler ou focar.")


def _visible_texts(window, limit: int = 60) -> list[str]:
    found: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in found:
            found.append(value[:300])

    add(_safe_text(window))
    try:
        for descendant in window.descendants():
            add(_safe_text(descendant))
            if len(found) >= limit:
                break
    except Exception:
        pass
    return found


def list_windows(limit: int = 80, with_handle: bool = False) -> list[dict]:
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    rows: list[dict] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = buffer.value.strip()
        if title:
            row = {"titulo": title, "pid": int(pid.value)}
            if with_handle:
                row["hwnd"] = int(hwnd)
            rows.append(row)
        return len(rows) < limit

    user32.EnumWindows(enum_proc(callback), 0)
    return rows


@tool
class CaptureScreen(Tool):
    name = "capturar_tela"
    group = "janelas"
    repeatable = True
    order = 145
    scope_label = "capturar a tela"
    description = (
        "Tira uma captura de uma janela (por pid ou trecho do título) ou da tela inteira e entrega a imagem ao modelo. "
        "Só serve a modelo com visão. Pede autorização. Retorno: {janela, largura, altura}."
    )
    sensitive = True
    parameters = schema(
        {
            "titulo": text_prop("Opcional. Trecho do título da janela."),
            "pid": int_prop("Opcional. Processo dono da janela."),
        },
        [],
    )

    def preview(self, arguments: dict) -> dict:
        title = str(arguments.get("titulo") or "").strip()
        pid = arguments.get("pid")
        target = title or (f"pid {pid}" if pid else "tela inteira")
        return {
            "title": "Capturar tela",
            "summary": target,
            "detail": f"Alvo: {target}\nA imagem vai para o modelo atual. Confira se não há nada sigiloso visível.",
        }

    def run(self, arguments: dict) -> dict:
        agent = getattr(getattr(self, "ctx", None), "agent", None)
        caps = getattr(agent, "_caps", {}) or {}
        if caps.get("conhecido") and not caps.get("visao"):
            raise ValueError("O modelo atual não tem visão. Escolha um modelo com visão para usar capturas.")
        try:
            from PIL import ImageGrab
        except ImportError as error:
            raise ValueError("Instale Pillow para capturar a tela.") from error
        title = str(arguments.get("titulo") or "").strip()
        pid = None
        if arguments.get("pid") not in (None, "", 0):
            pid = int(arguments["pid"])
        label = "tela inteira"
        bbox = None
        if title or pid is not None:
            row = _find_window(title, pid)
            label = row["titulo"]
            _foreground_hwnd(row["hwnd"])
            time.sleep(0.3)
            bbox = _window_rect(row["hwnd"])
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        image.thumbnail((1600, 1600))
        import base64
        import io

        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG", optimize=True)
        return {
            "janela": label,
            "largura": image.width,
            "altura": image.height,
            "mensagem": "A captura foi anexada como imagem na próxima mensagem.",
            "_imagem": base64.b64encode(buffer.getvalue()).decode("ascii"),
        }


def _find_window(title: str, pid: int | None) -> dict:
    own = os.getpid()
    needle = title.lower()
    fallback = None
    for row in list_windows(400, with_handle=True):
        if row["pid"] == own:
            continue
        if pid is not None:
            if row["pid"] != pid:
                continue
            if not needle or needle in row["titulo"].lower():
                return row
            fallback = fallback or row
        elif needle in row["titulo"].lower():
            return row
    if fallback is not None:
        return fallback
    raise ValueError("Nenhuma janela visível corresponde a esse pid ou título.")


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = (ctypes.c_long * 4)()
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect))
    except Exception:
        pass
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        raise ValueError("A janela está minimizada ou sem área visível.")
    return rect[0], rect[1], rect[2], rect[3]

