"""Entry point: GUI (default), --headless monitoring, --self-test, --smoke-test."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import time


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="PDFReplacementManager")
    ap.add_argument("--minimized", action="store_true", help="start in the system tray")
    ap.add_argument("--headless", action="store_true", help="monitor downloads without a window (Ctrl+C to stop)")
    ap.add_argument("--self-test", action="store_true", help="run the built-in end-to-end test and exit")
    ap.add_argument("--smoke-test", action="store_true", help="open and close the GUI (packaging check)")
    ap.add_argument("--data-dir", help="use a different data directory")
    ap.add_argument("--open", help="open this PDF in the editor")
    ap.add_argument("--watch", help="watch this folder (stored in settings)")
    ap.add_argument("--report", help="also write --self-test/--smoke-test results to this JSON file")
    args = ap.parse_args(argv)

    def report(obj):
        text = json.dumps(obj, indent=2)
        print(text)
        if args.report:
            with open(args.report, "w", encoding="utf-8") as fh:
                fh.write(text)
    if args.smoke_test and not args.data_dir:      # never touch real data or the real Downloads folder
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="prm-smoke-"))
        (tmp / "Downloads").mkdir()
        args.data_dir, args.watch = str(tmp / "data"), str(tmp / "Downloads")

    if args.self_test:
        from .selftest import run
        res = run()
        report(res)
        return 0 if res["ok"] else 1

    from .service import AppService
    if args.headless:
        svc = AppService(args.data_dir)
        if args.watch:
            svc.set_settings(watch_folders=[args.watch])
        svc.start()
        print(f"Monitoring {svc.settings()['watch_folders']} (data: {svc.paths.root}). Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        svc.stop()
        return 0

    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    app.setApplicationName("PDF Replacement Manager")
    app.setQuitOnLastWindowClosed(False)
    key = f"PDFReplacementManager-{getpass.getuser()}" + ("-smoke" if args.smoke_test else "")
    if not args.data_dir:
        sock = QLocalSocket()
        sock.connectToServer(key)
        if sock.waitForConnected(300):            # already running: bring it to front
            sock.write(b"show")
            sock.waitForBytesWritten(300)
            return 0
    server = QLocalServer()
    QLocalServer.removeServer(key)
    server.listen(key)

    from .ui.common import app_icon
    from .ui.main_window import MainWindow
    app.setWindowIcon(app_icon())
    svc = AppService(args.data_dir)
    if args.watch:
        svc.set_settings(watch_folders=[args.watch])
    win = MainWindow(svc)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.show_normal()))
    svc.start()
    if args.open:
        win.go("editor")
        win.pages["editor"].open_file(args.open)
    if not args.minimized:
        win.show()
    if args.smoke_test:
        from PySide6.QtCore import QTimer

        def finish():
            for key_ in ("dashboard", "profiles", "jobs", "settings", "diagnostics", "editor"):
                win.go(key_)
                app.processEvents()
            report({"ok": True, "pages": len(win.pages), "monitoring": svc.monitoring})
            win.quitting = True
            win.quit()
        QTimer.singleShot(1500, finish)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
