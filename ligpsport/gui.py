"""Desktop GUI for ligpsport (Qt / PySide6).

A thin front-end: scan for bike computers, pick one, then download
recorded activities and upload / delete routes. All device work goes
through :class:`ligpsport.session.DeviceSession`; this module only
does layout and plumbing.

BLE runs on an asyncio loop in a background thread so the Qt event
loop never blocks. Results come back to the GUI thread through a
queued Qt signal (:class:`_Bridge`).

Run with ``ligpsport-gui`` (``pip install 'ligpsport[gui]'``), or
``ligpsport-gui --simulator`` to try it without hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import logging
import os
import sys
import threading
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import Future
from typing import Any, TypeVar

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__, commands, discovery
from .credentials import CredentialStore
from .session import DeviceSession, TransportFactory, default_transport_factory

T = TypeVar("T")

_LOG = logging.getLogger(__name__)

SCAN_SECONDS = 8.0
ROUTE_FILE_FILTER = "Routes (*.gpx *.geojson *.json *.cnx *.fit);;All files (*)"
ROUTE_SUFFIXES = (".gpx", ".geojson", ".json", ".cnx", ".fit")


# ---------- async plumbing ------------------------------------------------


class _Bridge(QtCore.QObject):
    """Runs callables on the GUI thread, posted from any thread."""

    invoke = QtCore.Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.invoke.connect(self._run, QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        fn()

    def post(self, fn: Callable[[], None]) -> None:
        self.invoke.emit(fn)


class AsyncRunner:
    """An asyncio loop on a daemon thread, with GUI-thread callbacks."""

    def __init__(self, bridge: _Bridge) -> None:
        self._bridge = bridge
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._main, name="ligpsport-asyncio", daemon=True)
        self._thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(
        self,
        coro: Coroutine[Any, Any, T],
        on_ok: Callable[[T], None],
        on_error: Callable[[BaseException], None],
    ) -> Future[T]:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def done(f: Future[T]) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                self._bridge.post(lambda: on_error(exc))
            else:
                result = f.result()
                self._bridge.post(lambda: on_ok(result))

        fut.add_done_callback(done)
        return fut

    def run_blocking(self, coro: Coroutine[Any, Any, T], timeout: float) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2.0)


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.1f} MiB"


def _icon(widget: QtWidgets.QWidget, sp: QtWidgets.QStyle.StandardPixmap) -> QtGui.QIcon:
    return widget.style().standardIcon(sp)


SP = QtWidgets.QStyle.StandardPixmap
ROLE = QtCore.Qt.ItemDataRole.UserRole


# ---------- device list ---------------------------------------------------


class DevicePanel(QtWidgets.QWidget):
    """Left pane: discovered bike computers plus a rescan button."""

    selected = QtCore.Signal(object)  # discovery.Device
    rescan = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        title = QtWidgets.QLabel("Bike computers")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        self.list = QtWidgets.QListWidget()
        self.list.setIconSize(QtCore.QSize(24, 24))
        self.list.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self.list, 1)

        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.rescan_button = QtWidgets.QPushButton(_icon(self, SP.SP_BrowserReload), "Rescan")
        self.rescan_button.clicked.connect(self.rescan)
        layout.addWidget(self.rescan_button)

        self.setMinimumWidth(230)
        self._labels: dict[str, str] = {}

    def set_known_names(self, labels: dict[str, str]) -> None:
        """Map upper-cased addresses to friendly names from the credential store."""
        self._labels = labels

    def title_for(self, dev: discovery.Device) -> str:
        friendly = self._labels.get(dev.address.upper())
        return f"{friendly} ({dev.name})" if friendly else dev.name

    def clear(self) -> None:
        self.list.clear()

    def add_device(self, dev: discovery.Device) -> None:
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(ROLE).address == dev.address:
                return
        item = QtWidgets.QListWidgetItem(
            _icon(self, SP.SP_DriveNetIcon), f"{self.title_for(dev)}\n{dev.address}"
        )
        item.setData(ROLE, dev)
        if dev.rssi is not None:
            item.setToolTip(f"Signal: {dev.rssi} dBm")
        self.list.addItem(item)

    def set_scanning(self, scanning: bool, message: str = "") -> None:
        self.rescan_button.setEnabled(not scanning)
        self.status.setText(message)

    def _on_selection(self) -> None:
        items = self.list.selectedItems()
        if items:
            self.selected.emit(items[0].data(ROLE))


# ---------- activities ----------------------------------------------------


class ActivitiesTab(QtWidgets.QWidget):
    download = QtCore.Signal(list)  # list[commands.ActivityFile]
    folder_changed = QtCore.Signal(str)

    def __init__(self, folder: str) -> None:
        super().__init__()
        self.folder = folder
        layout = QtWidgets.QVBoxLayout(self)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Date", "Size", "Downloaded"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self._download_selected())
        header = self.tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        layout.addWidget(self.tree, 1)

        folder_row = QtWidgets.QHBoxLayout()
        folder_row.addWidget(QtWidgets.QLabel("Save to:"))
        self.folder_label = QtWidgets.QLabel()
        self.folder_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        folder_row.addWidget(self.folder_label, 1)
        change = QtWidgets.QPushButton(_icon(self, SP.SP_DirOpenIcon), "Change…")
        change.clicked.connect(self._choose_folder)
        folder_row.addWidget(change)
        layout.addLayout(folder_row)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.new_button = QtWidgets.QPushButton("Download new")
        self.new_button.setToolTip("Download every activity not yet in the folder")
        self.new_button.clicked.connect(self._download_new)
        buttons.addWidget(self.new_button)
        self.download_button = QtWidgets.QPushButton(
            _icon(self, SP.SP_ArrowDown), "Download selected"
        )
        self.download_button.clicked.connect(self._download_selected)
        buttons.addWidget(self.download_button)
        layout.addLayout(buttons)

        self._activities: list[commands.ActivityFile] = []
        self._busy = False
        self._show_folder()

    def path_for(self, activity: commands.ActivityFile) -> str:
        return os.path.join(self.folder, activity.default_filename())

    def set_activities(self, activities: Sequence[commands.ActivityFile]) -> None:
        self._activities = sorted(activities, key=lambda r: r.timestamp, reverse=True)
        self.refresh_rows()

    def refresh_rows(self) -> None:
        self.tree.clear()
        for activity in self._activities:
            saved = os.path.exists(self.path_for(activity))
            item = QtWidgets.QTreeWidgetItem(
                [
                    f"{activity.started_at:%Y-%m-%d  %H:%M}  ({activity.started_at:%a})",
                    _human_size(activity.file_size),
                    "✓" if saved else "",
                ]
            )
            item.setData(0, ROLE, activity)
            item.setTextAlignment(1, QtCore.Qt.AlignmentFlag.AlignRight)
            item.setTextAlignment(2, QtCore.Qt.AlignmentFlag.AlignCenter)
            self.tree.addTopLevelItem(item)
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self) -> None:
        missing = any(not os.path.exists(self.path_for(r)) for r in self._activities)
        self.download_button.setEnabled(not self._busy and bool(self.tree.selectedItems()))
        self.new_button.setEnabled(not self._busy and missing)

    def _download_selected(self) -> None:
        if self._busy:
            return
        activities = [item.data(0, ROLE) for item in self.tree.selectedItems()]
        if activities:
            self.download.emit(activities)

    def _download_new(self) -> None:
        activities = [r for r in self._activities if not os.path.exists(self.path_for(r))]
        if activities:
            self.download.emit(activities)

    def _choose_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Save activities to", self.folder)
        if folder:
            self.folder = folder
            self._show_folder()
            self.refresh_rows()
            self.folder_changed.emit(folder)

    def _show_folder(self) -> None:
        self.folder_label.setText(QtCore.QDir.toNativeSeparators(self.folder))


# ---------- routes --------------------------------------------------------


class RouteTree(QtWidgets.QTreeWidget):
    """Route list that also accepts route files dropped from a file manager."""

    files_dropped = QtCore.Signal(list)  # list[str]

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)

    def _paths(self, event: QtGui.QDropEvent | QtGui.QDragEnterEvent) -> list[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [
            u.toLocalFile()
            for u in mime.urls()
            if u.isLocalFile() and u.toLocalFile().lower().endswith(ROUTE_SUFFIXES)
        ]

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if self._paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        paths = self._paths(event)
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


class RoutesTab(QtWidgets.QWidget):
    upload = QtCore.Signal(list, bool)  # paths, start navigation
    delete = QtCore.Signal(object)  # commands.RoutePlan

    def __init__(self, settings: QtCore.QSettings) -> None:
        super().__init__()
        self._settings = settings
        layout = QtWidgets.QVBoxLayout(self)

        self.tree = RouteTree()
        self.tree.setHeaderLabels(["Name", "Distance", "Type", ""])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.files_dropped.connect(self._emit_upload)
        header = self.tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        layout.addWidget(self.tree, 1)

        hint = QtWidgets.QLabel("Tip: drop GPX files onto the list to upload them.")
        hint.setEnabled(False)
        layout.addWidget(hint)

        buttons = QtWidgets.QHBoxLayout()
        self.delete_button = QtWidgets.QPushButton(_icon(self, SP.SP_TrashIcon), "Delete")
        self.delete_button.clicked.connect(self._delete_selected)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        self.start_check = QtWidgets.QCheckBox("Start navigation after upload")
        buttons.addWidget(self.start_check)
        self.upload_button = QtWidgets.QPushButton(_icon(self, SP.SP_ArrowUp), "Upload route…")
        self.upload_button.clicked.connect(self._choose_files)
        buttons.addWidget(self.upload_button)
        layout.addLayout(buttons)

        self._busy = False
        self._update_buttons()

    def set_routes(self, routes: Sequence[commands.RoutePlan]) -> None:
        self.tree.clear()
        for route in sorted(routes, key=lambda r: r.id):
            item = QtWidgets.QTreeWidgetItem(
                [
                    route.name,
                    f"{route.total_distance / 1000:.1f} km",
                    route.kind,
                    "Navigating" if route.is_active else "",
                ]
            )
            item.setData(0, ROLE, route)
            item.setToolTip(0, f"Slot {route.id}")
            item.setTextAlignment(1, QtCore.Qt.AlignmentFlag.AlignRight)
            if route.is_active:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            self.tree.addTopLevelItem(item)
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.tree.setAcceptDrops(not busy)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.upload_button.setEnabled(not self._busy)
        self.delete_button.setEnabled(not self._busy and bool(self.tree.selectedItems()))

    def _choose_files(self) -> None:
        start_dir = str(self._settings.value("route_dir", QtCore.QDir.homePath()))
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Upload routes", start_dir, ROUTE_FILE_FILTER
        )
        if paths:
            self._settings.setValue("route_dir", os.path.dirname(paths[0]))
            self._emit_upload(paths)

    def _emit_upload(self, paths: list[str]) -> None:
        if not self._busy:
            self.upload.emit(paths, self.start_check.isChecked())

    def _delete_selected(self) -> None:
        items = self.tree.selectedItems()
        if items:
            self.delete.emit(items[0].data(0, ROLE))


# ---------- main window ---------------------------------------------------


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        bridge: _Bridge,
        runner: AsyncRunner,
        transport_factory: TransportFactory,
        scan: Callable[[Callable[[discovery.Device], None]], Coroutine[Any, Any, None]],
    ) -> None:
        super().__init__()
        self._bridge = bridge
        self._runner = runner
        self._factory = transport_factory
        self._scan = scan
        self._session: DeviceSession | None = None
        self._scan_future: Future[None] | None = None
        self._device: discovery.Device | None = None
        self._settings = QtCore.QSettings()

        self.setWindowTitle("liGPSPORT")
        self.resize(900, 560)

        self.devices = DevicePanel()
        self.devices.selected.connect(self._select_device)
        self.devices.rescan.connect(self.start_scan)

        self.title = QtWidgets.QLabel()
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        self.title.setFont(font)
        self.subtitle = QtWidgets.QLabel()
        self.subtitle.setEnabled(False)
        self.refresh_button = QtWidgets.QPushButton(_icon(self, SP.SP_BrowserReload), "Refresh")
        self.refresh_button.clicked.connect(self._refresh)

        default_folder = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.DownloadLocation
        )
        folder = str(self._settings.value("download_dir", default_folder))
        self.activities = ActivitiesTab(folder)
        self.activities.download.connect(self._download)
        self.activities.folder_changed.connect(lambda f: self._settings.setValue("download_dir", f))
        self.routes = RoutesTab(self._settings)
        self.routes.upload.connect(self._upload)
        self.routes.delete.connect(self._delete_route)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.activities, "Activities")
        self.tabs.addTab(self.routes, "Routes")

        detail = QtWidgets.QWidget()
        detail_layout = QtWidgets.QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        head = QtWidgets.QHBoxLayout()
        titles = QtWidgets.QVBoxLayout()
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        head.addWidget(self.refresh_button, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        detail_layout.addLayout(head)
        detail_layout.addWidget(self.tabs, 1)

        placeholder = QtWidgets.QLabel("Select a bike computer on the left.")
        placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        placeholder.setEnabled(False)

        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(placeholder)
        self.stack.addWidget(detail)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.devices)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 640])
        splitter.setContentsMargins(8, 8, 8, 0)
        self.setCentralWidget(splitter)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)

        try:
            known = {c.address.upper(): c.name for c in CredentialStore().entries()}
        except (OSError, ValueError) as exc:
            _LOG.warning("could not read credential store: %s", exc)
            known = {}
        self.devices.set_known_names(known)

    # ----- scanning

    def start_scan(self) -> None:
        self.devices.clear()
        self.devices.set_scanning(True, "Scanning…")

        def found(dev: discovery.Device) -> None:
            self._bridge.post(lambda: self.devices.add_device(dev))

        def ok(_: None) -> None:
            n = self.devices.list.count()
            msg = "No bike computers found." if n == 0 else ""
            self.devices.set_scanning(False, msg)

        def err(exc: BaseException) -> None:
            self.devices.set_scanning(False, f"Scan failed: {_describe(exc)}")

        self._scan_future = self._runner.submit(self._scan(found), ok, err)

    def stop_scan(self) -> None:
        if self._scan_future is not None and not self._scan_future.done():
            # Connecting while scanning is unreliable on some BlueZ setups.
            self._scan_future.cancel()
            self.devices.set_scanning(False)

    # ----- device selection

    def _select_device(self, dev: discovery.Device) -> None:
        if self._device is not None and dev.address == self._device.address:
            return
        self.stop_scan()
        old = self._session
        self._device = dev
        self._session = DeviceSession(dev.address, transport_factory=self._factory)
        if old is not None:
            self._runner.submit(old.close(), lambda _: None, lambda _: None)
        self.title.setText(self.devices.title_for(dev))
        self.subtitle.setText(dev.address)
        self.subtitle.setToolTip("")
        self.activities.set_activities([])
        self.routes.set_routes([])
        self.stack.setCurrentIndex(1)
        self._refresh()

    def _show_link(self, session: DeviceSession) -> None:
        mtu = session.mtu
        if mtu is not None:
            self.subtitle.setText(f"{session.address}  ·  MTU {mtu}")
            self.subtitle.setToolTip(
                "ATT MTU of the BLE link. 23 means the large MTU was not negotiated "
                "and transfers will be slow."
            )

    def _refresh(self) -> None:
        session = self._session
        if session is None:
            return

        async def load() -> tuple[commands.ActivityList, commands.RouteList]:
            activities = await session.list_activities()
            routes = await session.list_routes()
            return activities, routes

        def ok(result: tuple[commands.ActivityList, commands.RouteList]) -> None:
            activities, routes = result
            self.activities.set_activities(activities.files)
            self.routes.set_routes(routes.routes)
            self._show_link(session)
            self.statusBar().showMessage(
                f"{len(activities.files)} activities, {len(routes.routes)} routes", 5000
            )

        connecting = "Loading…" if session.connected else "Connecting…"
        self._start(session, connecting, load(), ok)

    # ----- operations

    def _start(
        self,
        session: DeviceSession,
        message: str,
        coro: Coroutine[Any, Any, T],
        on_ok: Callable[[T], None],
    ) -> None:
        """Run *coro*, ignoring the result if the user switched devices meanwhile."""
        self._set_busy(True, message)

        def ok(result: T) -> None:
            if session is not self._session:
                return
            self._set_busy(False)
            on_ok(result)

        def err(exc: BaseException) -> None:
            if session is not self._session:
                return
            self._set_busy(False)
            self.statusBar().showMessage(f"Error: {_describe(exc)}", 10000)
            QtWidgets.QMessageBox.warning(self, "liGPSPORT", _describe(exc))

        self._runner.submit(coro, ok, err)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.activities.set_busy(busy)
        self.routes.set_busy(busy)
        self.refresh_button.setEnabled(not busy)
        if busy:
            self.progress.setRange(0, 0)
            self.progress.show()
            self.statusBar().showMessage(message)
        else:
            self.progress.hide()
            self.statusBar().clearMessage()

    def _download(self, activities: list[commands.ActivityFile]) -> None:
        session = self._session
        if session is None:
            return
        folder = self.activities.folder
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "liGPSPORT", _describe(exc))
            return
        total = len(activities)

        def show(i: int, activity: commands.ActivityFile) -> None:
            # The device sends a whole activity as one frame, so progress
            # is per file rather than per byte.
            self.progress.setRange(0, total)
            self.progress.setValue(i)
            self.statusBar().showMessage(
                f"Downloading {i + 1} of {total}: {activity.started_at:%Y-%m-%d %H:%M} "
                f"({_human_size(activity.file_size)})"
            )

        async def go() -> list[commands.DownloadedFile]:
            results = []
            for i, activity in enumerate(activities):
                self._bridge.post(functools.partial(show, i, activity))
                path = self.activities.path_for(activity)
                results.append(await session.download_activity(activity, path))
                self._bridge.post(self.activities.refresh_rows)
            return results

        def ok(results: list[commands.DownloadedFile]) -> None:
            self.activities.refresh_rows()
            bad = [r.path for r in results if not r.fit_magic]
            if bad:
                QtWidgets.QMessageBox.warning(
                    self,
                    "liGPSPORT",
                    "These downloads don't look like FIT files:\n" + "\n".join(bad),
                )
            noun = "activity" if len(results) == 1 else "activities"
            self.statusBar().showMessage(f"Downloaded {len(results)} {noun} to {folder}", 10000)

        self._start(session, "Downloading…", go(), ok)

    def _upload(self, paths: list[str], start_navigation: bool) -> None:
        session = self._session
        if session is None:
            return

        async def go() -> tuple[list[commands.UploadedRoute], commands.RouteList]:
            uploaded = []
            for n, path in enumerate(paths):
                name = os.path.basename(path)
                message = f"Uploading {n + 1} of {len(paths)}: {name}"
                self._bridge.post(functools.partial(self.statusBar().showMessage, message))
                last = n == len(paths) - 1
                uploaded.append(
                    await session.upload_route(path, start_navigation=start_navigation and last)
                )
            return uploaded, await session.list_routes()

        def ok(result: tuple[list[commands.UploadedRoute], commands.RouteList]) -> None:
            uploaded, routes = result
            self.routes.set_routes(routes.routes)
            failed = [u for u in uploaded if u.status != 0]
            if failed:
                QtWidgets.QMessageBox.warning(
                    self,
                    "liGPSPORT",
                    "The device rejected:\n"
                    + "\n".join(f"{u.name} (status {u.status})" for u in failed),
                )
            else:
                names = ", ".join(u.name for u in uploaded)
                self.statusBar().showMessage(f"Uploaded {names}", 10000)

        self._start(session, "Uploading…", go(), ok)

    def _delete_route(self, route: commands.RoutePlan) -> None:
        session = self._session
        if session is None:
            return
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete route")
        box.setText(f"Delete “{route.name}” from the device?")
        box.setInformativeText("This cannot be undone.")
        delete = box.addButton("Delete", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not delete:
            return

        async def go() -> tuple[commands.DelRouteResult, commands.RouteList]:
            # The user confirmed above; that is the destructive opt-in.
            result = await session.delete_route(route.id, allow_destructive=True)
            return result, await session.list_routes()

        def ok(result: tuple[commands.DelRouteResult, commands.RouteList]) -> None:
            deleted, routes = result
            self.routes.set_routes(routes.routes)
            if deleted.deleted:
                self.statusBar().showMessage(f"Deleted {route.name}", 10000)
            else:
                QtWidgets.QMessageBox.warning(self, "liGPSPORT", deleted.format())

        self._start(session, "Deleting…", go(), ok)

    # ----- shutdown

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._session is not None:
            with contextlib.suppress(Exception):
                self._runner.run_blocking(self._session.close(), timeout=5.0)
        super().closeEvent(event)


# ---------- entry point ---------------------------------------------------


async def _ble_scan(found: Callable[[discovery.Device], None]) -> None:
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(SCAN_SECONDS):
            async for dev in discovery.watch():
                found(dev)


def _simulated_devices() -> tuple[
    TransportFactory, Callable[[Callable[[discovery.Device], None]], Coroutine[Any, Any, None]]
]:
    """Two fake bike computers, so the GUI can be tried without hardware."""
    from .simulator import SimulatedActivityFile, SimulatedTransport, SimulatorState

    fit = bytes([14, 0x20, 0, 0, 0, 0, 0, 0]) + b".FIT" + b"\0\0" + bytes(range(256)) * 160
    base = 1_130_000_000  # 2025-10-21 in FIT-epoch seconds
    states = {
        "SIM:00:00:00:00:01": SimulatorState(
            allow_destructive=True,
            activity_files=[
                SimulatedActivityFile(timestamp=base - d * 86_400, file_size=len(fit), content=fit)
                for d in range(5)
            ],
        ),
        "SIM:00:00:00:00:02": SimulatorState(allow_destructive=True),
    }

    def factory(address: str) -> SimulatedTransport:
        return SimulatedTransport(states[address])

    async def scan(found: Callable[[discovery.Device], None]) -> None:
        for i, address in enumerate(states):
            await asyncio.sleep(0.5)
            found(discovery.Device(address=address, name=f"BSC200 sim {i + 1}", rssi=-50))

    return factory, scan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ligpsport-gui", description="liGPSPORT desktop app")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--backend",
        choices=("bleak", "bluez"),
        default="bleak",
        help="BLE backend (bluez is Linux-only, faster transfers)",
    )
    parser.add_argument(
        "--simulator", action="store_true", help="show simulated devices instead of scanning"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("ligpsport")
    app.setApplicationDisplayName("liGPSPORT")
    app.setOrganizationName("ligpsport")

    bridge = _Bridge()
    runner = AsyncRunner(bridge)
    if args.simulator:
        factory, scan = _simulated_devices()
    else:
        factory, scan = default_transport_factory(args.backend), _ble_scan

    window = MainWindow(bridge, runner, factory, scan)
    window.show()
    QtCore.QTimer.singleShot(0, window.start_scan)
    try:
        return app.exec()
    finally:
        runner.stop()


if __name__ == "__main__":
    sys.exit(main())
