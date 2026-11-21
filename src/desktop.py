"""Desktop shell: local API server in a thread + a native window (pywebview / WebView2 on Windows).

If the native window cannot start (no WebView2 runtime, no GUI backend) the app opens the
system browser instead and keeps serving until «Завершить» is pressed in the header.
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from .core import paths

log = logging.getLogger("editmaker")


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


class JsApi:
    """Methods exposed to the page as window.pywebview.api.*"""

    def __init__(self):
        self.window = None

    def pick_folder(self, start: str = ""):
        import webview
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=start or "")
        return res[0] if res else None

    def pick_file(self, kind: str = "png", start: str = ""):
        import webview
        types = {
            "png": ("Изображение PNG (*.png)",),
            "track": ("Аудио (*.mp3;*.wav;*.flac;*.m4a;*.aac;*.ogg;*.opus)",),
        }.get(kind, ("Все файлы (*.*)",))
        res = self.window.create_file_dialog(webview.OPEN_DIALOG, directory=start or "", file_types=types)
        return res[0] if res else None

    def open_external(self, url: str):
        """OAuth must run in the system browser: Google blocks embedded web views."""
        if url.startswith(("http://127.0.0.1", "https://")):
            webbrowser.open(url)
        return True


def _setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(data_dir / "editmaker.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("check", "selftest"):
        # console helpers stay available in the frozen build: EditMaker.exe selftest C:\\temp\\test
        # A windowed .exe has no stdout: mirror the output into <data>/cli.log.
        from .cli import main as cli_main
        if sys.stdout is None:
            log_path = paths.default_data_dir() / "cli.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            sys.stdout = sys.stderr = open(log_path, "w", encoding="utf-8")
        return cli_main(argv)

    import uvicorn

    from .core.config import Settings
    from .server import create_app

    data_dir = paths.default_data_dir()
    _setup_logging(data_dir)
    settings = Settings(data_dir)
    port = _free_port(int(settings.get()["server"]["port"]))
    if port != settings.get()["server"]["port"]:
        settings.update({"server": {"port": port}})
    app = create_app(data_dir)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/"
    log.info("server on %s, data %s", url, data_dir)

    try:
        import webview

        api = JsApi()
        window = webview.create_window("EditMaker", url, js_api=api, width=1480, height=940,
                                       min_size=(1100, 700), background_color="#15181C")
        api.window = window
        webview.start(gui="edgechromium" if sys.platform.startswith("win") else None, private_mode=False)
        server.should_exit = True
    except Exception:  # noqa: BLE001
        log.error("native window failed, falling back to browser:\n%s", traceback.format_exc())
        webbrowser.open(url + "?browser=1")
        while th.is_alive():
            th.join(0.5)
    th.join(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
