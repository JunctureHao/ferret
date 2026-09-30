import ipaddress
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBoxSettingCard,
    CustomColorSettingCard,
    ExpandLayout,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    OptionsSettingCard,
    PlainTextEdit,
    PushSettingCard,
    ScrollArea,
    SettingCard,
    SettingCardGroup,
    SmoothMode,
    SubtitleLabel,
    SwitchSettingCard,
    TitleLabel,
    setTheme,
    setThemeColor,
)

from ferret.apps.common.info_bar import show_success, show_warning
from ferret.apps.settings.controllers import UpdateController
from ferret.apps.settings.dialogs import UpdateDialog
from ferret.core import update as update_core
from ferret.core.meta import REPO_URL
from ferret.core.settings import CONFIG

if TYPE_CHECKING:
    from ferret.apps.window import MainWindow
    from ferret.core.mitm import MitmFacade
    from ferret.core.update import UpdateBrief


class DnsServersDialog(MessageBoxBase):
    """自定义 DNS 服务器编辑对话框（.plans/dns-options.md §5.5）。

    每行一个 IPv4 / IPv6 地址，空行忽略。行内校验给出「第 N 行」定位，提交链上
    `facade.set_dns_options` 的整批校验是兜底闸门（预校验挡住时到不了那里）。
    """

    def __init__(self, current: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget(current)
        self.__init_layout()

    def __init_widget(self, current: list[str]) -> None:
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("自定义 DNS 服务器"))

        self.desc_label = BodyLabel(self)
        self.desc_label.setWordWrap(True)
        self.desc_label.setText(
            self.tr(
                "仅对 WireGuard 隧道内的 DNS 查询生效；留空使用系统 DNS。"
                "每行一个 IPv4 / IPv6 地址。"
            )
        )

        self.editor = PlainTextEdit(self)
        self.editor.setPlaceholderText("223.5.5.5")
        # 对话框存续期内的编辑器高度：几台服务器足够，多了一样要滚动。
        self.editor.setFixedHeight(140)
        if current:
            self.editor.setPlainText("\n".join(current))

        self.error_label = CaptionLabel(self)
        self.error_label.setWordWrap(True)
        # 警示色与 capture 页 exposure_label 同款（不做主题分叉，两主题下可读）。
        self.error_label.setStyleSheet("color: #c07000;")
        self.error_label.setVisible(False)

        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))
        self.editor.textChanged.connect(self._validate)
        self._validate()

    def __init_layout(self) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        layout.addWidget(self.editor)
        layout.addWidget(self.error_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(420)

    def _first_bad_line(self) -> tuple[int, str] | None:
        """逐行预校验：返回第一条坏行的 (行号, 原文)，全部合法返回 None。

        行号按编辑器里的自然行数（1 起，空行占位）—— 用户据此回编辑器找行。
        """
        for line_no, line in enumerate(self.editor.toPlainText().splitlines(), 1):
            text = line.strip()
            if not text:
                continue
            try:
                ipaddress.ip_address(text)
            except ValueError:
                return line_no, text
        return None

    def _validate(self) -> None:
        bad = self._first_bad_line()
        if bad is None:
            self.error_label.setVisible(False)
            self.yesButton.setEnabled(True)
        else:
            line_no, text = bad
            self.error_label.setText(
                self.tr("第 {} 行不是合法的 IP 地址：{}").format(line_no, text)
            )
            self.error_label.setVisible(True)
            self.yesButton.setEnabled(False)

    def get_servers(self) -> list[str]:
        return [
            line.strip()
            for line in self.editor.toPlainText().splitlines()
            if line.strip()
        ]


class SettingsInterface(ScrollArea):
    def __init__(
        self,
        parent: "MainWindow | None" = None,
        *,
        mitm: "MitmFacade | None" = None,
    ) -> None:
        super().__init__(parent)
        self._mitm = mitm
        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)

        self.setting_label = TitleLabel(self)
        self.setting_label.setText(self.tr("设置"))

        self.update_controller = UpdateController(self)
        self._update_dialog: UpdateDialog | None = None
        # 本次检查是不是「启动自动检查」：自动入口失败/无更新一律静默。
        self._auto_check = False

        # 关于与更新（.plans/3-auto-update.md §2）：版本展示 + 手动检查入口 +
        # 自动检查开关。关于类信息习惯置顶，故排在「个性化」之前。
        self.about_group = SettingCardGroup(
            title=self.tr("关于与更新"), parent=self.scroll_widget
        )
        self.version_card = SettingCard(
            FluentIcon.INFO,
            self.tr("当前版本"),
            f"v{update_core.current_version()}",
            parent=self.about_group,
        )
        self.update_card = PushSettingCard(
            self.tr("检查"),
            FluentIcon.UPDATE,
            self.tr("检查更新"),
            self.tr("检查 GitHub 上是否有新版本"),
            parent=self.about_group,
        )
        self.auto_update_card = SwitchSettingCard(
            FluentIcon.SYNC,
            self.tr("自动检查更新"),
            self.tr("启动后在后台静默检查，发现新版本才提示"),
            configItem=CONFIG.auto_check_update,
            parent=self.about_group,
        )
        # 开发态 / 便携版不支持原地更新（Velopack 物理限制，见 core/update.py
        # docstring）：手动入口降级为「去发布页下载」。
        if not update_core.update_supported():
            self.update_card.button.setText(self.tr("发布页"))
            self.update_card.setContent(
                self.tr("便携版 / 开发环境不支持原地更新，请前往发布页下载")
            )
            self.auto_update_card.setEnabled(False)

        # 分组
        self.personalization_group = SettingCardGroup(
            title=self.tr("个性化"), parent=self.scroll_widget
        )

        self.theme_card = OptionsSettingCard(
            configItem=CONFIG.themeMode,
            icon=FluentIcon.BRUSH,
            title=self.tr("应用主题"),
            content=self.tr("自定义应用外观"),
            texts=[self.tr("浅色"), self.tr("深色"), self.tr("使用系统设置")],
            parent=self.personalization_group,
        )
        self.theme_color_card = CustomColorSettingCard(
            configItem=CONFIG.themeColor,
            icon=FluentIcon.PALETTE,
            title=self.tr("主题颜色"),
            content=self.tr("更改应用的主题颜色"),
            parent=self.personalization_group,
        )
        self.zoom_card = OptionsSettingCard(
            configItem=CONFIG.dpi_scale,
            icon=FluentIcon.ZOOM,
            title=self.tr("界面缩放"),
            content=self.tr("调整控件和字体的大小"),
            texts=[
                "100%",
                "125%",
                "150%",
                "175%",
                "200%",
                self.tr("使用系统设置"),
            ],
            parent=self.personalization_group,
        )
        self.language_card = ComboBoxSettingCard(
            configItem=CONFIG.language,
            icon=FluentIcon.LANGUAGE,
            title=self.tr("语言"),
            content=self.tr("选择界面所使用的语言"),
            # 语言名一律用**该语言自己的写法**，所以这两条不进翻译目录 —— 看不懂当前
            # 界面语言的人，也得能在这里认出自己的语言（Windows 设置同样这么做）。
            texts=["简体中文", "English"],
            parent=self.personalization_group,
        )

        # Main Panel
        self.main_panel_group = SettingCardGroup(self.tr("主面板"), self.scroll_widget)
        self.minimize_to_tray_card = SwitchSettingCard(
            FluentIcon.MINIMIZE,
            self.tr("关闭后最小化至托盘"),
            self.tr("应用程序将继续在后台运行"),
            configItem=CONFIG.minimize_to_tray,
            parent=self.main_panel_group,
        )
        self.layout_card = ComboBoxSettingCard(
            configItem=CONFIG.layout,
            icon=FluentIcon.LAYOUT,
            title=self.tr("布局"),
            content=self.tr("切换表格信息中详细面板布局"),
            texts=[self.tr("水平"), self.tr("垂直")],
            parent=self.main_panel_group,
        )
        # 固定会话（plans/sticky-session.md）：全局行为偏好，不是规则 —— 刻意放
        # 设置页主面板而不是重写页。默认关：开着时实时流量表见到的请求头已含
        # 代理补回的 Cookie / Authorization，抓包就不再是「如实转发原件」。
        self.sticky_session_card = SwitchSettingCard(
            FluentIcon.FINGERPRINT,
            self.tr("固定会话"),
            self.tr(
                "固化 Cookie 与认证头：跨连接复用客户端会话不丢；"
                "仅补发给服务器的 Cookie/Auth，不改变服务器行为、不写请求参数"
            ),
            configItem=CONFIG.sticky_session_enabled,
            parent=self.main_panel_group,
        )
        # 无缓存·明文（.plans/capture-preferences-page.md）：原生 anticache +
        # anticomp 合成一个开关、同开同关。默认关：开着会改写请求头（删条件缓存
        # 头 + 改 Accept-Encoding=identity），抓到的就不是客户端原件。
        self.anticache_plaintext_card = SwitchSettingCard(
            FluentIcon.CLEAR_SELECTION,
            self.tr("无缓存 · 看明文"),
            self.tr(
                "删除条件缓存头强制服务器回最新内容，并要求明文响应不解压；"
                "会改写抓到的原始请求头"
            ),
            configItem=CONFIG.anticache_plaintext,
            parent=self.main_panel_group,
        )
        # 协议层两开关（.plans/2-protocol-switches.md）：同为「代理行为偏好」，
        # 排在无缓存·明文之后。默认开（原生出厂姿态）；关掉是调试降级手段，
        # 不改「如实转发」语义。h2c（明文升级）恒被剥离、与 http2 开关无关，
        # alt-svc 已缓存的客户端关掉 h3 后可能先试一次再回落 —— 文案都写明。
        self.http2_card = SwitchSettingCard(
            FluentIcon.CONNECT,
            self.tr("HTTP/2 支持"),
            self.tr(
                "关闭后 TLS 连接一律降级为 HTTP/1.1，报文按行可读；"
                "明文 h2c 升级本就不支持、恒被剥离"
            ),
            configItem=CONFIG.http2_enabled,
            parent=self.main_panel_group,
        )
        self.http3_card = SwitchSettingCard(
            FluentIcon.CLOUD,
            self.tr("HTTP/3 (QUIC) 支持"),
            self.tr(
                "关闭后客户端回落 HTTP/2 (TCP)，解决 QUIC/UDP 流量抓不到的问题；"
                "已缓存的 alt-svc 可能先试一次再回落"
            ),
            configItem=CONFIG.http3_enabled,
            parent=self.main_panel_group,
        )
        # DNS 解析（.plans/dns-options.md）：同为「代理行为偏好」，故在主面板组尾。
        # 卡 1 是「查看 + 编辑」入口，content 动态反映当前状态（见
        # _refresh_dns_servers_content）；卡 2 绑 configItem 自动落盘。
        # 两卡都只对 WireGuard 隧道内的 DNS 生效 —— regular 模式下客户端自解
        # DNS，选项管不到（无副作用，故常驻、不让路），文案必须写明防误报 bug。
        self.dns_servers_card = PushSettingCard(
            self.tr("编辑"),
            FluentIcon.GLOBE,
            self.tr("自定义 DNS 服务器"),
            self.tr("仅对 WireGuard 隧道内的域名解析生效；留空使用系统 DNS"),
            parent=self.main_panel_group,
        )
        self.dns_use_hosts_card = SwitchSettingCard(
            FluentIcon.DICTIONARY,
            self.tr("解析时查询 hosts 文件"),
            self.tr(
                "隧道内 DNS 应答先查本机 hosts，写一条即可把域名指向测试机"
                "（需管理员编辑系统 hosts 文件，且会影响本机自身解析）"
            ),
            configItem=CONFIG.dns_use_hosts_file,
            parent=self.main_panel_group,
        )
        self._refresh_dns_servers_content()

        self.__init_widget()

    def __init_widget(self):
        # self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        self.enableTransparentBackground()
        self.setSmoothMode(
            SmoothMode.NO_SMOOTH, Qt.Orientation.Vertical
        )  # 关闭平滑滚动，避免晃眼
        self.setObjectName("settingInterface")

        # initialize style sheet
        self.scroll_widget.setObjectName("scrollWidget")
        self.setting_label.setObjectName("settingLabel")

        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_layout(self):
        self.setting_label.move(36, 30)

        self.about_group.addSettingCard(self.version_card)
        self.about_group.addSettingCard(self.update_card)
        self.about_group.addSettingCard(self.auto_update_card)

        self.personalization_group.addSettingCard(self.theme_card)
        self.personalization_group.addSettingCard(self.theme_color_card)
        self.personalization_group.addSettingCard(self.zoom_card)
        self.personalization_group.addSettingCard(self.language_card)

        self.main_panel_group.addSettingCard(self.minimize_to_tray_card)
        self.main_panel_group.addSettingCard(self.layout_card)
        self.main_panel_group.addSettingCard(self.sticky_session_card)
        self.main_panel_group.addSettingCard(self.anticache_plaintext_card)
        self.main_panel_group.addSettingCard(self.http2_card)
        self.main_panel_group.addSettingCard(self.http3_card)
        self.main_panel_group.addSettingCard(self.dns_servers_card)
        self.main_panel_group.addSettingCard(self.dns_use_hosts_card)

        self.expand_layout.setSpacing(28)
        self.expand_layout.setContentsMargins(36, 10, 36, 0)
        self.expand_layout.addWidget(self.about_group)
        self.expand_layout.addWidget(self.personalization_group)
        self.expand_layout.addWidget(self.main_panel_group)

    def __connect_signal_to_slot(self):
        CONFIG.appRestartSig.connect(self.__show_restart_tooltip)
        CONFIG.themeChanged.connect(setTheme)
        CONFIG.themeColorChanged.connect(setThemeColor)
        # 开关翻转时卡片自己会把配置落盘，这里只负责把新值热更进内核。
        # 接 valueChanged 而不是卡片的 checkedChanged：配置项是唯一事实源，
        # 程序化改值（以后若有）也走同一条下发路。
        CONFIG.sticky_session_enabled.valueChanged.connect(
            self.__on_sticky_session_changed
        )
        CONFIG.anticache_plaintext.valueChanged.connect(
            self.__on_anticache_plaintext_changed
        )
        # 协议层两开关：任一变动都整体重推两项（快照原子，与固定会话同一条路）。
        CONFIG.http2_enabled.valueChanged.connect(self.__on_protocol_changed)
        CONFIG.http3_enabled.valueChanged.connect(self.__on_protocol_changed)
        # DNS：hosts 开关照 sticky 模式（valueChanged 热更，失败静默）；NS 列表
        # 走对话框提交链（校验通过才落盘，见 __on_dns_servers_clicked）。
        CONFIG.dns_use_hosts_file.valueChanged.connect(self.__on_dns_use_hosts_changed)
        self.dns_servers_card.clicked.connect(self.__on_dns_servers_clicked)

        # 更新链（.plans/3-auto-update.md §2/§3）：卡片入口 → controller 编排 →
        # 信号回来驱动卡片文案与 UpdateDialog 三态。
        self.update_card.clicked.connect(self.__on_update_card_clicked)
        self.update_controller.check_started.connect(self.__on_check_started)
        self.update_controller.check_finished.connect(self.__on_check_finished)
        self.update_controller.no_update.connect(self.__on_no_update)
        self.update_controller.check_failed.connect(self.__on_check_failed)
        self.update_controller.update_available.connect(self.__on_update_available)
        self.update_controller.download_failed.connect(self.__on_download_failed)
        self.update_controller.apply_failed.connect(self.__on_download_failed)

    @Slot(bool)
    def __on_sticky_session_changed(self, enabled: bool) -> None:
        """把固定会话开关热更进内核；失败静默。

        内核没跑时 `set_sticky_session` 只对齐内存副本（下次启动的种子会读到
        它），不会抛错；运行中下发失败（超时等）也不回拨开关 —— 开关已落盘，
        回拨反而让「配置说了什么」和「界面显示什么」分家，重开内核会按落盘值
        重放。
        """
        if self._mitm is None:
            return
        try:
            self._mitm.set_sticky_session(enabled)
        except (ValueError, RuntimeError, TimeoutError):
            pass

    @Slot(bool)
    def __on_anticache_plaintext_changed(self, enabled: bool) -> None:
        """把无缓存·明文开关热更进内核；失败静默（语义同固定会话那条）。"""
        if self._mitm is None:
            return
        try:
            self._mitm.set_anticache_plaintext(enabled)
        except (ValueError, RuntimeError, TimeoutError):
            pass

    @Slot(bool)
    def __on_protocol_changed(self, _enabled: bool) -> None:
        """把协议层开关热更进内核；失败静默（语义同固定会话那条）。

        两个开关各发各的 valueChanged，这里整体重推两项 —— 快照是原子的，
        分两条通道推只会多一次跨线程往返，还可能留下「h2 新的、h3 旧的」。
        """
        if self._mitm is None:
            return
        try:
            self._mitm.set_protocol_options(
                http2=bool(CONFIG.get(CONFIG.http2_enabled)),
                http3=bool(CONFIG.get(CONFIG.http3_enabled)),
            )
        except (ValueError, RuntimeError, TimeoutError):
            pass

    def _current_dns_servers(self) -> list[str]:
        """当前自定义 DNS：优先内核内存副本（运行中热更后的真值），否则落盘值。"""
        if self._mitm is not None:
            return self._mitm.dns_name_servers
        return list(CONFIG.get(CONFIG.dns_name_servers))

    def _refresh_dns_servers_content(self) -> None:
        """卡 1 的 content 动态反映当前状态 —— 它是「查看 + 编辑」入口。"""
        servers = self._current_dns_servers()
        if servers:
            self.dns_servers_card.setContent(
                self.tr("已设 {} 台：仅隧道内生效").format(len(servers))
            )
        else:
            self.dns_servers_card.setContent(
                self.tr("仅对 WireGuard 隧道内的域名解析生效；留空使用系统 DNS")
            )

    @Slot(bool)
    def __on_dns_use_hosts_changed(self, enabled: bool) -> None:
        """把 hosts 查询开关热更进内核；失败静默（语义同固定会话那条）。"""
        if self._mitm is None:
            return
        try:
            self._mitm.set_dns_options(use_hosts_file=enabled)
        except (ValueError, RuntimeError, TimeoutError):
            pass

    @Slot()
    def __on_dns_servers_clicked(self) -> None:
        """NS 编辑对话框的提交链：先热更（内含校验，坏值不落盘），成功后落盘。"""
        dialog = DnsServersDialog(self._current_dns_servers(), self.window())
        if not dialog.exec():
            return
        servers = dialog.get_servers()
        try:
            if self._mitm is not None:
                self._mitm.set_dns_options(name_servers=servers)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            show_warning(self.tr("DNS 设置未生效"), str(exc), self.window())
            return
        # 必须传新 list：原地 mutate 再 set 静默不落盘（见 core/settings.py 的坑）。
        CONFIG.set(CONFIG.dns_name_servers, list(servers))
        self._refresh_dns_servers_content()

    # ── 应用内更新（.plans/3-auto-update.md §2/§3）──

    def check_updates_auto(self) -> None:
        """启动自动检查入口：开关与形态闸门都收在这里，主窗口只管定时触发。"""
        if not CONFIG.get(CONFIG.auto_check_update):
            return
        if not update_core.update_supported():
            return
        self._auto_check = True
        self.update_controller.check()

    @Slot()
    def __on_update_card_clicked(self) -> None:
        # 开发态 / 便携版的降级语义：原地更新不可用，就把人带去发布页。
        if not update_core.update_supported():
            QDesktopServices.openUrl(QUrl(f"{REPO_URL}/releases"))
            return
        self._auto_check = False
        self.update_controller.check()

    @Slot()
    def __on_check_started(self) -> None:
        self.update_card.button.setEnabled(False)
        if update_core.update_supported():
            self.update_card.setContent(self.tr("正在检查更新…"))

    @Slot()
    def __on_check_finished(self) -> None:
        self.update_card.button.setEnabled(True)
        if update_core.update_supported():
            self.update_card.setContent(self.tr("检查 GitHub 上是否有新版本"))

    @Slot()
    def __on_no_update(self) -> None:
        # 自动入口静默；手动点了按钮就必须给个交代。
        if not self._auto_check:
            show_success("", self.tr("当前已是最新版本"), self.window())

    @Slot(str)
    def __on_check_failed(self, message: str) -> None:
        # 同上：自动入口失败只进日志，不弹界面。
        if not self._auto_check:
            show_warning(self.tr("检查更新失败"), message, self.window())

    @Slot(object, object)
    def __on_update_available(self, info: object, brief: "UpdateBrief") -> None:
        """发现新版本：自动/手动入口都弹同一个三态对话框。"""
        dialog = UpdateDialog(brief, self.window())
        self._update_dialog = dialog
        dialog.download_requested.connect(lambda: self.__start_download(dialog, info))
        self.update_controller.download_progress.connect(dialog.set_progress)
        self.update_controller.download_finished.connect(dialog.set_ready)
        # accepted 且 ready = 用户点了「重启应用」；其余收场（稍后 / Esc）都不动。
        if dialog.exec() and dialog.ready:
            self.update_controller.apply_and_restart(info)
        self.update_controller.download_progress.disconnect(dialog.set_progress)
        self.update_controller.download_finished.disconnect(dialog.set_ready)
        self._update_dialog = None

    def __start_download(self, dialog: UpdateDialog, info: object) -> None:
        dialog.set_downloading()
        self.update_controller.download(info)

    @Slot(str)
    def __on_download_failed(self, message: str) -> None:
        # 下载/应用失败的展示位在对话框上；框已关（Esc 走了）就静默。
        if self._update_dialog is not None:
            self._update_dialog.set_failed(message)

    @Slot()
    def __show_restart_tooltip(self):
        """show restart tooltip"""
        InfoBar.warning(
            title="",
            content=self.tr("配置将在重启后生效"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.BOTTOM,
            duration=3000,
            parent=self.window(),
        )
