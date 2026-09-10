# Nuitka 打包

> 动打包 / 加第三方依赖 / 升级 mitmproxy 前必读本文（触发条件见 `AGENTS.md` §1）。
> 本文是档案型文档：记录「已挖到哪、为什么不能再挖」，不随代码同步维护；
> 实际瘦身项以 `src/ferret/__main__.py` 顶部的 `# nuitka-project:` 注释为准。

- 瘦身项统一维护在 `src/ferret/__main__.py` 的 `# nuitka-project:` 注释；打包 `nuitka .\src\ferret\`（目录，非单文件）。
- 两步已串成一键脚本 `scripts/package.py`（Nuitka 编译 → `vpk pack`，编译中间产物在 `build/dist/`、发布产物在 `build/releases/`）；参数见其 docstring，本文其余记录的是两步内部的「为什么」。
- `bindings._STUBBED_MODULES` 现有 11 桩，须在 mitmproxy 导入前完成：
  - `mitmproxy.addons.{onboarding,onboardingapp,proxyauth,cut}` + `pyperclip`（原有）。
  - `mitmproxy.addons.{browser,command_history,comment,termlog}` —— 都是 mitmproxy 自家命令行界面用的。`termlog` 的桩**必须**带 `TermLog` 属性：`mitmproxy/master.py:25` 的类注解 `termlog.TermLog | None` 在导入期就求值。`comment` 只注册一条 `flow.comment` 控制台命令，`Flow.comment` 属性本身在 `mitmproxy/flow.py` 上，ferret 在 `facade.py` 直接赋值，不经过它。
  - `werkzeug` + `werkzeug.security{safe_join}` —— mitmproxy 全包只有 `maplocal.py:9` 用了 `safe_join` 一个函数，却拖进 34 个 werkzeug 子模块加它独占的 colorama / markupsafe（约 7 MB obj / 3.2 MB exe）。`bindings._safe_join` 是照搬上游的等价实现（BSD-3, © Pallets）；这是**目录穿越安全边界**，升级 mitmproxy / werkzeug 后要比对上游 `werkzeug/security.py` 的 `safe_join` 有没有变，别自己发挥。colorama / markupsafe 只被 werkzeug 引用，不必单独立桩。
  - `maplocal` 已解桩（重写页的「重定向（本地）」要用它），别再加回去。`script` 也别桩：脚本功能后续要做。
- **标准库字节码 blob**：standalone 默认把「没被排除的那部分标准库」整批塞进 `__bytecode.const`（389 个模块 / 5.58 MiB，**不**计入 report.xml 的 obj 体积，容易看漏）。该 blob 几乎不压缩（input 5,882,087 → blob 5,854,988），所以在这里省下的字节是 **1:1** 落到 exe 上的 —— 比编译模块的 obj→exe 边际折算（实测 **0.345**，不是全局 0.473）划得多。现已排掉 54 棵零引用者子树（按源尺寸口径 ~1.2 MiB）；`__main__.py` 里列表按字母序维护，新增先过「全量 import + 装配实测不进 sys.modules」和「引用反查无活引用者」两关再插入。
  - 判定方法：拿 report.xml 的 `<module_usages>` 反查引用者。注意 Nuitka 会把整个标准库都记在 `__main__` 名下（伪引用），**必须把 `__main__` 剔掉再看剩下几个**；`reason` 字段不能当依据（`typing` 也写着「non-excluded parts of standard library」）。
  - 刻意留着：`_sitebuiltins`（site.py 启动就加载，它还持着 pydoc 的函数级懒引用，所以 pydoc 也不排）、`_pylong`（CPython C 层在超大整数 ↔ 字符串转换时自己 import，静态图里看不见）。`_cffi_backend` 不可排：cryptography 48 的 `_rust.pyd` 初始化硬 import（Nuitka ImplicitImports 也挂 cryptography→_cffi_backend），排掉 = TLS 崩；`doctest` 待 report.xml 复核（引用者 `pickle._test()` 是死路径但 pickle 是核心活模块）。`encodings`（122 模块 / 535 KiB）也不能动 —— codecs 按名动态查表，而拓包要解任意 `Content-Type` 的 charset。
- `pyparsing.diagram`（269.5 KiB obj）已排：`pyparsing/core.py:2567` 的 `from .diagram import ...` 在 `create_diagram()` 函数体里，外面就套着 `except ImportError`，而 `railroad` 本来就没装 —— 这条路运行期必然失败并被吞掉。（`pyparsing.testing` 不用管，anti-bloat 插件已经帮忙剔了，连 `unittest` 都没进包。）
- 编译侧已挖尽：除 ferret 自身外，全图只剩 6 个零引用者编译模块（0.21 MiB），全是 Nuitka/PySide6 的 pre/postLoad 脚手架与 `mitmproxy_windows`（**三通道方案在用**：local 模式的 windows-redirector.exe + WinDivert 2.2.2 必须随包分发，还有 `mitmproxy_rs` 本体——都是运行期动态调起，静态图看不见引用，别裁）。三个看着像胖子的都已验证是**活的**：`ruamel`（6.55 MiB）是 6 个 contentview 的渲染后端（`contentviews/_utils.py::yaml_dumps`）；`pyparsing`（4.79 MiB）是 `flowfilter` 的词法底层；`service_identity` → `pyasn1` + `attr`（6.43 MiB）是 `aioquic.tls.verify_certificate` 的证书校验链，`tlsconfig.py:436` 默认就是 `CERT_REQUIRED`。
- 接新 mitmproxy addon / 第三方依赖时，先确认是否会被 Nuitka 误裁，必要时加 `--include-package` 或移除对应 `--nofollow`。勿裁 `pyasn1`(aioquic 硬链)、`ruamel.yaml`、`mitmproxy_rs.contentviews`、aioquic/pylsqpack、`wsproto`(WebSocket 层顶层 import，帧展示要用)。
- 打包后冒烟：exe 能起、GUI 不崩、mitmproxy master 正常 listen。
