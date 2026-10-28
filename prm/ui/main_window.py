"""Main window: navigation, system tray, notifications."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QStackedWidget, QSystemTrayIcon, QWidget)

from .. import APP_NAME, __version__
from .common import STATE_LABELS, ServiceSignals, app_icon
from .editor import EditorWidget
from .pages import DashboardPage, DiagnosticsPage, JobsPage, ProfilesPage, SettingsPage

NAV = [("dashboard", "Dashboard"), ("profiles", "Profiles"), ("editor", "PDF editor"), ("jobs", "Queue & history"),
       ("settings", "Settings & backup"), ("diagnostics", "Logs & diagnostics")]


class MainWindow(QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.quitting = False
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1400, 900)
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setFixedWidth(190)
        self.nav.setStyleSheet("QListWidget { border:none; background: palette(window); padding-top:8px; }"
                               "QListWidget::item { padding:10px 14px; border-radius:6px; margin:2px 6px; }"
                               "QListWidget::item:selected { background: palette(highlight); "
                               "color: palette(highlighted-text); }")
        for key, label in NAV:
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, key)
            self.nav.addItem(it)
        lay.addWidget(self.nav)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.pages = {
            "dashboard": DashboardPage(service), "profiles": ProfilesPage(service), "editor": EditorWidget(service),
            "jobs": JobsPage(service), "settings": SettingsPage(service), "diagnostics": DiagnosticsPage(service)}
        for key, _ in NAV:
            w = self.pages[key]
            if key != "editor":
                w.setContentsMargins(16, 12, 16, 12)
            self.stack.addWidget(w)
        self.nav.currentRowChanged.connect(self._nav_changed)
        self.pages["dashboard"].navigate.connect(self.go)
        self.pages["profiles"].openInEditor.connect(self.open_profile_in_editor)
        self.pages["editor"].statusMessage.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.pages["editor"].profileSaved.connect(lambda _pid: self.pages["profiles"].refresh())

        self.signals = ServiceSignals()
        self.signals.changed.connect(self.refresh_current)
        self.signals.notify.connect(self._notify)
        service.add_change_listener(self.signals.changed.emit)
        service.add_notification_listener(lambda job, kind, msg: self.signals.notify.emit(job, kind, msg))

        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        menu.addAction("Open", self.show_normal)
        self.pause_action = QAction("Pause monitoring", self)
        self.pause_action.triggered.connect(self.toggle_monitoring)
        menu.addAction(self.pause_action)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_normal() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()
        self._tray_hint_shown = False

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_current)
        self.timer.start(2500)
        self.nav.setCurrentRow(0)

    def go(self, key: str):
        self.nav.setCurrentRow([k for k, _ in NAV].index(key))

    def _nav_changed(self, row):
        self.stack.setCurrentIndex(row)
        self.refresh_current()

    def refresh_current(self):
        w = self.stack.currentWidget()
        if w is not self.pages["editor"] and w is not self.pages["settings"] and hasattr(w, "refresh"):
            w.refresh()
        self.pause_action.setText("Pause monitoring" if self.service.monitoring else "Resume monitoring")

    def open_profile_in_editor(self, pid):
        self.go("editor")
        self.pages["editor"].open_profile(pid)

    def toggle_monitoring(self):
        self.pages["dashboard"].toggle_monitoring()

    def _notify(self, job, kind, message):
        if not self.service.settings()["notifications"]:
            return
        if job and job["profile_id"]:
            prof = self.service.repo.get_profile(job["profile_id"])
            if prof and not prof.options.get("notify", True):
                return
        icon = {"done": QSystemTrayIcon.Information, "dry_run": QSystemTrayIcon.Information}.get(
            kind, QSystemTrayIcon.Warning)
        self.tray.showMessage(f"{APP_NAME}: {STATE_LABELS.get(kind, kind)}", message[:250], icon, 8000)

    def show_normal(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if not self.quitting and self.service.settings()["minimize_to_tray"]:
            e.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self.tray.showMessage(APP_NAME, "Still monitoring downloads in the background. "
                                                "Right-click the tray icon to quit.", QSystemTrayIcon.Information,
                                      5000)
                self._tray_hint_shown = True
            return
        self.quit()
        e.accept()

    def quit(self):
        ed = self.pages["editor"]
        if ed.session and ed.session.dirty:
            r = QMessageBox.question(self, "Unsaved edits", "Editor changes are not saved to a profile (they are "
                                                            "autosaved and can be recovered later). Quit anyway?")
            if r != QMessageBox.Yes:
                return
        self.quitting = True
        self.statusBar().showMessage("Finishing the current job and stopping…")
        QApplication.processEvents()
        self.service.stop()
        self.tray.hide()
        QApplication.quit()
