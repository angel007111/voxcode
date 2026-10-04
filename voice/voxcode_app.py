"""Панель помощника: плавающее окно поверх остальных + голосовое ядро (listener.Core).

  pythonw voxcode_app.py      — запустить (повторный запуск просто покажет окно)

Порт LOCK_PORT — и замок от второго экземпляра, и управление панелью:
пустое подключение или «show» — показать окно, «quit» — закрыть панель, «ping» — ничего.
Так restart.ps1 и трей закрывают панель, даже если она запущена от администратора.
"""
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import webview

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from listener import Core, load_config, save_config, setup_logging  # noqa: E402
from assistant import NAME  # noqa: E402

LOCK_PORT = 8791
QUIT_FLAG = HERE.parent / "quit.flag"  # для voxcode-tray.ps1: «закрыть всё»
log = logging.getLogger("voxcode")
window = None
core = None


def push(ev):
    if window is not None:
        try:
            window.evaluate_js(f"window.voxcode && voxcode.ev({json.dumps(ev, ensure_ascii=False)})")
        except Exception:
            pass


class Api:
    def init(self):
        cfg = load_config()
        return {**core.settings(), "state": core.state, "on_top": cfg.get("on_top", True), "name": NAME}

    def send_text(self, text):
        text = (text or "").strip()
        if text:
            core.send(text, "text")

    def quick(self, cmd):
        core.send(cmd, "button")

    def choose(self, n, text):
        """Кнопка варианта из say.options."""
        core.send(f"Вариант {int(n)}: {text}", "choice")

    def ptt_start(self):
        core.ptt_start()

    def ptt_stop(self):
        core.ptt_stop()

    def stop_speaking(self):
        core.stop_speaking()

    def set_wake(self, on):
        core.set_wake(bool(on))

    def set_muted(self, on):
        core.set_muted(bool(on))

    def set_device(self, kind, name):
        core.set_device(kind, name)

    def set_mic(self, on):
        core.set_mic(bool(on))

    def set_sensitivity(self, gain, threshold):
        core.set_sensitivity(gain, threshold)

    def calibrate(self):
        core.calibrate()

    def set_dictation(self, on):
        core.set_dictation(bool(on))

    def finish_dictation(self):
        if core.dictating:
            core.finish_dictation()

    def cancel_dictation(self):
        if core.cancel_dictation():
            core.emit(type="info", text="Диктовку отменил, ничего не выполнял")
            core.set_state(core.idle_state())

    def set_on_top(self, on):
        cfg = load_config()
        cfg["on_top"] = bool(on)
        save_config(cfg)
        window.on_top = bool(on)

    def open_terminal(self):
        u = ctypes.windll.user32
        h = u.FindWindowW("CASCADIA_HOSTING_WINDOW_CLASS", "VoxCode Core")
        if h:
            u.ShowWindow(h, 9)
            u.SetForegroundWindow(h)
        else:
            core.start_session()

    def restart(self, what="all"):
        """Перезапуск через restart.ps1 отвязанным процессом: «all», «core» (сессия) или «panel».
        Панель при этом закроется сама — скрипт её и поднимет."""
        import subprocess
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                "-File", str(HERE.parent / "restart.ps1")]
        if what == "core":
            args.append("-Core")
        elif what == "panel":
            args.append("-Panel")
        log.info("панель: перезапуск (%s)", what)
        subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                         | subprocess.CREATE_NO_WINDOW, close_fds=True)

    def minimize(self):
        window.hide()  # «свернуть» = в трей: кнопки на панели задач у панели нет

    def hide(self):
        window.hide()

    def quit_all(self):
        """⏻ Закрыть всё: сессию Claude, трей и панель.
        Если трей жив — ставим quit.flag, он закроет терминал и пришлёт нам «quit»
        (сам терминал не трогаем: трей увидел бы закрытое окно и поднял сессию заново)."""
        log.info("панель: закрыть всё")
        import subprocess
        # сначала сжать контекст сессии (/compact), если он заполнен на 60%+ — до 3 мин, если помощник занят
        compact = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE.parent / "compact.ps1")]
        if subprocess.run(compact + ["-Check"], creationflags=subprocess.CREATE_NO_WINDOW).returncode == 10:
            push({"type": "info", "text": "Сжимаю контекст сессии перед выходом…"})
            subprocess.run(compact, creationflags=subprocess.CREATE_NO_WINDOW)
        u = ctypes.windll.user32
        tray = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Global\\VoxCodeTray")  # SYNCHRONIZE
        if tray:
            ctypes.windll.kernel32.CloseHandle(tray)
            QUIT_FLAG.touch()
            # трей проверяет флаг раз в 0.5 с, забирает его и шлёт нам «quit» (выход в lock_server);
            # не дождались — закрываем сами
            deadline = time.time() + 5
            while QUIT_FLAG.exists() and time.time() < deadline:
                time.sleep(0.1)
            if not QUIT_FLAG.exists():
                time.sleep(10)
        h = u.FindWindowW("CASCADIA_HOSTING_WINDOW_CLASS", "VoxCode Core")
        if h:
            u.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
        os._exit(0)


def lock_server(sock):
    """Второй запуск подключается к порту — показываем окно; «quit» — выходим."""
    sock.listen(1)
    while True:
        conn, _ = sock.accept()
        try:
            conn.settimeout(1)
            cmd = conn.recv(16).strip().lower()
        except OSError:
            cmd = b""
        conn.close()
        if cmd == b"quit":
            log.info("панель: закрытие по команде quit")
            try:
                window.destroy()
            except Exception:
                pass
            os._exit(0)  # потоки аудио и Whisper — daemon, но поток окна может держать процесс
        if cmd in (b"", b"show") and window is not None:
            window.show()
            window.restore()


def hide_from_taskbar():
    """Убрать панель с панели задач: окно процесса → WS_EX_TOOLWINDOW вместо WS_EX_APPWINDOW.
    Вернуть окно — значок помощника в трее."""
    u = ctypes.windll.user32
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    pid = os.getpid()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        p = ctypes.c_ulong()
        u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(p))
        if p.value == pid and u.IsWindowVisible(ctypes.c_void_p(h)) and not u.GetParent(ctypes.c_void_p(h)):
            found.append(h)
        return True

    u.EnumWindows(cb, 0)
    for h in found:
        h = ctypes.c_void_p(h)
        style = u.GetWindowLongW(h, GWL_EXSTYLE)
        u.ShowWindow(h, 0)  # стиль панели задач применяется при повторном показе
        u.SetWindowLongW(h, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        u.ShowWindow(h, 5)
    log.info("панель: убрана с панели задач (%d окон)", len(found))


def on_moved(x, y):
    cfg = load_config()
    cfg["x"], cfg["y"] = x, y
    save_config(cfg)


def main():
    global window, core
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        # уже запущено (панель или фоновый слушатель) — просим показать окно
        try:
            socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=2).close()
        except OSError:
            pass
        return
    setup_logging()
    (HERE / "panel.pid").write_text(str(os.getpid()))  # запасной путь для restart.ps1 / трея
    threading.Thread(target=lock_server, args=(sock,), daemon=True).start()

    cfg = load_config()
    core = Core(on_event=push)
    window = webview.create_window(
        NAME, str(HERE / "ui" / "index.html"), js_api=Api(),
        width=cfg.get("w", 380), height=cfg.get("h", 640), x=cfg.get("x"), y=cfg.get("y"),
        min_size=(320, 420), frameless=True, easy_drag=False, on_top=cfg.get("on_top", True),
        background_color="#0b0f17",
    )
    window.events.moved += on_moved
    window.events.shown += hide_from_taskbar

    def start_core():
        threading.Thread(target=core.run, kwargs={"with_levels": True}, daemon=True).start()

    webview.start(start_core, gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
