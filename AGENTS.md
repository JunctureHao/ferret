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
- ❌ 在 Qt 线程直接读写 flow/master/view。要快照用 `flow.copy()`（见 `MitmFacade.all_http_flows`）。`apps/common/flow/models.py` 的 `set_view`/`handle_refresh`/`clear_data`/`remove_row` 是已知违反，应改走 facade 的 `clear_flows()`/`remove_flows()`。
- ❌ 使用 `ctx`。需 master/options 用手上的 `runtime.master`。`ctx` 不进 `bindings.__all__`。
- ❌ 跨层 import mitmproxy。`from mitmproxy import ...` 只允许在 `core/mitm/bindings.py`；`core/mitm/*` 内 `from ...bindings import`，其余 `from ferret.core.mitm import`。
- ❌ 向 master 追加 mitmproxy 命令行 addon（comment/cut/export/script 等），GUI 自行实现等效能力。
- ❌ 手动改 `Content-Length`——改 `flow.request.content`/`response.content` 后 mitmproxy 自动重算。
- 三个地址不可混用：`listen_host`（bind 用）/ 本机接入恒 `127.0.0.1`（`MitmFacade.local_client_host`）/ 局域网展示地址（`detect_lan_address()`，只显示不写配置）。系统代理只取 `127.0.0.1`。

## 4. 目录分层

- `core/`：`application.py`/`runtime.py`(`AppRuntime`)/`settings.py`/`network.py`(地址词表+局域网探测，无 Qt 无 mitmproxy)/`log.py`/`system_proxy/`(注册表代理)/`resources_rc.py`(勿手改)。
- `core/mitm/`：`bindings`(唯一 mitmproxy 入口)/`master`/`runtime`/`facade`/`addons`(`FerretTlsConfig`/`LogAddon`)/`export`/`io`/`certificate`(无 Qt 同步阻塞)/`blocklist`/`rewrite`/`__init__`(公开 API)。`engine.py` 零引用可删。
- `apps/`：`capture`/`certificate`/`common`/`session`/`settings`/`blocklist`/`rewrite`，**不直接 import mitmproxy 内部模块**。后台任务统一用 `apps/common/tasks.py::FunctionTask`。编辑类 UI 复用 `apps/common/edit/`（`ItemDualPanel`/`ToolPlainTextEdit`/`JsonDualPanel`），不新造编辑器。
- `utils/`：`http_parser.py`(body 预处理)/`scripts.py`(subprocess)。新增 utils 不再加依赖（现 `http_parser.py` 已误引 `core.mitm.bindings`，别扩散）。

## 5. 技术决策（勿推翻）

- 正向代理模式（监听端口 + 系统代理注册表），不用透明代理。
- 不引入 mitmproxy_rs 的 `certs`(Win/Linux 未实现)/`process_info`(透明代理用)/`syntax_highlight`(比自写 lexer 粗)。
- 已删除勿复活：顶层 `application/` 包、`utils/proxy_manager.py`、自造 `format_bytes`/`compute_folds`/`mime_of`。

## 6. 功能状态

实际装载 addon（`core/mitm/master.py` 为准）：Core、Block、StripDnsHttpsRecords、BlockList、AntiCache(关)、AntiComp(关)、ClientPlayback、DisableH2C、Proxyserver、DnsResolver、NextLayer、MapRemote、FerretTlsConfig、View、ReadFile、Save、LogAddon。

已实现：正向代理抓包、client_playback 重放、`.flow` 读写、HAR/curl/httpie/raw 导出、CA 证书页、系统代理开关、会话管理、屏蔽 blocklist、代理来源限制 block、重写 mapremote。

缺口：断点拦截 intercept、modifyheaders/modifybody、maplocal、serverplayback、stickycookie/stickyauth、流量备注 `flow.comment`。

⚠️ `README.md` 的「内置 Addon 对照」表已过期，以 `master.py` 为准。

## 7. Nuitka 打包（发布/大改动前）

- 瘦身项统一维护在 `src/ferret/__main__.py` 的 `# nuitka-project:` 注释；打包 `nuitka .\src\ferret\`（目录，非单文件）。
- `bindings._STUBBED_MODULES` 现有 6 桩：`mitmproxy.addons.{onboarding,onboardingapp,proxyauth,maplocal,cut}` + `pyperclip`，须在 mitmproxy 导入前完成。
- 接新 mitmproxy addon / 第三方依赖时，先确认是否会被 Nuitka 误裁，必要时加 `--include-package` 或移除对应 `--nofollow`。勿裁 `pyasn1`(aioquic 硬链)、`ruamel.yaml`、`mitmproxy_rs.contentviews`、aioquic/pylsqpack。
- 打包后冒烟：exe 能起、GUI 不崩、mitmproxy master 正常 listen。
