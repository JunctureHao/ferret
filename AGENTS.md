# AGENTS.md — Ferret 开发约定

基于 **PySide6 + QFluentWidgets + mitmproxy** 的桌面 HTTP/HTTPS 流量抓包工具。
改动代码前必须先遵守本文件；与代码冲突时以代码为准并同步更新本文件。

## 1. 技术栈

- **Python 3.12**（pyproject `requires-python = "==3.12.13"`），包管理用 **uv**。
- **GUI**：`PySide6==6.10.3` + `pyside6-fluent-widgets==1.11.3`。
  - 图标用 `from qfluentwidgets import FluentIcon`。
  - 主题兼容深浅色，用 `from qfluentwidgets import isDarkTheme`。
  - 控件优先 QFluentWidgets，不退回原生 Qt 样式。
  - 语法高亮走自写 `apps/common/edit/syntax.py`（已替掉 pygments），不引 pygments。
- **抓包内核**：`mitmproxy` 作为库嵌入（版本 `>=12.2.3`）。
- **代码门禁**（提交前必须绿）：`ruff check .` + `ty check`（临时装 使用uvx）。
  - 只格式化**自己改动的文件**，禁止全量 `ruff format .`。
  - ruff 忽略用 `# noqa: CODE`；ty 用 `# ty: ignore[rule]`；保留 `from __future__ import annotations`。
  - 本机在抓包时跑 ruff/ty 需加 `--system-certs`。
- **测试**：`tests/`，`python -m unittest discover -s tests`。碰 Qt 的测试文件 import PySide6 前设 `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")`。
- **提交信息**：`<type>(<scope>): <subject>`，type ∈ `feat/fix/docs/style/refactor/perf/test/build/ci/chore/revert`，scope ∈ `core/mitm/apps/utils`。
- **打包**：Nuitka，指令与瘦身项见 `src/ferret/__main__.py` 顶部 `# nuitka-project:` 注释。

## 2. 原生能力优先（勿重复造轮子）

以下一律用 mitmproxy 原生，不要自己实现：
- Cookie / query：`flow.request.cookies` / `.query`（勿手拆 header）。
- 解码 body：`message.get_text(strict=False)` / `get_content(strict=False)`（**别用 `.text`/`.content`**，畸形编码会抛 `ValueError`）。
- body 视图：`contentviews.prettify_message(message, flow)`。注意输出过 `escape_control_characters`；`syntax_highlight` 无 `json` 值（JSON 自报 `yaml`），见 `apps/common/flow/views.py::_body_lang`。
- 字节大小：`human.pretty_size`（不自造 `format_bytes`）。
- HAR 导出：`SaveHar().make_har`（纯函数）。
- curl/httpie/raw 导出：`mitmproxy.addons.export` 模块级函数；唯一分叉 `core/mitm/export.py::curl_command`（Windows 引号），不要改回原生。
- 屏蔽：`BlockList` + `parse_spec`；重写 URL：`MapRemote` + `parse_map_remote_spec`；代理来源限制：`Block`。ferret 只造 spec 字符串，经 `options.update(...)` 下发（addons.add 前选项不存在）。
- CA：`certs.CertStore.from_store` / `Cert` 字段 / `Cert.to_pem()`；系统信任库只走 Windows `certutil`。

## 3. 桥接红线（违反会崩溃/数据错乱）

mitmproxy Master 在独立 asyncio 线程，Qt 在主线程。合法通道只有三条：
1. `MitmRuntime.call(callback, timeout=5.0)` 投到 mitm 线程。
2. Qt 侧一律经 `MitmFacade`（`apps/` 只持 facade，不直接调 `runtime.call`）。
3. 事件经 `_ViewSignalBridge` 转 Qt Signal，**不要自己 poll View**。

禁止项：
- ❌ 在 Qt 线程直接读写 flow/master/view。要快照走 `facade._snapshot()`（见 `MitmFacade.all_http_flows`/`intercepted_flows`），**不要**直接用 `flow.copy()`：原生 `Serializable.copy()` 会换掉 `flow.id`，而界面回头找真流量（`release_flows` / `apply_request_edits` / `save_flows`）全靠这个 id。`apps/common/flow/models.py` 的 `set_view`/`handle_refresh`/`clear_data`/`remove_row` 是已知违反，应改走 facade 的 `clear_flows()`/`remove_flows()`。
- ❌ 使用 `ctx`。需 master/options 用手上的 `runtime.master`。`ctx` 不进 `bindings.__all__`。
- ❌ 跨层 import mitmproxy。`from mitmproxy import ...` 只允许在 `core/mitm/bindings.py`；`core/mitm/*` 内 `from ...bindings import`，其余 `from ferret.core.mitm import`。
- ❌ 向 master 追加 mitmproxy 命令行 addon（comment/cut/export/script 等），GUI 自行实现等效能力。
- ❌ 手动改 `Content-Length`——改 `flow.request.content`/`response.content` 后 mitmproxy 自动重算。
- 三个地址不可混用：`listen_host`（bind 用）/ 本机接入恒 `127.0.0.1`（`MitmFacade.local_client_host`）/ 局域网展示地址（`detect_lan_address()`，只显示不写配置）。系统代理只取 `127.0.0.1`。

## 4. 目录分层

- `core/`：`application.py`/`runtime.py`(`AppRuntime`)/`settings.py`/`network.py`(地址词表+局域网探测，无 Qt 无 mitmproxy)/`log.py`/`system_proxy/`(注册表代理)/`resources_rc.py`(勿手改)。
- `core/mitm/`：`bindings`(唯一 mitmproxy 入口)/`master`/`runtime`/`facade`/`addons`(`FerretTlsConfig`/`LogAddon`)/`export`/`io`/`certificate`(同步阻塞，调用方负责挪后台线程)/`blocklist`/`rewrite`/`intercept`(断点规则+报文写回，规则不带 `~q`/`~s`，两个钩子共用一个过滤器)/`compose`(手工发送：`build_compose_flow` 从零造 flow 走 `ClientPlayback`，`ComposeAddon` 在 response/error 钩子里报结果并按 `record` 决定是否从 View 摘除)/`__init__`(公开 API)。`engine.py` 零引用可删。**会送到界面的异常文案**（`certificate`/`facade`/`gateway`/`intercept`/`rewrite`/`runtime`）用 `QCoreApplication.translate("<Ctx>", ...)` 包一层，所以这几个模块 import QtCore（不碰控件）；日志与 `from_dict` 校验消息不译（后者被 `rules_from_raw` 吞掉，从不上界面）。
- `apps/`：`capture`/`certificate`/`common`/`session`/`settings`/`blocklist`/`rewrite`/`intercept`（断点页只留规则；队列与请求/响应编辑器在独立的非模态窗口 `intercept/window.py`，构造时 parent 必须为 None，否则 `qframelesswindow` 不补 `Qt.Window` 会退化成子控件）/`compose`（手工请求编辑页：method/URL/参数/请求头/请求体 → `MitmFacade.send_custom_request`，结果经 `MitmRuntime.compose_result` 信号回来直接消费 `build_flow_detail` 字典），**不直接 import mitmproxy 内部模块**。后台任务统一用 `apps/common/tasks.py::FunctionTask`。编辑类 UI 复用 `apps/common/edit/`（`ItemDualPanel`/`ToolPlainTextEdit`/`JsonDualPanel`），不新造编辑器。
- `utils/`：`http_parser.py`(body 预处理)/`scripts.py`(i18n 流水线，subprocess)/`i18n.py`(`QT_TRANSLATE_NOOP` 标记 + `resolve_marker`，见 §8)。新增 utils 不再加依赖（现 `http_parser.py` 已误引 `core.mitm.bindings`，别扩散）。

## 5. 技术决策（勿推翻）

- 正向代理模式（监听端口 + 系统代理注册表），不用透明代理。
- 不引入 mitmproxy_rs 的 `certs`(Win/Linux 未实现)/`process_info`(透明代理用)/`syntax_highlight`(比自写 lexer 粗)。
- 已删除勿复活：顶层 `application/` 包、`utils/proxy_manager.py`、自造 `format_bytes`/`compute_folds`/`mime_of`。

## 6. 功能状态

实际装载 addon（`core/mitm/master.py` 为准）：Core、Block、StripDnsHttpsRecords、AntiCache(关)、AntiComp(关)、ClientPlayback、DisableH2C、Proxyserver、DnsResolver、GatewayL4Addon、NextLayer、MapRemote、MapLocal、ModifyBody、ModifyHeaders、FerretTlsConfig、GatewayL7Addon、FerretIntercept、View、ComposeAddon(挂在 View 之后，摘除靠 `loop.call_soon` 排到当轮钩子之后)、ReadFile、Save、LogAddon。BlockList 已撤（网关取代，见 `master.py` 注释）。

已实现：正向代理抓包、client_playback 重放、`.flow` 读写、HAR/curl/httpie/raw 导出、CA 证书页、系统代理开关、会话管理、屏蔽 blocklist、代理来源限制 block、网关（L4/L7 策略）、重写六类（mapremote/maplocal/modifyheaders×2/modifybody×2）、断点 intercept（命中即在请求期与响应期各停一次，规则不选阶段；改请求或响应→放行/丢弃/伪造响应/撤销）、WebSocket 逐帧展示、SSE 事件分行展示、流量标记与备注、手工请求 compose（编辑页自己拼请求经内核发出，重写/网关/断点规则照常命中；「进入流量列表」可选，不选的等 replay 结束后从 View 摘除）。

缺口：serverplayback、stickycookie/stickyauth、SSE 实时推送（见下）。

WebSocket：帧不走 addon，走 `mitmproxy.proxy.layers.websocket`（`layers/__init__.py`
顶层就 import 它，连带 `wsproto`），经 `UiBridgeAddon` 的 `websocket_start` /
`websocket_message` / `websocket_end` 三个钩子转成 Qt 信号实时到界面，详情页「消息」
逐帧展示。信号只送 `flow_id` + 值对象，界面再回头问一趟 `websocket_frames()` ——
一条行情连接上千帧，跟着选中一起搬过界不划算。显示上限 `WS_FRAME_LIMIT`（界面策略，
`ws_frames()` 本身恒返回全部）。

SSE：响应体一律缓冲 —— `stream_large_bodies` 默认 `None`（`proxyserver` 声明的），
ferret 不设它，也不碰 `flow.response.stream`。所以事件表读的是**已结束**的响应体，
端点不收尾就一直看不到，这正是原生 `ServerSideEvents` addon（内容只有一条告警，
ferret 没装）在说的 mitmproxy#4469。将来要做实时推送，`stream_large_bodies` 必须配
`store_streamed_bodies`：只开前者的话 body 压根不入库，事件表反而更空。解析在
`utils/sse.py`（纯函数、零依赖），不在 `core/mitm/`。

⚠️ `README.md` 的「内置 Addon 对照」表已过期，以 `master.py` 为准。

## 7. Nuitka 打包（发布/大改动前）

- 瘦身项统一维护在 `src/ferret/__main__.py` 的 `# nuitka-project:` 注释；打包 `nuitka .\src\ferret\`（目录，非单文件）。
- `bindings._STUBBED_MODULES` 现有 11 桩，须在 mitmproxy 导入前完成：
  - `mitmproxy.addons.{onboarding,onboardingapp,proxyauth,cut}` + `pyperclip`（原有）。
  - `mitmproxy.addons.{browser,command_history,comment,termlog}` —— 都是 mitmproxy 自家命令行界面用的。`termlog` 的桩**必须**带 `TermLog` 属性：`mitmproxy/master.py:25` 的类注解 `termlog.TermLog | None` 在导入期就求值。`comment` 只注册一条 `flow.comment` 控制台命令，`Flow.comment` 属性本身在 `mitmproxy/flow.py` 上，ferret 在 `facade.py` 直接赋值，不经过它。
  - `werkzeug` + `werkzeug.security{safe_join}` —— mitmproxy 全包只有 `maplocal.py:9` 用了 `safe_join` 一个函数，却拖进 34 个 werkzeug 子模块加它独占的 colorama / markupsafe（约 7 MB obj / 3.2 MB exe）。`bindings._safe_join` 是照搬上游的等价实现（BSD-3, © Pallets）；这是**目录穿越安全边界**，升级 mitmproxy / werkzeug 后要比对上游 `werkzeug/security.py` 的 `safe_join` 有没有变，别自己发挥。colorama / markupsafe 只被 werkzeug 引用，不必单独立桩。
  - `maplocal` 已解桩（重写页的「重定向（本地）」要用它），别再加回去。`script` 也别桩：脚本功能后续要做。
- **标准库字节码 blob**：standalone 默认把「没被排除的那部分标准库」整批塞进 `__bytecode.const`（389 个模块 / 5.58 MiB，**不**计入 report.xml 的 obj 体积，容易看漏）。该 blob 几乎不压缩（input 5,882,087 → blob 5,854,988），所以在这里省下的字节是 **1:1** 落到 exe 上的 —— 比编译模块的 obj→exe 边际折算（实测 **0.345**，不是全局 0.473）划得多。现已排掉 40 棵零引用者子树（~695 KiB）。
  - 判定方法：拿 report.xml 的 `<module_usages>` 反查引用者。注意 Nuitka 会把整个标准库都记在 `__main__` 名下（伪引用），**必须把 `__main__` 剔掉再看剩下几个**；`reason` 字段不能当依据（`typing` 也写着「non-excluded parts of standard library」）。
  - 刻意留着的两个：`_sitebuiltins`（site.py 启动就加载）、`_pylong`（CPython C 层在超大整数 ↔ 字符串转换时自己 import，静态图里看不见）。`encodings`（122 模块 / 535 KiB）也不能动 —— codecs 按名动态查表，而拓包要解任意 `Content-Type` 的 charset。
- `pyparsing.diagram`（269.5 KiB obj）已排：`pyparsing/core.py:2567` 的 `from .diagram import ...` 在 `create_diagram()` 函数体里，外面就套着 `except ImportError`，而 `railroad` 本来就没装 —— 这条路运行期必然失败并被吞掉。（`pyparsing.testing` 不用管，anti-bloat 插件已经帮忙剔了，连 `unittest` 都没进包。）
- 编译侧已挖尽：除 ferret 自身外，全图只剩 6 个零引用者编译模块（0.21 MiB），全是 Nuitka/PySide6 的 pre/postLoad 脚手架与 `mitmproxy_windows`（透明代理要用）。三个看着像胖子的都已验证是**活的**：`ruamel`（6.55 MiB）是 6 个 contentview 的渲染后端（`contentviews/_utils.py::yaml_dumps`）；`pyparsing`（4.79 MiB）是 `flowfilter` 的词法底层；`service_identity` → `pyasn1` + `attr`（6.43 MiB）是 `aioquic.tls.verify_certificate` 的证书校验链，`tlsconfig.py:436` 默认就是 `CERT_REQUIRED`。
- 接新 mitmproxy addon / 第三方依赖时，先确认是否会被 Nuitka 误裁，必要时加 `--include-package` 或移除对应 `--nofollow`。勿裁 `pyasn1`(aioquic 硬链)、`ruamel.yaml`、`mitmproxy_rs.contentviews`、aioquic/pylsqpack、`wsproto`(WebSocket 层顶层 import，帧展示要用)。
- 打包后冒烟：exe 能起、GUI 不崩、mitmproxy master 正常 listen。

## 8. i18n（英文源 + `zh_CN.qm`）

- **源语言是英文**：所有 `tr()` / `translate()` 的字面量写英文，中文只存在于 `src/ferret/resources/i18n/zh_CN.ts`。默认语言仍是简体中文（`core/settings.py`）。选 English 时**不装业务翻译器**（源文本即英文），仓库里没有也不需要 `en_GB.qm`。
- 改完任何文案跑 `uv run python -m ferret.utils.scripts`：lupdate → lrelease → rcc 一条链。少跑一步不会报错，界面只是静默退回英文 —— `tests/core/test_i18n.py` 就是为此立的守卫（代码字面量与 `.ts` 双向对齐、`.ts` 逐条与编译后的 `.qm` 一致、`<location>` 指向真实文件）。
- **lupdate 必须排除 `core/resources_rc.py`**：3.6 MB 生成物会让它以 `0xC0000409`(STACK_BUFFER_OVERRUN) 崩掉。历史上目录停更半年就是这么来的。
- **rcc 输出必须落在 `core/resources_rc.py`**：应用 import 的是 `ferret.core.resources_rc`，写到别处等于没编。
- rcc 会把源文件的 mtime 编进资源表，所以每跑一次流水线，`core/resources_rc.py` 都会有几字节的时间戳差异（3.8 MB 文件的 diff 看着吓人，实际只有那一处）。别为了「diff 干净」去手改这个文件。
- **模块级与类体（含 `ClassVar`）不得求值翻译**：`core/application.py` 顶层就 `from ferret.apps.window import MainWindow`，那一刻 `_init_i18n()` 还没装翻译器，求出来的文案会永久冻结成英文。这类表存 `QT_TRANSLATE_NOOP("Ctx", "text")` 标记，到使用点用 `QCoreApplication.translate` / `resolve_marker` 求值（`utils/i18n.py`；那里还重绑了 `QT_TRANSLATE_NOOP` 的签名，PySide6 的 stub 返回 `object` 会让 ty 报错）。
- **lupdate 是静态扫描**，两件事提取不到：**f-string 内部**（把文案整句留在外面、变量交给 `.format()`）、**`tr(变量)` / `translate(变量, ...)`**（context 与源文本都得是字面量；唯一例外是 `resolve_marker` 那个共用查表器，context 由调用方给）。
- **不要拼句**：`tr("{}失败").format(动作)` 换个语序就没法译，每个分支写整句（例：`core/mitm/certificate.py::_checked` 由调用方整句传入）。
- 日志（`logger.*`）与 `from_dict` 校验消息不译 —— 后者被 `rules_from_raw` 吞掉，从不上界面。**但 `core/` 里会经信号/异常上界面的报错要译**（例：`core/system_proxy/service.py` 的三条异常经 `CaptureController.last_error` 显示在捕获页的警告条上）。
- 语言名（`apps/settings/views.py` 的 `["简体中文", "English"]`）刻意不译：看不懂当前界面语言的人也得认出自己的语言。
