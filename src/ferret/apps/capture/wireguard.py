"""WireGuard 设备管理；磁盘与内核操作均在 FunctionTask 中完成。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    LineEdit,
    MessageBoxBase,
    SpinBox,
    SubtitleLabel,
    TableWidget,
    TransparentPushButton,
)

from ferret.apps.common.tasks import FunctionTask
from ferret.core.mitm import MitmFacade, WireGuardDevice, wireguard_devices_to_config
from ferret.core.network import PORT_MAX, PORT_MIN
from ferret.core.settings import CONFIG


@dataclass(frozen=True)
class _DeviceSnapshot:
    devices: list[WireGuardDevice]
    health: dict[str, bool | str]
    engaged: bool
    enabled: bool


class WireGuardDeviceDialog(MessageBoxBase):
    """只编辑设备描述；保存、生成密钥与热更交由管理窗口。"""

    def __init__(
        self, device: WireGuardDevice, parent: QWidget, *, is_new: bool = False
    ) -> None:
        super().__init__(parent)
        self._device = device
        self.title_label = SubtitleLabel(
            self.tr("添加 WireGuard 设备")
            if is_new
            else self.tr("编辑 WireGuard 设备"),
            self,
        )
        self.name_edit = LineEdit(self)
        self.name_edit.setText(device.display_name)
        self.name_edit.setMaxLength(80)
        self.name_edit.setPlaceholderText(self.tr("设备名称"))
        self.name_edit.setAccessibleName(self.tr("设备名称"))
        self.port_spin = SpinBox(self)
        self.port_spin.setRange(PORT_MIN, PORT_MAX)
        self.port_spin.setValue(device.port)
        self.port_spin.setAccessibleName(self.tr("UDP 端口"))
        self.enabled_check = CheckBox(self.tr("启用此设备"), self)
        self.enabled_check.setChecked(device.enabled)
        self.hint_label = CaptionLabel(
            self.tr("每台设备使用独立的二维码。更改端口后，需要重新扫码导入。"),
            self,
        )
        self.hint_label.setWordWrap(True)
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(BodyLabel(self.tr("设备名称"), self))
        self.viewLayout.addWidget(self.name_edit)
        port_row = QHBoxLayout()
        port_row.addWidget(BodyLabel(self.tr("UDP 端口"), self))
        port_row.addStretch(1)
        port_row.addWidget(self.port_spin)
        self.viewLayout.addLayout(port_row)
        self.viewLayout.addWidget(self.enabled_check)
        self.viewLayout.addWidget(self.hint_label)
        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))
        self.name_edit.textChanged.connect(self._validate_name)
        self._validate_name()
        self.widget.setMinimumWidth(430)

    def _validate_name(self) -> None:
        self.yesButton.setEnabled(bool(self.name_edit.text().strip()))

    def device(self) -> WireGuardDevice:
        return replace(
            self._device,
            name=self.name_edit.text().strip(),
            port=self.port_spin.value(),
            enabled=self.enabled_check.isChecked(),
        )


class _DeviceConfirmationDialog(MessageBoxBase):
    def __init__(self, title: str, text: str, action: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(title, self))
        description = BodyLabel(text, self)
        description.setWordWrap(True)
        self.viewLayout.addWidget(description)
        self.yesButton.setText(action)
        self.cancelButton.setText(self.tr("取消"))
        self.widget.setMinimumWidth(440)


class WireGuardDevicesDialog(MessageBoxBase):
    """设备操作独立保存，不依赖父抓包设置窗口是否点应用。

    一次只运行一个任务，且持有至 finished。关闭请求延迟到任务结束，避免父窗口
    在密钥写入或热更中途销毁任务持有者；所有 Qt 更新由具名槽回主线程完成。
    """

    devicesChanged = Signal()

    def __init__(self, facade: MitmFacade, parent: QWidget) -> None:
        super().__init__(parent)
        self._facade = facade
        self._devices: list[WireGuardDevice] = []
        self._task: FunctionTask | None = None
        self._operation = ""
        self._result: object = None
        self._selected_id: str | None = None
        self._close_requested = False
        self._loaded = False
        self._previous_devices: list[WireGuardDevice] = []
        self._previous_config: object = None
        self._save_error = ""
        self.title_label = SubtitleLabel(self.tr("WireGuard 设备"), self)
        self.description = CaptionLabel(
            self.tr(
                "一份二维码仅供一台设备使用。设备更改立即保存；开始抓包后接通已启用设备。"
            ),
            self,
        )
        self.description.setWordWrap(True)
        self.add_button = TransparentPushButton(
            FluentIcon.ADD, self.tr("添加设备"), self
        )
        self.edit_button = TransparentPushButton(FluentIcon.EDIT, self.tr("编辑"), self)
        self.toggle_button = TransparentPushButton(self.tr("停用"), self)
        self.delete_button = TransparentPushButton(
            FluentIcon.DELETE, self.tr("删除"), self
        )
        self.rotate_button = TransparentPushButton(self.tr("更换密钥"), self)
        self.qr_button = TransparentPushButton(
            FluentIcon.QRCODE, self.tr("二维码"), self
        )
        self._selection_buttons = (
            self.edit_button,
            self.toggle_button,
            self.delete_button,
            self.rotate_button,
            self.qr_button,
        )
        actions = QHBoxLayout()
        actions.setSpacing(4)
        actions.addWidget(self.add_button)
        actions.addStretch(1)
        for button in self._selection_buttons:
            actions.addWidget(button)

        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("设备名称"),
                self.tr("UDP 端口"),
                self.tr("启用状态"),
                self.tr("监听状态"),
            ]
        )
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setMinimumHeight(230)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )

        self.empty_label = CaptionLabel(self.tr("尚无设备，请先添加设备。"), self)
        self.empty_label.hide()
        self.error_label = CaptionLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.progress_label = CaptionLabel(self)
        self.progress_label.hide()
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.description)
        self.viewLayout.addLayout(actions)
        self.viewLayout.addWidget(self.table)
        self.viewLayout.addWidget(self.empty_label)
        self.viewLayout.addWidget(self.error_label)
        self.viewLayout.addWidget(self.progress_label)
        self.widget.setMinimumWidth(710)
        self.hideYesButton()
        self.cancelButton.setText(self.tr("关闭"))
        self.add_button.clicked.connect(self._add_device)
        self.edit_button.clicked.connect(self._edit_device)
        self.toggle_button.clicked.connect(self._toggle_device)
        self.delete_button.clicked.connect(self._delete_device)
        self.rotate_button.clicked.connect(self._rotate_device)
        self.qr_button.clicked.connect(self._show_qr)
        self.table.itemSelectionChanged.connect(self._sync_actions)
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._sync_actions()
        self.refresh()

    def _snapshot(self) -> _DeviceSnapshot:
        devices = self._facade.wireguard_devices
        health: dict[str, bool | str]
        try:
            health = self._facade.wireguard_device_health()
        except Exception as exc:  # noqa: BLE001
            # 内核健康读取失败不能掩盖已经应用的设备变更，仍须完成配置落盘。
            health = {device.id: str(exc) for device in devices}
        return _DeviceSnapshot(
            devices,
            health,
            self._facade.channels_engaged,
            self._facade.use_wireguard,
        )

    @Slot()
    def refresh(self) -> None:
        if self._task is None and not self._close_requested:
            self._start("refresh", self._snapshot)

    def _start(self, operation: str, work: Callable[[], object]) -> None:
        if self._task is not None:
            return
        self._operation = operation
        self._result = None
        task = FunctionTask(work)
        self._task = task
        task.signals.succeeded.connect(self._on_succeeded)
        task.signals.failed.connect(self._on_failed)
        task.signals.finished.connect(self._on_finished)
        if operation != "refresh":
            self.error_label.hide()
            self.progress_label.setText(self.tr("正在处理设备配置…"))
            self.progress_label.show()
        self._sync_actions()
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_succeeded(self, result: object) -> None:
        self._result = result

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.error_label.setText(
            self.tr("设备配置保存失败：{}；恢复原配置也失败：{}").format(
                self._save_error, message
            )
            if self._operation == "rollback"
            else self.tr("设备操作失败：{}").format(message)
        )
        self.error_label.show()

    @Slot()
    def _on_finished(self) -> None:
        task, self._task = self._task, None
        result, self._result = self._result, None
        operation, self._operation = self._operation, ""
        if task is not None:
            task.signals.succeeded.disconnect(self._on_succeeded)
            task.signals.failed.disconnect(self._on_failed)
            task.signals.finished.disconnect(self._on_finished)
        self.progress_label.hide()
        if isinstance(result, _DeviceSnapshot):
            if operation == "save" and not self._persist(result):
                return
            self._render(result)
            if operation in {"save", "rollback"}:
                self.devicesChanged.emit()
        self._sync_actions()
        if self._close_requested:
            self.reject()
        elif isinstance(result, WireGuardDevice):
            self._edit(result, is_new=True)
        elif isinstance(result, str) and operation == "qr":
            # 延迟 import 避免与抓包设置窗口的入口形成循环。
            from ferret.apps.capture.views import WireGuardConfigDialog

            device = self._selected_device()
            dialog = WireGuardConfigDialog(
                result,
                self.window(),
                device_name=device.display_name if device else "",
            )
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()

    def _persist(self, snapshot: _DeviceSnapshot) -> bool:
        try:
            # Config.save 会停止 Qt 的延迟保存计时器，必须在 GUI 线程调用。
            CONFIG.set(
                CONFIG.wireguard_devices,
                wireguard_devices_to_config(snapshot.devices),
            )
        except Exception as exc:  # noqa: BLE001
            self._save_error = str(exc)
            CONFIG.set(CONFIG.wireguard_devices, self._previous_config, save=False)
            self.error_label.setText(self.tr("设备配置保存失败：{}").format(exc))
            self.error_label.show()
            previous = list(self._previous_devices)

            def rollback() -> _DeviceSnapshot:
                self._facade.set_wireguard_devices(previous)
                return self._snapshot()

            self._start("rollback", rollback)
            # rollback 不得清掉落盘失败的原因。
            self.error_label.show()
            return False
        return True

    def _render(self, snapshot: _DeviceSnapshot) -> None:
        selected = self._selected_device()
        selected_id = self._selected_id or (selected.id if selected else None)
        self._devices = snapshot.devices
        self._loaded = True
        self.table.setRowCount(len(snapshot.devices))
        for row, device in enumerate(snapshot.devices):
            health = snapshot.health.get(device.id, False)
            if not device.enabled:
                listening = self.tr("未监听")
            elif not snapshot.enabled:
                listening = self.tr("通道未启用")
            elif not snapshot.engaged:
                listening = self.tr("未开始抓包")
            elif isinstance(health, str):
                listening = self.tr("监听失败")
            elif health:
                listening = self.tr("监听就绪")
            else:
                listening = self.tr("正在启动")
            values = (
                device.display_name,
                str(device.port),
                self.tr("已启用") if device.enabled else self.tr("已停用"),
                listening,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, device.id)
                item.setToolTip(
                    health if column == 3 and isinstance(health, str) else value
                )
                self.table.setItem(row, column, item)
            if device.id == selected_id:
                self.table.selectRow(row)
        self._selected_id = None
        if self.table.currentRow() < 0 and snapshot.devices:
            self.table.selectRow(0)
        self.empty_label.setVisible(not snapshot.devices)

    def _selected_device(self) -> WireGuardDevice | None:
        item = self.table.item(self.table.currentRow(), 0)
        if item is None:
            return None
        device_id = item.data(Qt.ItemDataRole.UserRole)
        return next(
            (device for device in self._devices if device.id == device_id), None
        )

    @Slot()
    def _sync_actions(self) -> None:
        # 健康读取与修改串行，避免后台刷新覆盖刚保存的选择或并发访问配置。
        busy = self._task is not None
        self.add_button.setEnabled(self._loaded and not busy)
        device = self._selected_device()
        for button in self._selection_buttons:
            button.setEnabled(device is not None and not busy)
        self.toggle_button.setText(
            self.tr("停用")
            if device is not None and device.enabled
            else self.tr("启用")
        )

    @Slot()
    def _add_device(self) -> None:
        name = self.tr("设备 {}").format(len(self._devices) + 1)
        self._start("new", lambda: self._facade.new_wireguard_device(name))

    @Slot()
    def _edit_device(self) -> None:
        device = self._selected_device()
        if device is not None:
            self._edit(device)

    def _edit(self, device: WireGuardDevice, *, is_new: bool = False) -> None:
        self._timer.stop()
        dialog = WireGuardDeviceDialog(device, self.window(), is_new=is_new)
        try:
            if not dialog.exec():
                return
            updated = dialog.device()
            devices = list(self._devices)
            if is_new:
                devices.append(updated)
            else:
                devices = [
                    updated if item.id == updated.id else item for item in devices
                ]
            self._selected_id = updated.id
            self._save(devices)
        finally:
            dialog.deleteLater()
            self._timer.start()

    def _save(self, devices: list[WireGuardDevice]) -> None:
        self._remember_config()

        def work() -> _DeviceSnapshot:
            self._facade.set_wireguard_devices(devices)
            return self._snapshot()

        self._start("save", work)

    def _remember_config(self) -> None:
        self._previous_devices = list(self._devices)
        self._previous_config = deepcopy(CONFIG.get(CONFIG.wireguard_devices))

    @Slot()
    def _toggle_device(self) -> None:
        device = self._selected_device()
        if device is None:
            return
        self._save(
            [
                replace(item, enabled=not item.enabled)
                if item.id == device.id
                else item
                for item in self._devices
            ]
        )

    def _confirm(self, title: str, text: str, action: str) -> bool:
        self._timer.stop()
        dialog = _DeviceConfirmationDialog(title, text, action, self.window())
        try:
            return bool(dialog.exec())
        finally:
            dialog.deleteLater()
            self._timer.start()

    @Slot()
    def _delete_device(self) -> None:
        device = self._selected_device()
        if device is not None and self._confirm(
            self.tr("删除设备“{}”？").format(device.display_name),
            self.tr("此设备将断开，原二维码将失效。其他设备继续使用各自的配置。"),
            self.tr("删除"),
        ):
            self._save([item for item in self._devices if item.id != device.id])

    @Slot()
    def _rotate_device(self) -> None:
        device = self._selected_device()
        if device is None or not self._confirm(
            self.tr("更换“{}”的密钥？").format(device.display_name),
            self.tr("原二维码将失效，此设备需要重新扫码导入。其他设备不受影响。"),
            self.tr("更换密钥"),
        ):
            return
        self._remember_config()

        def work() -> _DeviceSnapshot:
            self._facade.rotate_wireguard_device(device.id)
            return self._snapshot()

        self._start("save", work)

    @Slot()
    def _show_qr(self) -> None:
        device = self._selected_device()
        if device is not None:
            self._start("qr", lambda: self._facade.wireguard_client_config(device.id))

    def reject(self) -> None:
        self._timer.stop()
        if self._task is not None:
            self._close_requested = True
            self.progress_label.setText(self.tr("正在完成设备操作，请稍候…"))
            self.progress_label.show()
            return
        super().reject()
