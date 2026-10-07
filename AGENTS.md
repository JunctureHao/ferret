# AGENTS.md — Ferret 开发约定

基于 **PySide6 + QFluentWidgets + mitmproxy** 的桌面 HTTP/HTTPS 流量抓包工具。
改动代码前必须先遵守本文件；与代码冲突时以代码为准，并回改本文件里失真的那条规则。
下文 `core/`、`apps/`、`utils/`、`resources/` 路径相对 `src/ferret/`，其余路径相对仓库根目录。

## 0. 本文件的维护规则（先读）

- 本文件只收两类内容：**防错规则**（不写就会犯错）与**决策结论**（已定，勿推翻）。
- 不写状态快照：版本号、文件清单、addon 清单、「已实现」列表、UI 布局描述不进本文件；事实源是 `pyproject.toml`、`core/mitm/master.py`、`core/mitm/__init__.py` 与相关代码。
- 新增前先问：**不写它，agent 会犯什么错？** 答不上来就不加。「为什么」优先写进代码注释，这里留结论 + 有效指针，勿复制实现细节。
- 全文预算约 110 行；要加新的，先删或并旧的。

## 1. 技术栈与门禁

- 依赖与版本以 `pyproject.toml` / `uv.lock` 为准；包管理用 **uv**。
- GUI：控件优先 QFluentWidgets（图标 `FluentIcon`、主题 `isDarkTheme`），不退回原生 Qt 样式；语法高亮走 `apps/common/edit/syntax.py`，不引 pygments。
- **提交前门禁必须绿**：`uvx ruff check .` + `uvx ty check`。只格式化**自己改动的文件**，禁止全量 `ruff format .`；ruff 忽略用 `# noqa: CODE`，ty 用 `# ty: ignore[rule]`；保留 `from __future__ import annotations`。本机抓包时 uv / uvx 加 `--system-certs`。
- 测试：`uv run python -m unittest discover -s tests`；碰 Qt 的测试在 import PySide6 前设 `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")`；等信号 / 等内核就绪用 `tests/core/mitm/_qt.py` 的轮询原语，勿新写嵌套 `QEventLoop.exec` 等待。
- 提交信息：`<type>(<scope>): <subject>`，type ∈ `feat/fix/docs/style/refactor/perf/test/build/ci/chore/revert`，scope ∈ `core/mitm/apps/utils`。
- 打包：Nuitka，瘦身项统一在 `src/ferret/__main__.py` 顶部 `# nuitka-project:` 注释维护；**动打包 / 加第三方依赖 / 升级 mitmproxy 前必读 `docs/packaging.md`**。

## 2. 复用入口与原生能力（勿重复造轮子）

复用以下入口并保留已有适配，不另造并行实现：

- Cookie / query：`flow.request.cookies` / `.query`，勿手拆 header。
- 解码 body：`message.get_text(strict=False)` / `get_content(strict=False)`；读取时别用 `.text` / `.content`，畸形编码会抛 `ValueError`。
- body 视图：`contentviews.prettify_message(message, flow)`；注意输出经过 `escape_control_characters`，JSON 的 `syntax_highlight` 自报 `yaml`，映射见 `apps/common/flow/detail.py::_body_lang`。
- 字节大小用 `human.pretty_size`；HAR 导出用 `SaveHar().make_har`；curl/httpie/raw 用 `mitmproxy.addons.export` 模块级函数，唯一分叉 `core/mitm/export.py::curl_command` 保留 Windows 引号适配。
- 重写统一由 `core/mitm/addons.py::FerretRewriteAddon` 执行，模型/校验在 `core/mitm/rewrite.py`。**同钩子内行序＝执行序**；响应头在 `responseheaders`、响应体在 `response`，勿为统一行序推迟响应头修改。
- 重写下发经 `core/mitm/runtime.py::apply_rewrite_rules` 保存内存副本、用 `self.call` 换预编译快照；勿恢复 `options.update` 的重写 spec 通道或原生 MapRemote/MapLocal/ModifyHeaders/ModifyBody。
- 屏蔽 / 来源限制：连接级用原生 `Block`；L7 屏蔽（出）由网关承载，`BlockList` 仅供兼容迁移。
- CA：`certs.CertStore.from_store` / `Cert` 字段 / `Cert.to_pem()`；系统信任库只走 Windows `certutil`。
- 上游 TLS 用原生 `ssl_insecure` / `ssl_verify_upstream_trusted_ca` / `add_upstream_certs_to_client_chain`；合并公共根与用户根走 `core/mitm/certificate.py::build_trusted_ca_bundle`，保留内容指纹，不自建 TLS context。
- mTLS 用原生 `client_certs`（单一路径，目录按 `<主机名>.pem` 精确匹配）；热更成功后及内核启动时须清 TLS context 缓存，复用 `core/mitm/runtime.py::clear_proxy_server_context_cache`，避免同路径换证仍出示旧证；`confdir` 不接。
- 反向代理用原生 `mode_specs.ReverseMode` / `proxyserver.configure`，勿自实现 TCP/HTTP 转发；保留 `UpdateAltSvc` 的 reverse 模式适配。
- mock 响应池复用原生 `ServerPlayback` 的 `FerretServerPlayback` 子类，保留认证挑战时不作答的闸门；链位与覆盖语义以 `core/mitm/master.py` 注释为准。
- mock 装载只走 `add_flows` / `load_flows`，不碰 `server_replay` 选项文件通道；权威副本在 `runtime.mock_pool`，条目 id 等于来源流量 id，复制须在 mitm 线程上保留 id（见 `core/mitm/facade.py`）。

## 3. 桥接红线（违反会崩溃/数据错乱）

mitmproxy Master 在独立 asyncio 线程，GUI 在主线程：

1. `MitmRuntime.call(callback, timeout=5.0)` 将操作投到 mitm 线程。
2. Qt 侧操作 mitm 流量与规则经 `MitmFacade`，`apps/` 不直接调用 `runtime.call`；控制器可持 runtime 管理生命周期、读取运行状态并连接信号。
3. 流量事件经 `UiBridgeAddon`、运行状态经 runtime 转 Qt Signal，勿自行轮询 core View。

- ❌ 在 Qt 线程直接操作 master/view、修改活 flow，或对活 flow 构建详情/解码 body；这些经 facade 投到 mitm 线程。跨线程交付的流量行一律是 `core/mitm/rows.py::FlowRow` 不可变快照（桥接信号与 `visible_flow_rows()` 都在内核侧折好，mitmweb `flow_to_json` 同款边界）；勿把活引用加回信号载荷或导出/删除/回放 API（这些一律按 flow.id 寻址）。新增改变行内容的内核侧变更路径必须经既有收口重发快照，漏发＝表格显示过期。
- 完整对象快照经 `all_http_flows()` / `intercepted_flows()` 获取（导出与断点窗口专用）；内部的 `core/mitm/facade.py::_snapshot` 是模块函数，负责保留 `flow.id`，勿自行 `flow.copy()`。回放创建新流量时仍应生成新 id。
- ❌ 使用 `ctx`。需 master/options 用手上的 `runtime.master`，仍须遵守线程边界；`ctx` 不进 `bindings.__all__`。
- ❌ 跨层 import mitmproxy。业务源码只有 `core/mitm/bindings.py` 可直接 import mitmproxy；`core/mitm/*` 从 bindings 引入，其余用 `from ferret.core.mitm import`。
- ❌ 向 master 追加 mitmproxy 命令行 addon（comment/cut/export/script 等），GUI 自行实现等效能力。
- ❌ 手动改 `Content-Length`；写入 `flow.request.content` / `response.content` 后由 mitmproxy 自动重算。
- 三个地址不可混用：`listen_host` 用于 bind；本机接入恒 `127.0.0.1`（`MitmFacade.local_client_host`）；`detect_lan_address()` 只用于局域网展示，不写配置。系统代理只写 `127.0.0.1`。

## 4. 分层与 import 门禁

- `core/` 不实现业务页面或控件；`core/application.py` 负责 Qt 应用启动与装配，配置、翻译及线程/信号基础设施可依赖 Qt。`core/network.py` 不依赖 mitmproxy。
- `packages/sysproxy` 是零依赖、零 Qt 的独立 workspace 成员：不许 import ferret / PySide6，不自造默认目录，journal 路径由宿主注入；英文异常常量的展示翻译在 `apps/capture/controllers.py::_SYSTEM_PROXY_ERRORS`，新增常量须同步映射并过 `tests/core/test_system_proxy.py`。
- `core/mitm/` 不依赖 QtWidgets；`bindings.py` 是唯一 mitmproxy 入口，对外 API 以 `__init__.py` 为准；送到界面的异常文案用 `QCoreApplication.translate("<Ctx>", ...)`，日志与不上界面的 `from_dict` 校验消息不译。
- `apps/` 后台任务统一用 `apps/common/tasks.py::FunctionTask`，调用方必须持有任务到 finished，避免包装与 signals 被 GC（见 `apps/update/controllers.py` docstring）。
- 编辑类 UI 复用 `apps/common/edit/`（`ItemDualPanel` / `ToolPlainTextEdit` / `JsonDualPanel`），不新造编辑器；方法词表复用 `apps/common/http_methods.py`。
- `utils/` 不再新增依赖；正文派生实现放在 `core/mitm/body.py`，`utils/http_parser.py` 只保留延迟兼容转发，勿恢复与 core 的循环导入。

## 5. 技术决策（勿推翻；详细理由见对应代码注释）

- **运行期不引入 SQLite / 嵌入式存储**：WS 历史在内核钩子内直接裁剪原生 `flow.websocket.messages`（`core/mitm/view.py::_trim_websocket`，保留窗口 = 界面 = 导出），跨线程 UI 事件用纯内存合并队列（`core/mitm/ui_events.py`）；有界化问题优先裁剪原生结构，勿新建落盘副本。
- **五通道抓包**：regular + local + wireguard + reverse + socks5 可组合，经原生 `options.update(mode=[...])` 热更；模式串统一复用 `core/mitm/modes.py` 的构造函数。
- local 保留 `@127.0.0.1:0` 查重占位；reverse / socks5 保留显式 `@host:port` 独立端口，监听地址跟随 `listen_host`；reverse 的 HTTPS 保留原生 TCP+UDP（`BOTH`）语义，勿裁成裸 TCP。
- SOCKS5 入站仅支持 TCP CONNECT；认证用同一份 `proxyauth`，不因 SOCKS5 接通而关闭认证（见 `core/mitm/runtime.py::_effective_proxyauth`）。
- **上游代理只替换 regular 的出口**：占 `mode[0]`，二者不并存；spec 不带 `@`、不含凭证，认证走 `upstream_auth`。关闭/无目标/无用户名时返回 `None`，不传空串（见 `core/mitm/runtime.py::_upstream_auth`）。
- `upstream_auth` 非空时 reverse 请求也会被补 `Authorization`，保持既定语义与选项闸门（`tests/core/mitm/test_upstream.py`）。上游仅支持 HTTP(S)，其余通道出口仍直连，裸 TCP/UDP 不经上游，凭证按现有配置明文落盘。
- **启停语义**：应用启动不接通抓包通道、不 attach 系统代理、不开写入闸门，但允许 regular 底座监听；「开始」接通选定通道、按勾选 attach 系统代理并开闸，「停止」回落会话，内核可继续供 Compose 使用。
- 通道意图值落盘、`set_channels_engaged` 接通位不落盘；外部流量写入闸门在控制器 `_on_flow_added`，Compose 显式记录独立于该闸门，由 `UiBridgeAddon` 分流，勿改 core View 收录来控制界面记录。
- **守护进程拆除必须同步**：`MitmRuntime.stop` 在存活事件循环上同步 `_disarm_local_redirector`，`_run_master` 开场防御性再清一次（见 `core/mitm/runtime.py`）。
- 通道接通时，若 WireGuard 开启，或 reverse / socks5 开启且绑定 `ANY_HOST`，`block_private` 下发值强制 False；配置原值保留，撤下通道即恢复（见 `core/mitm/runtime.py::_effective_block_private`）。
- 通道实例启动失败不会由 `options.update` 同步抛出；控制器抓包中须延迟检查 `channel_health`，不能把选项更新成功当成通道就绪。
- `intercept_expression` 按 phase 分组后用显式 `&` 挂 `~q` / `~s`，勿用隐式并列；优先级陷阱见 `core/mitm/intercept.py`。
- 不引入 `mitmproxy_rs` 的 `certs` / `syntax_highlight`；`rs_*` 能力经 bindings 接入。QR 用 `segno` / `core/mitm/modes.py::qr_matrix`，不引 qrcode。
- SSE 保留 `core/mitm/sse.py` 的自研 tee，勿改为仅缓冲完整响应或丢弃流式 body。
- **数据目录只用 Roaming**（`get_config_dir` / `AppDataLocation`）；勿改成与 Velopack 安装根冲突的 Local / `AppConfigLocation`，旧数据迁移复用 `core/settings.py::_migrate_legacy_config_dir`，勿动 Velopack 文件。

## 6. 功能边界

- 不做 transparent / tun：保留 Windows 支持边界，不引入需整进程管理员的透明代理或 Linux-only tun。
- 已删除勿复活：顶层 `application/` 包、`utils/proxy_manager.py`、自造 `format_bytes` / `compute_folds` / `mime_of`。

## 7. i18n（中文源 + `en_GB.qm`）

- 源语言是简体中文：`tr()` / `translate()` 字面量直接写中文，英文进 `resources/i18n/en_GB.ts`；默认中文且不装业务翻译器，English 装 `en_GB.qm`。
- 修改界面文案或翻译后跑 `uv run python -m ferret.utils.scripts`（lupdate → lrelease → rcc），并验证 `tests/core/test_i18n.py`；`core/resources_rc.py` 是生成物，勿手改。
- 修改资源流水线时保留 lupdate 排除 `core/resources_rc.py`、rcc 输出到该文件的约束；原因见 `utils/scripts.py` docstring。
- 翻译调用不要嵌在 f-string 内，整句翻译后再 `.format()`；`tr()` / `translate()` 的 context 与源文本须为字面量，唯一例外是共用查表器 `resolve_marker`。
- 模块级与类体（含 `ClassVar`）不得求值翻译；存 `QT_TRANSLATE_NOOP("Ctx", "中文源文本")` 标记，到使用点用 `QCoreApplication.translate` / `resolve_marker` 求值（`utils/i18n.py`）。
- 不拼句：每个分支翻译完整句子，不用 `tr("{}失败").format(动作)` 拼装句子。
- 日志与不上界面的 `from_dict` 校验消息不译；settings 页语言名列表刻意不译。
