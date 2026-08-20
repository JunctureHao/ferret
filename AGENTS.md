# AGENTS.md — Ferret 开发约定

Ferret 是基于 **PySide6 + QFluentWidgets + mitmproxy** 的桌面 HTTP/HTTPS
流量抓包工具。本文件是 AI 协作的硬性约束，改动代码前必须先遵守。
文中每条事实都对应真实代码位置；与代码冲突时以代码为准，并顺手更新本文件。

## 1. 技术栈（必须使用的库/风格）

- **Python 3.12**（pyproject `requires-python = "==3.12.13"`），包管理用 **uv**。
- **GUI**：`PySide6==6.10.3` + `pyside6-fluent-widgets==1.11.3`。
  - 图标统一用 `from qfluentwidgets import FluentIcon`，如 `FluentIcon.CERTIFICATE`。
  - 主题感知用 `from qfluentwidgets import isDarkTheme`，颜色/高亮需兼容深浅主题
    （参考 `apps/common/edit/widgets.py` 的 `_apply_search_highlight`）。
  - 控件优先用 QFluentWidgets 提供的（TransparentToolButton / SimpleCardWidget /
    MessageBoxBase / Action 等），不要退回原生 Qt 控件样式。
- **抓包内核**：`mitmproxy`（作为库嵌入，版本 `>=12.2.3`）。

### 1.1 优先用 mitmproxy 原生能力（勿重复造轮子）

| 需求 | 用这个 | 落地位置 |
| --- | --- | --- |
| Cookie | `flow.request.cookies` / `flow.response.cookies`（勿手拆 header） | `apps/common/flow/models.py` |
| query | `flow.request.query` | 同上 |
| 解码 body | `message.get_text(strict=False)` / `get_content(strict=False)`（已处理 charset + 解压）。**别用 `.text` / `.content`**：编码或压缩畸形时它们抛 `ValueError` | `utils/http_parser.py` |
| body 美化 + 视图判定 | `contentviews.prettify_message(message, flow)`，一次拿到 JSON / XML / protobuf / gRPC / msgpack / multipart / urlencoded / GraphQL / hexdump 等全部原生视图，自带 fallback-to-raw 和异常兜底 | `utils/http_parser.py::build_body` |
| 字节大小格式化 | `human.pretty_size` → `11b` / `1.0k` / `3.0g`（不自造 `format_bytes`） | `apps/common/flow/{models,views}.py`、`apps/session/models.py` |
| HAR 导出 | `mitmproxy.addons.savehar.SaveHar().make_har`（纯函数，勿重写算法） | `core/mitm/export.py` |
| httpie / raw 导出 | `mitmproxy.addons.export` 的模块级函数 | `core/mitm/export.py` |
| 代理访问控制（按来源 IP 类别） | `mitmproxy.addons.block` 的 `Block`（`client_connected` 里读 `is_loopback` / `is_private` / `is_global` 三个 stdlib 属性，命中就 `client.error` 杀连接。**没有任何名单**，也不看目的地址） | `core/mitm/master.py`、`core/network.py` |
| 屏蔽请求 | `mitmproxy.addons.blocklist` 的 `BlockList` + `parse_spec`（匹配、`Response.make` 空响应、444 走 `flow.kill()` 全是原生的，ferret 只造 spec 字符串） | `core/mitm/blocklist.py` |
| 重写请求目标（URL 重定向） | `mitmproxy.addons.mapremote` 的 `MapRemote` + `parse_map_remote_spec`（正则匹配、`re.sub` 改写、`request.url` setter 连 Host 头与 scheme/端口一起更新，全是原生的，ferret 只造 spec 字符串） | `core/mitm/rewrite.py` |
| CA 证书：生成 / 查看 / 导出 | `certs.CertStore.from_store` → `create_store`（一次写全 6 个产物）、`Cert` 的现成字段（cn / subject / issuer / notbefore / notafter / has_expired / serial / fingerprint / keyinfo / is_ca）、`Cert.to_pem()`。**装进/移出系统信任库 mitmproxy 完全不管**，只能走系统命令（Windows `certutil`） | `core/mitm/certificate.py` |
| 构造请求/响应（暂无调用点，将来需要时） | `Request.make` / `Response.make` | — |

- **唯一允许的分叉**：`core/mitm/export.py::curl_command`。原生实现用 POSIX 单引号，
  Windows cmd 下不可用，故本地重写并加 `_to_windows_curl`。不要「修回」直接调原生。
- `prettify_message` 的两个坑：
  - 输出全部过 `strutils.escape_control_characters`（非 ASCII 保留，控制字符转义），
    body 显示文本与线上原始字节不完全一致，这是预期行为。
  - `syntax_highlight` 取值只有 `css / javascript / xml / yaml / none / error`，
    **没有 `json`** —— JSON 视图自报 `yaml`。所以不能按 `yaml` 直接判 JSON，
    否则 protobuf / msgpack / urlencoded / hexdump 会被喂进 JSON lexer。
    映射规则见 `apps/common/flow/views.py::_body_lang`。
- `block_list` spec（`<sep>flowfilter<sep>status`）的三个坑：
  - 分隔符是 `option[0]`，`rem.split(sep, 2)` 要求**恰好 2 段** —— URL 天然带 `/` 和 `:`，
    写死任一个都会切出 3 段抛 `ValueError`。`blocklist.py::_pick_separator` 按表达式内容
    动态挑一个未出现的字符，拼完**必须再过一遍原生 `parse_block_spec` 复核**。
  - `block_list` 选项由 `BlockList.load` 注册，`addons.add()` 之前**不存在** ——
    不能 `Options(block_list=…)`，只能 `FerretMaster(...)` 之后 `options.update(...)`。
  - 失败的 `options.update` 整体回滚（选项值与 `BlockList.items` 都停在上一个合法状态），
    所以 `specs_from_rules` 一次性校验全部规则，坏一条就整批不下发。
- `map_remote` spec（`<sep>[flowfilter<sep>]url-regex<sep>replacement`）的六个坑（已实测）：
  - 分隔符同样是 `option[0]`，但这次 `rem.split(sep, 2)` 接受 **2 段或 3 段**（3 段时
    第一段当 flowfilter）—— 段数不固定，比 `block_list` 更阴：分隔符出现在 replacement 里
    不抛异常，只是**静默读错**（实测 `parse_map_remote_spec("/foo/http://new.com/x")` →
    subject=`http:`、replacement=`/new.com/x`）。所以 `rewrite.py::_pick_separator` 动态挑字符，
    拼完过一遍原生解析器**还要回读比对两段是否原样** —— 只判「没抛异常」远远不够。
  - 原生解析器**只 `re.compile` subject，从不校验 replacement**：坏反向引用要等
    `MapRemote.request` 里那句 `re.sub` 才炸，异常直接窜出 addon 钩子（实测 `|foo|bar\1`
    过得了 `parse_map_remote_spec`，请求期抛 `re.error`）。`_validate_template` 拿空串预跑
    一次 `re.sub` 提前拦住 —— `re.sub` 是**预先**解析替换串的，模式不命中也照样报错。
    两种异常都要 catch：坏转义是 `re.error`，未知分组名（`\g<nope>`）是 `IndexError`。
  - **replacement 不能用 `re.escape`**：它是 `re.sub` 的替换串，唯一的元字符是反斜杠，
    翻倍就够（`escape_template`）；`re.escape` 会给 `/`、`.` 也加反斜杠，直接写进 URL。
  - **空 replacement 在钩子里抛 `ValueError: No hostname given`**（`request.url` 的 setter），
    所以 `RewriteRule.template` 直接拒空；`equals` 模式是整条 URL 替换，还额外要求带
    scheme + host 的完整 URL，缺一样同样是那个 setter 在真实流量上抛。
  - **`MapRemote.request` 的 for 循环没有 `break`**：命中的 spec 按顺序**逐条累积**改写
    同一条 URL（实测 `a.com→b.com` + `b.com→c.com` 两条会把 `https://a.com/x` 变成
    `https://c.com/x`）。所以规则**行序有语义**：规则页提供上移/下移，表格不开排序。
  - `map_remote` 选项与 `block_list` 同病：由 `MapRemote.load` 注册，`addons.add()` 之前
    **不存在**（实测 `"map_remote" in options` → add 前 False / 后 True）；`options.update`
    同样原子回滚，所以 `rewrite_option_updates` 一次性构造并校验全部规则，坏一条整批不下发，
    且**恒写全量列表** —— 规则删光要把选项写成 `[]`，否则内核继续用上一批 spec。
- `block_global` / `block_private` 的两个坑（已实测）：
  - 和 `block_list` 一样由 `Block.load` 注册，`addons.add()` 之前**不存在**
    （`registered before add? False` / `after add? True`），只能在 `FerretMaster(...)`
    之后 `options.update(...)`。
  - 它们是 bool 选项，**不走 `OptionsError`**：类型不对抛
    `TypeError: Expected <class 'bool'> for block_global, ...`，键名不对抛
    `KeyError: Unknown options: ...`。所以 `runtime.apply_block_options` 的回滚写成
    「先回滚内存副本，再原样抛」——只 catch `OptionsError` 那段代码永远不会执行。
- CA 证书的六个坑（`core/mitm/certificate.py`，已实测）：
  - **重新生成必须连 `{APP_NAME}-ca.pem`（私钥）一起删**：`from_store` 只要看到它就复用旧私钥，
    序列号与指纹都不变，「重新生成」等于空操作。整组产物见 `CA_ARTIFACTS`（6 个）。
  - **`certutil` 输出是本地化的**（本机打印中文），唯一可以分支的信号是**退出码**，
    绝不解析 stdout：查到 `0`，查不到 `0x80090011`。
  - **`certutil -delstore` 一条都没删到时也返回 `0`**，退出码判断不了「删没删掉」，
    必须每轮先 `-store` 复核（`uninstall()` 的循环，最多删 `_MAX_DELETE_ROUNDS` 张）。
  - **安装 / 卸载弹的 Windows 安全警告里点「否」，certutil 以 `0x800704c7`
    （`ERROR_CANCELLED` 包成的 HRESULT）退出**，那不是失败而是「没做」：单独抛
    `CertificateCancelled`（`CertificateError` 的子类），控制器把它翻成返回 `None`，
    界面只补一次检测、不弹错误提示。卸载的删除循环也靠这个异常跳出，
    否则同一个警告会被连弹 `_MAX_DELETE_ROUNDS` 次。退出码按 `& 0xFFFFFFFF`
    当无符号读 —— DWORD 在某些环境里是负数递过来的。
  - **信任状态按序列号判定，不能按 CN 子串**：同名旧 CA 会被误判成「已安装」，
    表现是界面显示已信任、浏览器照样报证书错误 —— 这就是 `TrustState.STALE`，
    必须单独出一档状态并提示重新安装。
  - 重新生成后**不必重启内核**：`options.update(confdir=…)` 触发原生
    `TlsConfig.configure({"confdir"})` → `CertStore.from_store`（`optmanager.update_known`
    对传入的每个键都发 `changed`，即使值没变）。见 `runtime.reload_certificate_store`。
    热加载失败只记日志：下次启动自然读到新证书。
- 超过 `MAX_PRETTY_SIZE`（1MB）跳过美化直接给 raw：mitmproxy 的
  `content_view_lines_cutoff` 需要配套「显示全部」按钮，ferret 暂无。

### 1.2 mitmproxy 导入必须走 bindings

- 所有 `from mitmproxy import ...` **只允许出现在 `core/mitm/bindings.py`**（当前 0 例外）。
- `core/mitm/*` 内部 → `from ferret.core.mitm.bindings import ...`
- 其余任何位置 → `from ferret.core.mitm import ...`（包 `__init__` 是稳定公开 API）
- 新增 addon / 原生类：先加进 `bindings.py` 并写入 `__all__`；需要给 `apps/` 用的，
  再在 `core/mitm/__init__.py` 转出一层。

## 2. 代码质量门禁（提交前必须通过）

- **ruff**（按默认规则，仓库无 `[tool.ruff]` 配置）：`ruff check .` 必须全绿；
  **自己改动的文件**必须过 `ruff format`。
  - ⚠️ 存量有 11 个文件不满足 `ruff format --check`：`apps/capture/views.py`、
    `apps/common/filter.py`、`apps/session/{controllers,services}.py`、
    `apps/settings/views.py`、`core/{settings,resources_rc}.py`、
    `core/system_proxy/backends.py`、`tests/apps/capture/test_toolbar.py`、
    `tests/apps/common/flow/test_views.py`、
    `tests/core/mitm/test_capture_master_client_playback.py`。
    **不要全量 `ruff format .`**，会混进大片与本次改动无关的 diff。
- **ty**：`ty check` 必须全绿（默认配置，仓库无 `[tool.ty]`，**不是** strict 模式）。
- ruff / ty 都不是声明依赖（pyproject 无 dev 组），临时装：`uv pip install ruff ty`。
  若本机存在 TLS 中间人（包括 ferret 自己正在抓包），需加 `--system-certs`，
  否则报 `invalid peer certificate: UnknownIssuer`。
- 忽略注释按工具区分，别混用：
  - ruff → `# noqa: BLE001`（带错误码，`src/` 现用法）
  - ty → `# ty: ignore[rule]`
  - mypy → `# type: ignore[code]`（**方括号是 mypy 的，不是 ruff 的**）
  - 现状：`src/` 用 `# noqa: <code>`，`tests/` 存在裸 `# type: ignore`。
- 需要前向引用时保留 `from __future__ import annotations`（删除会 `NameError`），
  现有 8 个文件依赖它，见 `core/mitm/runtime.py`、`core/mitm/facade.py`。
- **test**：测试在 `tests/`，基于 **unittest + `mitmproxy.test.tflow`**。
  - 运行：`python -m unittest discover -s tests`（当前 63 项全绿）。
  - 没有 conftest，`tests/__init__.py` 为空：**每个碰 Qt 的测试文件自己**在 import
    PySide6 之前写 `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")`。
    已知漏项：`tests/core/mitm/test_runtime.py` 建了 `QCoreApplication` 却没设，
    本机有桌面才侥幸通过，无头 CI 会挂。
  - 新增功能须配套/更新 `tests/` 用例，且 **tests/ 与 src/ 都要过 ruff + ty**。

## 3. mitmproxy 桥接红线（最重要，违反会崩溃/数据错乱）

mitmproxy Master 跑在独立 asyncio 线程（`core/mitm/runtime.py` 的 `_MitmThread`），
Qt 在主线程。**只有三条合法通道**：

1. **调用**：`MitmRuntime.call(callback, *, timeout=5.0)`，把闭包投到 mitm 线程执行并同步取回。
2. **封装**：Qt 侧一律经 `MitmFacade`（`core/mitm/facade.py`）。它内部按
   `if self.runtime.is_running` 决定走 `runtime.call(...)` 还是就地执行（未启动时没有别的线程）。
   `apps/` 只持有 facade（controllers 里的 `self._mitm`），不直接调 `runtime.call`。
3. **事件**：View 的 blinker 信号（`sig_view_add/update/remove/refresh`）由
   `_ViewSignalBridge` 转成 `MitmRuntime` 上的 Qt Signal（`flow_added` / `flow_updated` /
   `flow_removed` / `view_refreshed`），Qt 自动队列回主线程。**不要自己 poll View。**

红线：

- **禁止在 Qt 线程直接读写 flow / master / view 对象**。需要快照就 `flow.copy()`
  （见 `MitmFacade.all_http_flows`）。
  ⚠️ **已知违反（技术债，改到这些地方请顺手修）**：`apps/common/flow/models.py` 的
  `set_view` / `handle_refresh` 直接 `list(view)`、`clear_data` 直接 `view.clear()`、
  `remove_row` 直接 `view.remove([flow])` —— facade 里已经有 `clear_flows()` /
  `remove_flows()`，应改为经 facade。
- **禁止使用 `ctx`**。`ctx.options` 只由 `Master.__init__` 写入（`mitmproxy/master.py:52`），
  Qt 线程读到的是空的；需要 master/options 时用手上的 `runtime.master`。
  唯一豁免：`bindings.py` 末尾在**导入期、主线程**给 `ctx.options` 兜一个默认 `Options()`
  （`contentviews.make_metadata` 无条件读它，而 Master 起不来时只读会话页也要能渲染 body）。
  正因这是唯一豁免，`ctx` **不进 `bindings.__all__`**，别再从任何地方引它。
- **三个地址，三个用途，不能混用一个变量**（词表见 `core/network.py`）：
  ① **绑定地址** `listen_host`（`127.0.0.1` 或 `0.0.0.0`）只用于 socket bind 与端口探测；
  ② **本机接入地址恒为 `127.0.0.1`**，系统代理和工具栏端点只能取
  `MitmFacade.local_client_host`；③ **局域网展示地址**（`detect_lan_address()`）
  只用于显示和复制，**绝不写进配置，也不进任何网络调用**。
  `0.0.0.0` 是「多开一个入口」，不是「换一个入口」—— `INADDR_ANY` 本身覆盖环回，
  所以放开监听时系统代理**不需要**跟着改。把 `0.0.0.0:8080` 写进 Windows 代理设置，
  系统会拿它当目标地址去连，抓包会整体失效（回归用例见
  `tests/apps/capture/test_controllers.py` 的
  `test_system_proxy_stays_on_loopback_when_bound_to_all_interfaces`）。
- 修改 `flow.request.content` / `response.content` 后，mitmproxy 自动重算
  `Content-Length`，不要手动改 header。
- 不向 master 追加 mitmproxy 的命令行 addon（comment / cut / export / script 等），
  GUI 场景自行实现等效能力。

## 4. 目录分层（禁止跨层）

- `core/`：`application.py`（QApplication 装配）、`runtime.py`（`AppRuntime`，持有
  `MitmRuntime` + `MitmFacade`）、`settings.py`、`network.py`
  （监听地址词表 + 端口/地址收敛 + 局域网 IP 探测，无 Qt 无 mitmproxy，core 与 apps 共用）、
  `log.py`、`system_proxy/`
  （Windows 注册表代理开关）、`resources_rc.py`（Qt 生成物，勿手改）。
- `core/mitm/`：`bindings`（唯一 mitmproxy 入口）、`master`（`FerretMaster` addon 装配）、
  `runtime`（线程 + 信号桥）、`facade`、`addons`（`FerretTlsConfig` / `LogAddon`）、
  `export`、`io`、`certificate`（CA 生成 / 查看 / 导出 + Windows 信任库安装卸载，
  无 Qt、全部同步阻塞，调用方负责挪线程）、`blocklist`（屏蔽规则模型 + spec 构造，无 Qt）、
  `rewrite`（重写规则模型 + spec 构造，无 Qt；只认 mitmproxy 的选项名，不含任何 UI 文案）、
  `__init__`（公开 API）。
  - `engine.py` 是历史兼容 shim，只 re-export `CaptureMaster` / `FerretMaster`，
    **零引用者，可直接删**。
- `apps/`：业务与界面（`capture` / `certificate` / `common` / `session` / `settings` /
  `blocklist` / `rewrite`），**不得直接 import mitmproxy 内部模块**（当前 0 例外），
  一律经 `core/mitm` 的封装访问。
  - 后台任务统一用 `apps/common/tasks.py::FunctionTask`（`session` 与 `certificate` 共用），
    不要在各页各写一份 `QRunnable`。
  - `apps/certificate/`：证书页。certutil 一次查询实测 90~400ms、生成一套 CA 约 65ms，
    全部丢进线程池（限 1 线程，保证「先卸旧、再装新」这类连续操作不互相插队）；
    唯一留在主线程的是 `reload_certificate_store()` —— 它要碰 `master.options`（§3 红线）。
    证书自成一页独立 interface，抓包页工具栏**没有**证书角标（`cert_btn` /
    `certificate_requested` / `set_certificate_installed` 全仓库已 0 处），
    `apps/capture` 与 `apps/certificate` 互不认识、也不经 `MainWindow` 转发检测结果。
  - 设置页骨架（`apps/settings/views.py` 与 `apps/certificate/views.py` 共用：ScrollArea +
    `setViewportMargins(0, 80, 0, 20)` + 悬浮 TitleLabel `move(36, 30)` + `ExpandLayout`
    边距 `36,10,36,0` / 间距 28 + SettingCardGroup），三处细节非照抄不可（已实测）：
    ① `enableTransparentBackground()` **必须在 `setWidget()` 之后**调 —— 它内部是
    `if self.widget(): self.widget().setStyleSheet(...)`，提前调等于没调，
    深色主题下内层 QWidget 留着浅色底（这就是证书页曾经的主题 bug）；
    ② `ExpandLayout` 只按 `w.height()` 摆位、从不改高度，高度随内容变的卡片必须自己
    `setFixedHeight` 再回头 `group.adjustSize()`（它的 eventFilter 只认「高度变、宽度没变」，
    窗口横向缩放正落在盲区里）；
    ③ 长文本标签一律 `setMinimumWidth(1)` —— QLabel 拿整段文字宽当 `minimumSizeHint` 往上顶，
    横向滚动条又是关掉的，一个 SHA-256 指纹就能把整页顶到 1500px、把右侧按钮挤出视口
    （指纹本身也改成空格分组，QLabel 只在空白处断行）。
  - `apps/rewrite/`：重写规则页，四件套 `models` / `controllers` / `dialogs` / `views`
    与 `apps/blocklist/` 同构（控制器是规则的唯一权威副本，先下发 facade 成功再落盘）。
    页面按「容纳 mitmproxy 全部重写能力」搭骨架 —— 类型列与类型下拉都由 `RewriteKind`
    生成，成员只有一个时下拉自动置灰 —— 但**目前只落地 `map_remote`**。扩展方式：给
    `RewriteKind` 加成员（成员值就是 mitmproxy 的选项名）+ `RewriteRule.to_spec` 加一条
    分支，`rewrite_option_updates` 与整条下发链路都不用改。
    ⚠️ 接 `map_local` 之前得先摘掉 `bindings.py::_STUBBED_MODULES` 里的打包桩
    （连带 `__main__.py` 的 `--nofollow-import-to`，见 §7）；`modify_body` 要等
    `apps/common/edit/` 的代码编辑器修好。
  - 编辑类 UI 复用 `apps/common/edit/`：`ItemDualPanel` 编 headers、`ToolPlainTextEdit`
    编 body、`JsonDualPanel` 看 JSON 树，不新造编辑器。
  - 语法高亮走自写 lexer `apps/common/edit/syntax.py`（`tokenize_http` /
    `tokenize_json` / `tokenize_html`，已替掉 pygments），高亮器在 `highlighter.py`。
- `utils/`：`http_parser.py`（body 展示预处理）、`scripts.py`（纯 subprocess 工具）。
  - ⚠️ `utils/` 本应是叶子层，但 `http_parser.py` 现在 import 了 `core.mitm.bindings`。
    要彻底理干净就把 `build_body` 挪进 `core/mitm/`；新增 utils 不要再往里加依赖。

## 5. 已确认的技术决策（勿推翻）

- **正向代理模式**：监听端口 + 系统代理设置（Windows 注册表），不用透明代理
  （WinDivert / TUN / WireGuard）。
- `mitmproxy_rs`：`contentviews`（protobuf / gRPC 解码）已随 `prettify_message` 间接用上；
  `certs` 在 Windows/Linux 未实现；`process_info` 仅服务透明代理的进程选择器，
  正向代理无需；`syntax_highlight` 比 ferret 自己的 lexer 粗，不引入。
- 提交信息用 Conventional Commits 变体 `<type>(<scope>): <subject>`，type ∈
  `feat/fix/docs/style/refactor/perf/test/build/ci/chore/revert`，scope 用 `core` /
  `mitm` / `apps` 等。
- 已生效的移除项（勿复活）：顶层 `application/` 包删除、`runtime` 移入 `core/`、
  `utils/proxy_manager.py` 删除；自造的 `format_bytes` / `compute_folds` / `mime_of`
  删除（改用 mitmproxy 原生）。

## 6. 功能状态速查

- **实际装载的 addon 以 `core/mitm/master.py` 里 `FerretMaster.__init__` 的 `addons.add(...)`
  为准**：Core、Block、StripDnsHttpsRecords、BlockList、AntiCache、AntiComp、ClientPlayback、
  DisableH2C、Proxyserver、DnsResolver、NextLayer、MapRemote、FerretTlsConfig、View、ReadFile、
  Save、LogAddon。
  （AntiCache / AntiComp 已装载但对应 option 默认关闭；BlockList 的 `block_list`
  由 `apps/blocklist` 的规则页下发，无规则时等于关闭；Block 默认
  `block_global=True` / `block_private=False`，与 mitmproxy 出厂姿态一致。）
  Block 的位置（`Core` 之后、`StripDnsHttpsRecords` 之前）对齐原生 `default_addons()`：
  它只挂 `client_connected`，必须在任何流量成形之前决定放不放这条连接。
  BlockList 的位置（`StripDnsHttpsRecords` 之后、`AntiCache` 之前）对齐原生
  `default_addons()`，同时保证它早于 `View.request` —— 被拦的 flow 照样进流量表
  （403 显示状态码，444 走 `kill()` 显示 Error）。
  MapRemote 的位置（`NextLayer` 之后、`FerretTlsConfig` 之前）同样对齐原生
  `default_addons()`，同时保证它早于 `View.request` —— 流量表显示的是改写后的目标地址。
  `map_remote` 由 `apps/rewrite` 的规则页下发，无规则时是空列表，等于关闭。
- 已实现：正向代理抓包、client_playback 重放、`.flow` 读写、HAR / curl / httpie / raw
  导出、**CA 证书页**（`apps/certificate`：查看原生字段 / 导出 PEM·CER·P12 /
  安装卸载 Windows 用户根信任库 / 重新生成并热加载，见 §1.1 的六个坑）、
  系统代理开关、会话管理、
  **屏蔽规则 blocklist**（`apps/blocklist` 规则页 + 流量表右键「屏蔽此主机」，
  规则存 `config.json` 的 `Proxy.BlockList`）、
  **代理访问控制 block**（`apps/capture` 的 `ProxyPortDialog` 把「监听地址 + 端口 +
  来源限制」一次提交；来源开关热生效走 `options.update`，绑定地址/端口要重开 socket，
  所以控制器先下发开关再重启 —— 重启失败不会丢掉已生效的开关。配置落
  `Proxy.{ListenHost,ListenPort,BlockGlobal,BlockPrivate}`；环回来源被原生 Block
  无条件放行，所以绑环回时两个开关置灰但保留勾选状态）、
  **重写规则 mapremote**（`apps/rewrite` 规则页：命中的请求在发出前被改写目标地址，
  Host 头/scheme/端口由原生 setter 一起更新；三种匹配方式 包含/等于/正则，
  规则存 `config.json` 的 `Rewrite.Rules`，见 §1.1 的六个坑）。
- 功能缺口（GUI 后续方向）：**断点拦截 intercept**（全仓库 0 处 `intercept`）、
  modifyheaders / modifybody（重写页已留好接入点，等 `apps/common/edit/` 可用）、
  maplocal（同上，另需先摘打包桩）、serverplayback、stickycookie / stickyauth、
  **流量备注**（全仓库 0 处 `flow.comment`，也无备注 UI）。
- ⚠️ **侧边栏文案基本没进翻译文件**：`i18n/zh_CN.qm` 里只有 `captures` 与 `settings`
  两条，`blocklist` / `sessions` / `certificate` / `rewrite` 都会**直接显示英文键名**
  （`zh_CN.ts` 还引用着已不存在的 `../../view/window.py`，早已失效）。新增页面照现状
  加就行，别单独给自己的页面硬编码中文 —— 要治就整体重跑 `pyside6-lupdate`。
- ⚠️ **`README.md` 的「内置 Addon 功能对照」表已过期**：它把 readfile / anticache /
  anticomp / disable_h2c / strip_dns_https_records 标成 ❌，实际都已装载；savehar 标 ❌，
  实际 `make_har` 正在用。判断功能状态以 `master.py` 为准，别照抄 README。

## 7. Nuitka 打包验证（发布/大改动前必须通过）

- 打包指令与全部 `--nofollow-import-to` / `--noinclude-dlls` 瘦身项集中在
  `src/ferret/__main__.py` 顶部的 `# nuitka-project:` 注释，改动须在此处统一维护。
- 运行：`nuitka .\src\ferret\`（注意是**目录**，不是某个 .py 文件），或
  `python -m nuitka src/ferret/__main__.py`。打包前若存在 `dist/` 先删除。
- 瘦身是两套机制配合，**配对规则是「被 nofollow 的模块必须要么有桩，要么只被已桩掉
  （或永不导入）的模块引用」**，不是「一一对应」：
  - `bindings.py` 的 `_STUBBED_MODULES` 现有 6 个桩
    （`mitmproxy.addons.{onboarding,onboardingapp,proxyauth,maplocal,cut}` + `pyperclip`），
    必须在任何 mitmproxy 导入之前完成。
  - `__main__.py` 的 mitmproxy 侧有 16 项 nofollow，其中 11 项没有桩，靠依赖链成立：
    flask / werkzeug / jinja2 / itsdangerous / markupsafe / blinker 只被 `onboardingapp` 引、
    ldap3 / bcrypt 只被 `proxyauth`（含 `utils.htpasswd`）引 —— 这两个模块都已桩掉；
    asgiref 只被 `asgiapp` 引，而 `mitmproxy/addons/__init__.py` 压根不 import 它；
    zstandard.backend_cffi 靠 C 扩展是主后端成立。
    注意 `bindings.py` 里 `from mitmproxy.addons import export` 会触发
    `addons/__init__.py`，把 comment / script / stickycookie / modify* 等**全部** addon 拉进导入期
    —— 桩机制存在的原因就是这个，别以为「没装载就不会被 import」。
  - `maplocal` 反过来是有桩没 nofollow（只省了 import，没省编译体积）。
  - `click` 裁的是 PyPI 的 click；mitmproxy 库路径用的是 vendored
    `mitmproxy.contrib.click`（`mitmproxy/log.py` 依赖它），**不要一起裁**。
  - 例外：`pyasn1` 不能排除（aioquic → service_identity 运行时硬链）。
- `contentviews` 引入的传递依赖不要顺手裁：`ruamel.yaml`、`mitmproxy_rs.contentviews`、
  aioquic / pylsqpack（h3 路径）。
- 打包后必须做**启动冒烟验证**：`dist/Ferret/Ferret.exe` 能起、GUI 不崩、
  能启动抓包内核（mitmproxy master 正常 listen）。
- 新增 mitmproxy addon 或第三方依赖时，先确认它是否会被 Nuitka 误裁，
  必要时在 `__main__.py` 加 `--include-package` 或移除对应 `--nofollow`。
