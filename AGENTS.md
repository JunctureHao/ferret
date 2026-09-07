# AGENTS.md — Ferret 开发约定

基于 **PySide6 + QFluentWidgets + mitmproxy** 的桌面 HTTP/HTTPS 流量抓包工具。
改动代码前必须先遵守本文件；与代码冲突时以代码为准，并回改本文件里失真的那条规则。

## 0. 本文件的维护规则（先读）

- 本文件只收两类内容：**防错规则**（不写就会犯错）与**决策结论**（已定，勿推翻）。

- 不写状态快照：版本号、文件清单、addon 清单、「已实现」列表、UI 布局描述一律不进本文件——它们的事实源是 `pyproject.toml`、`core/mitm/master.py`、`core/mitm/__init__.py` 与各子包 controllers/views 的 docstring 及代码注释，写副本必腐烂。

- 新增一行前先问：**不写它，agent 会犯什么错？** 答不上来就不加。「为什么」优先写进代码注释（本仓库注释即档案），这里最多留一行结论 + 指针。

- 全文预算 \~110 行；要加新的，先删或并旧的。

## 1. 技术栈与门禁

- 依赖与版本以 `pyproject.toml` / `uv.lock` 为准；包管理用 **uv**。

- GUI：控件优先 QFluentWidgets（图标 `FluentIcon`、主题 `isDarkTheme`），不退回原生 Qt 样式；语法高亮走自写 `apps/common/edit/syntax.py`，不引 pygments。

- **提交前门禁必须绿**：`ruff check .` + `ty check`（uvx 临时装）。只格式化**自己改动的文件**，禁止全量 `ruff format .`；ruff 忽略用 `# noqa: CODE`，ty 用 `# ty: ignore[rule]`；保留 `from __future__ import annotations`。本机抓包时跑门禁加 `--system-certs`。

- 测试：`python -m unittest discover -s tests`；碰 Qt 的测试文件在 import PySide6 前设 `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")`。

- 提交信息：`<type>(<scope>): <subject>`，type ∈ `feat/fix/docs/style/refactor/perf/test/build/ci/chore/revert`，scope ∈ `core/mitm/apps/utils`。

- 打包：Nuitka，瘦身项统一在 `src/ferret/__main__.py` 顶部 `# nuitka-project:` 注释维护；**动打包 / 加第三方依赖 / 升级 mitmproxy 前必读** **`docs/packaging.md`**。

## 2. 原生能力优先（勿重复造轮子）

以下一律用 mitmproxy 原生，不要自己实现：

- Cookie / query：`flow.request.cookies` / `.query`（勿手拆 header）。

- 解码 body：`message.get_text(strict=False)` / `get_content(strict=False)`（**别用** **`.text`** **/** **`.content`**，畸形编码会抛 `ValueError`）。

- body 视图：`contentviews.prettify_message(message, flow)`。注意输出过 `escape_control_characters`；`syntax_highlight` 无 `json` 值（JSON 自报 `yaml`），见 `apps/common/flow/views.py::_body_lang`。

- 字节大小：`human.pretty_size`（不自造 `format_bytes`）。

- HAR 导出：`SaveHar().make_har`（纯函数）。

- curl/httpie/raw 导出：`mitmproxy.addons.export` 模块级函数；唯一分叉 `core/mitm/export.py::curl_command`（Windows 引号），不要改回原生。

- 屏蔽 / 重写 / 来源限制：`BlockList`+`parse_spec`、`MapRemote`/`MapLocal`+`parse_map_remote_spec`/`parse_map_local_spec`、`Block`。ferret 只造 spec 字符串，经 `options.update(...)` 下发（addons.add 前选项不存在）。

- CA：`certs.CertStore.from_store` / `Cert` 字段 / `Cert.to_pem()`；系统信任库只走 Windows `certutil`。

## 3. 桥接红线（违反会崩溃/数据错乱）

mitmproxy Master 在独立 asyncio 线程，Qt 在主线程。合法通道只有三条：

1. `MitmRuntime.call(callback, timeout=5.0)` 投到 mitm 线程。
2. Qt 侧一律经 `MitmFacade`（`apps/` 只持 facade，不直接调 `runtime.call`）。
3. 事件经 `_ViewSignalBridge` 转 Qt Signal，**不要自己 poll View**。

禁止项：

- ❌ 在 Qt 线程直接读写 flow/master/view。要快照走 `facade._snapshot()`（见 `all_http_flows` / `intercepted_flows`），**不要**直接 `flow.copy()`：原生 `Serializable.copy()` 会换掉 `flow.id`，而界面回头找真流量（`release_flows` / `apply_request_edits` / `save_flows`）全靠这个 id。

- ❌ 使用 `ctx`。需 master/options 用手上的 `runtime.master`；`ctx` 不进 `bindings.__all__`。

- ❌ 跨层 import mitmproxy。`from mitmproxy import ...` 只允许出现在 `core/mitm/bindings.py`；`core/mitm/*` 内部 `from ...bindings import`，其余一律 `from ferret.core.mitm import`。

- ❌ 向 master 追加 mitmproxy 命令行 addon（comment/cut/export/script 等），GUI 自行实现等效能力。

- ❌ 手动改 `Content-Length`——改 `flow.request.content` / `response.content` 后 mitmproxy 自动重算。

- 三个地址不可混用：`listen_host`（bind 用）/ 本机接入恒 `127.0.0.1`（`MitmFacade.local_client_host`）/ 局域网展示地址（`detect_lan_address()`，只显示不写配置）。系统代理只写 `127.0.0.1`。

## 4. 分层与 import 门禁

- `core/`：无 Qt；`network.py` 无 mitmproxy。系统代理是独立 workspace 成员 `packages/sysproxy`（零依赖、零 Qt）：不许 import ferret / PySide6、不自造默认目录，journal 路径由宿主注入；它只抛英文常量，展示边界在 `CaptureController._SYSTEM_PROXY_ERRORS` 翻译（包常量新增必须同步补映射，`tests/core/test_system_proxy.py` 钉着）。

- `core/mitm/`：`bindings.py` 是唯一 mitmproxy 入口，对外 API 以 `__init__.py` 为准；会送到界面的异常文案（`certificate` / `facade` / `gateway` / `intercept` / `modes` / `rewrite` / `runtime`）用 `QCoreApplication.translate("<Ctx>", ...)` 包一层（不碰控件）；日志与 `from_dict` 校验消息不译（后者从不上界面）。

- `apps/`：不直接 import mitmproxy 内部模块；后台任务统一 `apps/common/tasks.py::FunctionTask`；编辑类 UI 复用 `apps/common/edit/`（`ItemDualPanel` / `ToolPlainTextEdit` / `JsonDualPanel`），不新造编辑器；方法词表 `apps/common/http_methods.py` 与断点共享。各子包职责与 UI 结构以自己的 controllers / views docstring 为准，本文件不复述。

- `utils/`：不再新增依赖；`utils/http_parser.py` 现存一处对 `core/mitm/bindings` 的历史误引，勿模仿扩散（唯一例外，机理见 `core/mitm/detail.py` 注释）。

- 新增文件按上面门禁归类即可，本文件不维护目录清单。

## 5. 技术决策（勿推翻；详细理由见对应代码注释）

- **三通道抓包**：regular + local + wireguard 任意组合并存，经 `options.update(mode=[...])` 热更（官方 `proxyserver` 路径）。local spec 必须挂 `@127.0.0.1:0` 占位（上游 #7063 查重缺陷，上游修复后可整体移除）。

- **不做 transparent / tun**：Windows 上游明文 unsupported、需整进程管理员、重定向端口硬编码 8080、随包分发 WinDivert 1.3.0；tun 在 Rust 侧 Linux-only。

- **启停语义**：应用启动零抓包动作；「开始」= 通道接通 + 系统代理 attach（按勾选）+ 开写入闸门，「停止」整体回落。通道**意图值**（`use_local` / `local_spec` / `use_wireguard`，落盘）与**接通位**（`set_channels_engaged`，不落盘）分离；写入闸门在控制器（`_on_flow_added`）且**不碰 core View**（intercept/compose 依赖）。

- **守护进程拆除必须同步**：`MitmRuntime.stop` 在存活事件循环上同步 `_disarm_local_redirector`，`_run_master` 开场防御性再清一次（守护进程在进程外，内核停止会丢挂起任务，机理见 `runtime.py` 注释）。

- **block\_private 为 wireguard 让路**：`_effective_block_private()` 在通道接通且 wireguard 开启时强制 False，用户配置值保留、回落即恢复。

- 通道实例启动失败（UAC 拒绝等）不被 `options.update` 同步抛出，只能延迟读 `channel_health`，控制器抓包中 1.5s 轮询一次。

- `intercept_expression` 按 phase 分组后用**显式** **`&`** 挂 `~q` / `~s`，不能用并列——flowfilter 里并列优先级低于 `|`，会把整串段攥住。

- 不引入 `mitmproxy_rs` 的 `certs` / `syntax_highlight`；`rs_*`（local / wireguard / process\_info）经 bindings 接入。

- QR 编码用 `segno`（零二级依赖；qrcode 会把 colorama 拉回依赖树），矩阵经 `modes.qr_matrix`。

- SSE 靠自研 tee（mitmproxy 对 SSE 零支持，两条原生路都不通），见 `core/mitm/sse.py`。

- 已删除勿复活：顶层 `application/` 包、`utils/proxy_manager.py`、自造 `format_bytes` / `compute_folds` / `mime_of`。

## 6. 功能边界

- 实际装载的 addon 以 `core/mitm/master.py` 为准，本文件不维护清单。

- 尚未实现（实现后更新本行）：serverplayback、stickycookie/stickyauth。

## 7. i18n（中文源 + `en_GB.qm`）

- 源语言是简体中文：所有 `tr()` / `translate()` 字面量直接写中文，英文只进 `resources/i18n/en_GB.ts`；默认语言仍是简体中文；选中文不装业务翻译器（源文本直显），选 English 装 `en_GB.qm`。

- 改任何文案后必须跑 `uv run python -m ferret.utils.scripts`（lupdate → lrelease → rcc 一条链，rcc 输出必须落 `core/resources_rc.py`）。少跑一步不报错，英文界面静默退回中文——`tests/core/test_i18n.py` 是守卫，跑它验证。lupdate 必须排除 `core/resources_rc.py`（3.6 MB 生成物会让它崩）；该文件是生成物勿手改（rcc 会把 mtime 编进资源表，diff 大是正常）。

- lupdate 是静态扫描，两件事提取不到：**f-string 内部**（整句留外面、变量交给 `.format()`）与 **`tr(变量)`** **/** **`translate(变量, ...)`**（context 与源文本都得是字面量；唯一例外 `resolve_marker` 的共用查表器，context 由调用方给）。

- 模块级与类体（含 `ClassVar`）不得求值翻译（翻译器那时还没装，求出的文案会永久冻结成中文）：存 `QT_TRANSLATE_NOOP("Ctx", "text")` 标记，到使用点用 `QCoreApplication.translate` / `resolve_marker` 求值（`utils/i18n.py`）。

- 不拼句：每个分支写整句（`tr("{}失败").format(动作)` 换个语序就没法译）。

- 日志与 `from_dict` 校验消息不译；语言名列表（settings 页）刻意不译。

