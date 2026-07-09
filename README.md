# Ferret

基于 PySide6 + qfluentwidgets 的桌面抓包工具，使用 mitmproxy 作为核心代理引擎。

- Python 3.12.13，包管理使用 uv
- UI 框架：PySide6 6.10.3 + pyside6-fluent-widgets[full] 1.11.2

---

# 架构

采用分层架构，核心引擎与 UI 解耦，界面通过信号/槽与控制器通信。

```
src/ferret/
├── core/                    # 核心引擎层（与 UI 解耦）
│   ├── capture.py           # mitmproxy 集成：SnifferWorker(QThread) + UITrafficAddon
│   ├── http.py              # HTTP 类型封装：HttpMethod 枚举 + HttpClient（基于 httpx2）
│   ├── model.py             # 数据包表格模型 PacketTableModel + 过滤模型 PacketProxyModel
│   └── signals.py           # 全局信号 AppSignals
│
├── controllers/             # 控制器层（MVC 协调，不持有 UI 引用）
│   ├── capture_controller.py# 抓包生命周期管理
│   └── request.py           # 请求面板业务（方法列表、请求数据持有）
│
├── config/                  # 配置系统
│   ├── settings.py          # QConfig 子类 Config + CONFIG 单例（JSON 落盘）
│   └── resources_rc.py      # 编译后的资源文件
│
├── utils/                   # 工具函数
│   ├── exporter.py          # mitmproxy 导出 API 封装（curl/httpie/raw）
│   ├── http_parser.py       # HTTP 报文解析、body 解码/美化、折叠
│   ├── process_resolver.py  # 通过源端口反查进程（psutil）
│   └── proxy_manager.py     # 系统代理开关（Win/macOS/Linux）
│
└── views/                   # 视图层
    ├── window.py            # MainWindow(FluentWindow) + 系统托盘 + 置顶
    ├── common/              # 通用组件（button/dialog/edit/filter/icon/panel...）
    └── interface/           # 三个子界面
        ├── capture/         # 抓包界面
        ├── request/         # API 请求界面
        └── settings.py      # 设置界面
```

## 分层职责

| 层 | 职责 | 依赖方向 |
|---|---|---|
| **core** | 抓包引擎、HTTP 发送内核、数据模型 | 不依赖 PySide6 业务层 |
| **controllers** | 业务生命周期管理、信号协调 | 依赖 core，不持有 UI |
| **views** | 纯 UI 渲染、用户输入收集 | 通过 controller 与 core 交互 |
| **config** | 配置定义与持久化 | 全局单例 |
| **utils** | 无状态工具函数 | 独立可测 |

## 关键设计

- **线程模型**：抓包运行在 `SnifferWorker(QThread)` 内，通过 `asyncio.run` 驱动 mitmproxy 事件循环；UI 通过信号接收数据包，不在工作线程操作 UI
- **配置持久化**：基于 qfluentwidgets 的 `QConfig`，配置以 JSON 落盘，设置卡片双向绑定自动写盘
- **HTTP 封装**：`core/http.py` 用 `HttpMethod` 枚举约束方法，`HttpClient` 基于 httpx2 封装，默认启用 HTTP/2 并兜底异常

---

# 功能构成

## 1. 抓包（Capture）

- 基于 mitmproxy 作为代理引擎，捕获经过本机的 HTTP/HTTPS 流量
- 流量表格展示，支持按请求/响应多维度查看（原始报文、请求头、参数、请求体、响应体、Cookie）
- 请求体按类型（JSON / XML / 文本 / 二进制）分类渲染，支持语法高亮与折叠
- 支持流量筛选、导出（curl / httpie / raw）
- 通过源端口反查发起请求的进程
- 可接管系统代理（Win / macOS / Linux）

## 2. API 请求调试（Request）

- 独立的请求调试界面：URL 输入 + HTTP 方法选择（GET / POST / PUT / DELETE / PATCH / HEAD / OPTIONS / CONNECT / TRACE）+ 发送
- 请求体编辑面板，支持多种内容类型
- 响应展示面板，显示状态码与响应内容
- 业务由 `RequestController` 持有，UI 仅负责渲染（`core/http.py` 的 `HttpClient` 基于 httpx2 发送，默认 HTTP/2）

## 3. 设置（Settings）

- 基于 qfluentwidgets 设置卡片，配置项（DPI 缩放、语言、最小化托盘、布局、主题等）自动持久化

## 4. 通用能力

- 主窗口集成系统托盘、置顶按钮
- 通用组件库：按钮、对话框、编辑器（含高亮器）、筛选器、图标、面板、分割器等
