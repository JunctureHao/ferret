"""流量标记（``flow.marked``）的 UI 侧帮助函数与全量 emoji 选择对话框。

标记值是上游钉死的 emoji 短码（``":bug:"`` 这类，`mitmproxy.utils.emoji.emoji`
整本字典，1843 条，用户不可扩充 —— 旧版开关写的 ``":default:"`` 也是其中一条，
映射到 ``"●"``），显示时经同一本字典翻译成 emoji 字符。写回与校验走
`MitmFacade.set_flow_marked`，本模块只管「把 1843 个候选摆出来让人挑一个」。

方案与决策见 `.plans/flow-mark.md`。
"""

from functools import cache

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    ListView,
    MessageBoxBase,
    SearchLineEdit,
    SubtitleLabel,
)

from ferret.core.mitm import emoji

# 不在字典里的短码的显示兜底。取值与原生 console 的 `SYMBOL_MARK` 一致
# （`tools/console/common.py:97,162` 同款 `emoji.emoji.get(marker, SYMBOL_MARK)`），
# 也正是 `":default:"` 在字典里的值 —— 旧开关标过的流量看上去不会变样。
# （本文件有 `tr()`，注释一律用 `#` —— `#:` 会被 lupdate 吃成译者说明。）
FALLBACK_GLYPH = "●"

_SHORTCODE_ROLE = int(Qt.ItemDataRole.UserRole) + 1

# emoji-first 字体族：把每个字形优先路由到彩色 emoji 字体（本机 Windows 的
# `Segoe UI Emoji` 有全套彩色字形），字体里没有的（`●` 兕底符、裸字母 `a`/`1`）
# 自然回退到界面族。上游字典里的 ✈ ♉ ⚓ 这类文本态符号在**单族** UI 字体
# （`Microsoft YaHei`）下只有又细又淡的单色字形，逐字回退也够不着 emoji 字体 ——
# 显式把 emoji 族排在最前才画得出彩色。
#
# 多族 `setFamilies` 会触发 QFontDatabase 整库扫描（启动期禁忌，见
# `core/application.py::_init_font`）。这里刻意只挂在标记选择器和表格 Mark 列上：
# 扫描的开销落在「打开选择器 / 首个标记渲染」那一刻，不落启动路径 —— 与那份单族
# 策略同源（「字体库的钱推迟到用户真的看到那一刻才付」）。
#
# 缺失的族 Qt 自动跳过，故此列表跨平台安全：非 Windows 落到 Apple/Noto/系统族。
_EMOJI_FAMILIES = (
    "Segoe UI Emoji",
    "Apple Color Emoji",
    "Noto Color Emoji",
    "Microsoft YaHei",
)


@cache
def emoji_font(pixel_size: int) -> QFont:
    """emoji-first 字体，按像素字号缓存（表格 Mark 列与选择器网格共用）。

    delegate 侧靠 `Qt.FontRole` 生效：qfluentwidgets 的 `TableItemDelegate`
    渲染时 `option.font = index.data(FontRole) or getFont(13)`，会盖掉视图自身的
    `setFont` —— 所以字体必须从 model 的 `FontRole` 递出去，不能只设在视图上。
    """
    font = QFont()
    font.setFamilies(list(_EMOJI_FAMILIES))
    font.setPixelSize(pixel_size)
    return font


# 选择器列表的行字号（glyph 与短码同一行同字号）；表格 Mark 列另用字号（见 models.py）。
_PICKER_GLYPH_PX = 18


_ZWJ = "‍"


def _present(glyph: str) -> str:
    """把上游存的字符归一成能正常渲染的显示字形（纯显示，不碰存储）。

    上游 `utils/emoji.py` 在国旗的两个区域指示符（`U+1F1E6..1F1FF`）之间硬塞了一个
    ZWJ（`":us:"` = `🇺 ZWJ 🇸`）—— 它 console 里靠这个把旗拆成两个字母框，但在 GUI
    里这个 ZWJ 恰恰**破坏**了旗面合成，画出来是两个方框字母而不是国旗。剥掉即还原成
    合法国旗序列，字体能合成旗面（本机 Segoe UI Emoji cmap 全覆盖区域指示符）。

    只对含区域指示符的序列剥 ZWJ：家庭 / 职业那 223 个合成 emoji（`:astronaut:` =
    `🧑 ZWJ 🚀`）的 ZWJ 是**必需**的合成粘合剂，不含区域指示符，一律不动。
    """
    if _ZWJ in glyph and any(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in glyph):
        return glyph.replace(_ZWJ, "")
    return glyph


def marker_glyph(shortcode: str) -> str:
    """emoji 短码 → 显示字符；未标记是空字符，不认得的短码落到兜底符号。

    不认得的值只能来自外部（别人存的 .flow 文件、手写的脚本），Ferret 自己的
    写入链（`set_flow_marked`）在校验层就拒掉了。
    """
    if not shortcode:
        return ""
    return _present(emoji.emoji.get(shortcode, FALLBACK_GLYPH))


@cache
def _marker_entries() -> tuple[tuple[str, str], ...]:
    """整本 emoji 字典 → ``(shortcode, glyph)`` 序列，模块级懒缓存。

    glyph 经 `_present` 归一（国旗剥掉上游注入的 ZWJ），与 `marker_glyph` 同源，
    保证选择器网格与表格 Mark 列、概览卡三处显示完全一致。

    字典在上游源码里是生成物（`utils/emoji.py` 自述 auto-generated），运行期
    不会变，缓存一份即可。QAbstractListModel 要稳定顺序：`:xxx:` 短码按字典序
    排前面，字典尾部那 62 个单字母 / 数字（`"a"` → `"a"`，上游同样认作合法标记）
    垫底 —— 否则网格一打开先是一排 0-9，emoji 反倒要往下翻。
    """
    return tuple(
        (shortcode, _present(glyph))
        for shortcode, glyph in sorted(
            emoji.emoji.items(), key=lambda kv: (not kv[0].startswith(":"), kv[0])
        )
    )


class _MarkerListModel(QAbstractListModel):
    """全量标记的列表模型（竖排 ListMode）：DisplayRole=「glyph  短码」，
    Decoration 无；ToolTipRole/内部角色=纯短码（供搜索与写回）。

    为什么每行连短码一起显示、不用 IconMode 只摆 glyph：IconMode 网格在本机
    实测有活体绘制缺陷（大量格子不绘字形，`grab()` 却正常，说明是视图刷新层的坑
    而非字体），换 ListMode + qfluentwidgets `ListView` 的行委托绘制稳定；顺带把
    短码亮在字形边上，认不出图形也能读名字，比纯网格更好用。
    """

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self._entries = _marker_entries()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._entries)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return None
        shortcode, glyph = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return f"{glyph}   {shortcode}"
        if role in (Qt.ItemDataRole.ToolTipRole, _SHORTCODE_ROLE):
            return shortcode
        # 字体经 FontRole 递出，delegate 才认（见 `emoji_font` docstring）。行内既有
        # emoji 又有拉丁短码，emoji-first 字体族的 YaHei 兜住拉丁字母。
        if role == Qt.ItemDataRole.FontRole:
            return emoji_font(_PICKER_GLYPH_PX)
        return None


class _MarkerFilterModel(QSortFilterProxyModel):
    """按 shortcode 子串过滤。过滤键不进展示（展示是 glyph），所以自定义
    filterAcceptsRow 而不是 `setFilterKeyColumn` 那套（那是按列的）。

    不走基类的 `setFilterFixedString`：那套内部是正则转义，搜 `"*"` 这类字符时
    `pattern()` 拿回来的是转义后的串，子串比较就失了效。纯文本存自己手上。"""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._needle = ""

    def set_filter_text(self, text: str) -> None:
        self._needle = text.strip().lower()
        # Qt 6.10 起 `invalidateFilter()` 弃用，按行/列拆成了两个。
        self.invalidateRowsFilter()

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        if not self._needle:
            return True
        model = self.sourceModel()
        assert isinstance(model, _MarkerListModel)
        return self._needle in model._entries[source_row][0]


class MarkerPickerDialog(MessageBoxBase):
    """全量 emoji 标记选择器：搜索框 + 网格。双击或「确定」生效。

    出参读 `selected`：接受时是短码，取消时是 None。搜索只认 shortcode 原文
    （上游字典没有中文名，自译 1843 条 i18n 链吃不下），tooltip 把短码亮出来
    就是为了让人搜得着。
    """

    def __init__(self, current: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.selected: str | None = None

        self.title_label = SubtitleLabel(self.tr("标记流量"), self)
        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr("按标记名搜索，如 bug"))

        self._model = _MarkerListModel(self)
        self._proxy = _MarkerFilterModel(self)
        self._proxy.setSourceModel(self._model)

        # 竖排 ListMode + qfluentwidgets `ListView`：行委托绘制稳定、底色 / 选中高亮
        # 自带随主题走的 QSS。此前 IconMode 网格（无论原生 QListView 还是 ListView）在
        # 本机都有活体绘制缺陷 —— 大量格子不绘字形，`grab()` 抓像素却正常，是视图刷新层
        # 的坑不是字体。ListMode 是 ListView 委托的主场，绘制稳，顺带每行把短码亮在
        # glyph 边上，更好认好搜。
        self.grid = ListView(self)
        self.grid.setModel(self._proxy)
        self.grid.setUniformItemSizes(True)
        self.grid.setMinimumSize(420, 320)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.grid, 1)
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(520)

        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))
        # 没选中任何一格时「确定」没有可交付的值，置灰比 accept 后静默空转诚实。
        self.yesButton.setEnabled(False)

        self.search_edit.textChanged.connect(self._proxy.set_filter_text)
        self.grid.doubleClicked.connect(self.__on_double_clicked)
        selection = self.grid.selectionModel()
        assert selection is not None
        selection.currentChanged.connect(self.__on_current_changed)

        if current:
            self.__locate(current)

    def __locate(self, shortcode: str) -> None:
        """打开时把当前标记滚动定位并选中；外部写进来的野短码无位可定，不选。"""
        keys = [key for key, _ in self._model._entries]
        try:
            row = keys.index(shortcode)
        except ValueError:
            return
        index = self._proxy.mapFromSource(self._model.index(row))
        if index.isValid():
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)

    def __on_current_changed(
        self, current: QModelIndex, _previous: QModelIndex
    ) -> None:
        # 搜索把当前项过滤掉时 current 会变无效，「确定」随之回灰。
        self.yesButton.setEnabled(current.isValid())

    def __current_shortcode(self) -> str | None:
        index = self.grid.currentIndex()
        if not index.isValid():
            return None
        return index.data(_SHORTCODE_ROLE)

    def __on_double_clicked(self, index: QModelIndex) -> None:
        shortcode = index.data(_SHORTCODE_ROLE)
        if shortcode:
            self.selected = shortcode
            self.accept()

    def accept(self) -> None:
        # 「确定」路径：以当前选中项为准。按钮在无选中时是灰的，这里再兜一层：
        # 键盘回车等旁路进来时 selected 保持 None，调用方按「没选」处理。
        shortcode = self.__current_shortcode()
        if shortcode:
            self.selected = shortcode
        super().accept()
