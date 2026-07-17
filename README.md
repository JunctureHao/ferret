# Ferret

基于 PySide6 + qfluentwidgets 的桌面抓包工具，使用 mitmproxy 作为核心代理引擎。

- Python 3.12.13，包管理使用 uv
- UI 框架：PySide6 6.10.3 + pyside6-fluent-widgets[full] 1.11.2

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
