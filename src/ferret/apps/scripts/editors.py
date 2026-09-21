"""Detail panel for one script: source editor plus the load-error pane."""

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    StrongBodyLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from ferret.apps.common.edit import Language, ToolPlainTextEdit
from ferret.apps.common.splitter import BaseSplitter
from ferret.apps.scripts.models import (
    origin_icon,
    origin_label,
    script_name,
    state_icon,
    state_label,
)
from ferret.core.mitm import (
    SCRIPT_ORIGIN_NEW,
    ScriptEntry,
    ScriptState,
    ScriptStatus,
)


class ScriptEditorPanel(QWidget):
    """选中条目的正文面板。

    两种来源同一个面板，差别只在**可写与否**（plans/scripts.md §3.4 那张表）：
    new 条目在应用内编辑、保存即重载；import 条目只读预览，改文件要去系统编辑器，
    回来点「重载」。只读预览不在表里，但表禁的是「内嵌编辑」——
    看一眼自己加载的是什么，比逼用户开外部编辑器有用。

    高亮用 `Language.TEXT`：`Language` 还没有 Python 分词器，补它是
    plans/scripts.md §7 第 3 条的后续增量，不与本功能捆绑。
    """

    save_requested = Signal(str, str)
    save_as_requested = Signal(str)
    reload_requested = Signal(str)
    open_external_requested = Signal(str)
    dirty_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._entry: ScriptEntry | None = None
        self._status: ScriptStatus | None = None
        self._saved_text = ""
        self._readable = False
        # 程序化灌文本不算「用户改了」，否则切一行就亮起保存按钮。
        self._syncing = False
        self._dirty = False

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self.clear()

    # --- 构造 ---

    def __init_widget(self):
        self.origin_icon = IconWidget(FluentIcon.DOCUMENT, self)
        self.origin_icon.setFixedSize(16, 16)
        self.name_label = StrongBodyLabel(self)
        self.state_icon = IconWidget(FluentIcon.HISTORY, self)
        self.state_icon.setFixedSize(14, 14)
        self.state_label = CaptionLabel(self)
        self.path_label = CaptionLabel(self)
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.editor = ToolPlainTextEdit(self)
        self.hint_label = CaptionLabel(self.editor)

        self.save_btn = self.__tool_button(FluentIcon.SAVE, self.tr("保存并重载"))
        self.save_as_btn = self.__tool_button(FluentIcon.SAVE_AS, self.tr("另存为"))
        self.reload_btn = self.__tool_button(FluentIcon.SYNC, self.tr("重载"))
        self.open_btn = self.__tool_button(
            FluentIcon.DEVELOPER_TOOLS, self.tr("在编辑器中打开")
        )

        # 加载失败时才出现；traceback 不译（plans/scripts.md §3.4）。
        self.error_view = ToolPlainTextEdit(self)
        self.error_view.set_read_only(True)
        self.error_label = CaptionLabel(self.error_view)
        self.error_label.setText(self.tr("加载错误"))
        self.error_view.tool_layout.addWidget(self.error_label)
        self.error_view.setVisible(False)

        self.placeholder = BodyLabel(self.tr("选择左侧的脚本查看内容"), self)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def __tool_button(self, icon: FluentIcon, tip: str) -> TransparentToolButton:
        button = TransparentToolButton(icon, self)
        button.setFixedSize(30, 30)
        button.setIconSize(QSize(15, 15))
        button.setToolTip(tip)
        button.installEventFilter(ToolTipFilter(button, 600, ToolTipPosition.TOP))
        return button

    def __init_layout(self):
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        header_layout.addWidget(self.origin_icon)
        header_layout.addWidget(self.name_label)
        header_layout.addSpacing(6)
        header_layout.addWidget(self.state_icon)
        header_layout.addWidget(self.state_label)
        header_layout.addStretch(1)

        tool_layout = self.editor.tool_layout
        tool_layout.addWidget(self.hint_label)
        tool_layout.addStretch(1)
        tool_layout.addWidget(self.save_btn)
        tool_layout.addWidget(self.save_as_btn)
        tool_layout.addWidget(self.reload_btn)
        tool_layout.addWidget(self.open_btn)

        self.splitter = BaseSplitter(Qt.Orientation.Vertical, self)
        self.splitter.addWidget(self.editor)
        self.splitter.addWidget(self.error_view)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)

        self.body = QWidget(self)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        body_layout.addWidget(header)
        body_layout.addWidget(self.path_label)
        body_layout.addWidget(self.splitter, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(0)
        layout.addWidget(self.placeholder, 1)
        layout.addWidget(self.body, 1)

    def __connect_signal_to_slot(self):
        self.editor.changed.connect(self._on_text_changed)
        self.save_btn.clicked.connect(self._on_save)
        self.save_as_btn.clicked.connect(self._on_save_as)
        self.reload_btn.clicked.connect(self._on_reload)
        self.open_btn.clicked.connect(self._on_open_external)

    # --- 对外 ---

    @property
    def entry(self) -> ScriptEntry | None:
        return self._entry

    @property
    def dirty(self) -> bool:
        return self._dirty

    def clear(self) -> None:
        """回到「没选中任何脚本」。"""
        self._entry = None
        self._status = None
        self._readable = False
        self._load_text("")
        self.error_view.setVisible(False)
        self.body.setVisible(False)
        self.placeholder.setVisible(True)

    def show_entry(
        self, entry: ScriptEntry, status: ScriptStatus | None, text: str
    ) -> None:
        """展示一条脚本的正文（`text` 由控制器读盘得到）。"""
        self._entry = entry
        self._readable = True
        self._sync_header(entry, status)
        self._load_text(text)
        self.set_status(status)
        self.placeholder.setVisible(False)
        self.body.setVisible(True)

    def show_unreadable(
        self, entry: ScriptEntry, status: ScriptStatus | None, reason: str
    ) -> None:
        """文件读不出来（多半是被移走了）：不给编辑，只留重载/打开。"""
        self._entry = entry
        self._readable = False
        self._sync_header(entry, status)
        self._load_text("")
        self.set_status(status, fallback=reason)
        self.placeholder.setVisible(False)
        self.body.setVisible(True)

    def update_entry(self, entry: ScriptEntry, status: ScriptStatus | None) -> None:
        """条目对象换了（启停/换序）但还是同一个文件：只换引用与表头，不碰正文。

        重读正文会吃掉用户正在改的内容 —— 勾一下「启用」就丢一段代码。
        """
        self._entry = entry
        self.set_status(status)

    def set_status(self, status: ScriptStatus | None, fallback: str = "") -> None:
        """只刷状态，不动正文 —— 内核回报是异步到的，正文可能正在被编辑。"""
        self._status = status
        if self._entry is not None:
            self._sync_header(self._entry, status)
        self.error_label.setText(self.tr("加载错误"))
        error = status.error if status is not None else ""
        text = error or fallback
        if text:
            self.error_view.set_text(text, lang=Language.TEXT)
            self.error_view.setVisible(True)
        else:
            self.error_view.setVisible(False)
        self._refresh_buttons()

    def mark_saved(self, text: str) -> None:
        """写盘成功后把基线挪到当前内容，保存按钮随之熄灭。"""
        self._saved_text = text
        self._refresh_dirty()

    # --- 内部 ---

    def _sync_header(self, entry: ScriptEntry, status: ScriptStatus | None) -> None:
        icon = origin_icon(entry.origin)
        if icon is not None:
            self.origin_icon.setIcon(icon)
        self.origin_icon.setToolTip(origin_label(entry.origin))
        self.name_label.setText(script_name(entry))
        state = state_icon(status)
        if state is not None:
            self.state_icon.setIcon(state)
        self.state_label.setText(state_label(status))
        self.path_label.setText(entry.path)

    def _load_text(self, text: str) -> None:
        self._syncing = True
        try:
            self.editor.set_text(text, lang=Language.TEXT)
        finally:
            self._syncing = False
        self._saved_text = text
        self._refresh_buttons()

    def _editable(self) -> bool:
        """只有应用内新建的脚本在应用内改（§3.4 那张表）。"""
        return (
            self._entry is not None
            and self._entry.origin == SCRIPT_ORIGIN_NEW
            and self._readable
        )

    def _refresh_buttons(self) -> None:
        editable = self._editable()
        self.editor.set_read_only(not editable)
        self.save_btn.setVisible(editable)
        self.save_as_btn.setVisible(self._readable)
        # import 条目改完外部文件要手动点重载；new 条目保存即重载，重载按钮仍留着
        # —— 外部工具改过同一个文件时它是唯一的补救入口。
        self.reload_btn.setEnabled(self._entry is not None)
        self.open_btn.setEnabled(self._entry is not None)
        if self._entry is None:
            self.hint_label.setText("")
        elif not self._readable:
            self.hint_label.setText(self.tr("文件读不到，内容无法显示"))
        elif editable:
            self.hint_label.setText("")
        else:
            self.hint_label.setText(self.tr("导入的脚本只读，改动请用系统编辑器"))
        self._refresh_dirty()

    def _refresh_dirty(self) -> None:
        dirty = self._editable() and self.editor.text() != self._saved_text
        self.save_btn.setEnabled(dirty)
        if dirty != self._dirty:
            self._dirty = dirty
            self.dirty_changed.emit(dirty)

    @Slot()
    def _on_text_changed(self) -> None:
        if self._syncing:
            return
        self._refresh_dirty()
        # 用户又动了正文，上一轮的加载错误就不再对应屏幕上这份内容了。
        if self._status is not None and self._status.state == ScriptState.ERROR:
            self.error_label.setText(self.tr("加载错误（保存后重新校验）"))

    @Slot()
    def _on_save(self) -> None:
        if self._entry is not None and self._editable():
            self.save_requested.emit(self._entry.path, self.editor.text())

    @Slot()
    def _on_save_as(self) -> None:
        if self._entry is not None and self._readable:
            self.save_as_requested.emit(self.editor.text())

    @Slot()
    def _on_reload(self) -> None:
        if self._entry is not None:
            self.reload_requested.emit(self._entry.path)

    @Slot()
    def _on_open_external(self) -> None:
        if self._entry is not None:
            self.open_external_requested.emit(self._entry.path)
