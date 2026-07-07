# Ferret

基于 PySide6 + qfluentwidgets 的桌面抓包工具，使用 mitmproxy 作为核心代理引擎。

- Python 3.12.13，包管理使用 uv
- UI 框架：PySide6 6.10.3 + pyside6-fluent-widgets[full] 1.11.2

---

# 数据流程设计：从 mitmproxy 到展示

> 本文档规划 Ferret 的数据链路：从 mitmproxy 捕获流量开始，到 UI 各面板（原始 / 请求头 / 参数 / 请求体 / 响应体）最终展示为止。
> 设计目标：**单一真相源、解析只做一次、UI 只渲染不解析、折叠与高亮能力是数据的固有属性。**

## 一、设计原则（铁律）

| # | 原则 | 理由 |
|---|------|------|
| 1 | **解析只在 mitmproxy 线程做一次** | 避免 UI 重复解码/格式化导致卡顿 |
| 2 | **Body 按类型建模**（Binary / Json / Xml / Text） | 能否高亮、能否折叠、能否格式化是 body 的固有属性，应在数据层确定 |
| 3 | **fold_regions 是纯数据，在 mitmproxy 阶段计算** | UI 直接拿去画，零解析 |
| 4 | **Packet 不可变、跨线程安全** | 通过信号跨线程传递对象，无拷贝竞争 |
| 5 | **UI 只渲染，不解析** | 单一真相源，避免状态分裂 |
| 6 | **不可逆信息损失在 mitmproxy 阶段定死** | 二进制 / 编码 / 原始字节只有此处握有 `raw_content` |

## 二、整体数据流

```
mitmproxy HTTPFlow（原生对象）
   │
   ▼
[FlowParser / PacketBuilder]      ← mitmproxy 线程内，纯解析
   │  一次性：解码 body、拆 params、分类 Body、算 fold_regions
   ▼
Packet（领域模型，不可变）          ← 结构化、带派生数据
   │
   ▼ 跨线程信号
[EventEmitter / Controller]        ← UI 线程边界
   │
   ▼
PacketTableModel → 表格行
   │
   ▼ 选中某行
CapturesDataPanel.set_data(packet)
   │
   ├─ RawView        → packet.raw_request_text / raw_response_text
   ├─ HeaderView     → packet.req_headers / res_headers
   ├─ ParamsView     → packet.params
   ├─ BodyView       → packet.req_body / res_body（含 fold_regions）
   └─ CookieView     → packet.req_headers["Cookie"] 解析
```

**关键点**：所有面板订阅同一个 `Packet`，不存在"原始面板解析后驱动其他面板"。各面板平行地从 Packet 取自己那段。

## 三、mitmproxy 线程：解析层设计

### 3.1 增量构建 Packet

mitmproxy 回调按 flow 生命周期分阶段触发：

```
requestheaders → request → responseheaders → response
```

不在每个阶段重建整个 dict，而是**按 flow_id 累积构建 Packet**：

```python
class PacketBuilder:
    """跟踪单个 flow 的构建，跨 mitmproxy 阶段累积"""
    def __init__(self, flow_id: str):
        self.packet = Packet(flow_id=flow_id)

    def on_request_headers(self, flow):
        self.packet.method = flow.request.method
        self.packet.url = flow.request.pretty_url
        self.packet.path = flow.request.path
        self.packet.params = parse_query(flow.request.query)   # 一次拆好
        self.packet.req_headers = normalize_headers(flow.request.headers)

    def on_request(self, flow):
        raw = flow.request.raw_content or b""
        self.packet.req_body = self._build_body(
            raw, flow.request.headers.get("Content-Type"))

    def on_response_headers(self, flow):
        self.packet.res_headers = normalize_headers(flow.response.headers)
        self.packet.status = flow.response.status_code

    def on_response(self, flow):
        raw = flow.response.raw_content or b""
        self.packet.res_body = self._build_body(
            raw, flow.response.headers.get("Content-Type"))
        self.packet.complete = True
```

### 3.2 核心：`_build_body` 解码与分类

所有不可逆决策在此完成，结果存储为**枚举化的 Body 类型**：

```python
def _build_body(self, raw: bytes, content_type: str) -> "Body":
    decoded = try_decode(raw, content_type)   # gzip/deflate + charset 探测

    if decoded is None:                        # 二进制（图片/gzip/protobuf）
        return BinaryBody(raw=raw, mime=guess_mime(content_type))

    if is_json(content_type, decoded):
        parsed = json.loads(decoded)
        return JsonBody(
            raw_text=decoded,
            pretty=json.dumps(parsed, indent=4, ensure_ascii=False),
            fold_regions=compute_folds(decoded),   # 只算一次
            size=len(raw),
        )

    if is_xml(content_type, decoded):
        return XmlBody(
            raw_text=decoded,
            fold_regions=compute_xml_folds(decoded),
            size=len(raw),
        )

    return TextBody(raw_text=decoded, size=len(raw))
```

### 3.3 Body 类型建模

```python
@dataclass
class Body:
    size: int

@dataclass
class TextBody(Body):
    raw_text: str

@dataclass
class JsonBody(Body):
    raw_text: str
    pretty: str              # 格式化后的缩进文本
    fold_regions: list       # 折叠区域（纯数据）

@dataclass
class XmlBody(Body):
    raw_text: str
    fold_regions: list

@dataclass
class BinaryBody(Body):
    raw: bytes               # 保留原始字节，不解码
    mime: str                # image/png, application/gzip ...
```

**为什么分类型**：UI 拿到 `JsonBody` 就知道"用 JSON 高亮器 + 启用折叠"；拿到 `BinaryBody` 就知道"不渲染内容、显示占位符"。能力内建于数据，UI 零判断。

### 3.4 `compute_folds` —— 纯函数，语言无关

```python
@dataclass
class FoldRegion:
    start_line: int          # 0-based
    end_line: int
    brace_type: str          # "{" / "[" / "<"

def compute_folds(text: str) -> list[FoldRegion]:
    """栈匹配括号对，返回所有跨行折叠区域（单行配对忽略）"""
    # 只依赖文本，不依赖 Qt / lexer
    # 支持 { } [ ]，未来可扩展 < >（XML 标签）
```

`fold_regions` 是**纯数据**，UI 层直接用于绘制，不需要重新解析。

## 四、Packet 领域模型

```python
@dataclass(frozen=True)      # 不可变 → 跨线程安全
class Packet:
    flow_id: str
    method: str
    url: str
    path: str
    params: dict[str, str]
    req_headers: dict[str, str]
    req_body: Body
    res_headers: dict[str, str]
    res_body: Body
    status: int
    complete: bool

    # 派生文本（按需生成并缓存）
    def raw_request_text(self) -> str: ...
    def raw_response_text(self) -> str: ...
```

**frozen 的价值**：Packet 在 mitmproxy 线程构建、通过信号发给 UI 线程。不可变对象跨线程零风险，UI 只能读不能改。

## 五、线程边界

```python
class TrafficAddon:
    def __init__(self, emit):
        self.emit = emit                     # UI 线程信号
        self._builders: dict[str, PacketBuilder] = {}

    def request(self, flow):
        b = self._builders.setdefault(flow.id, PacketBuilder(flow.id))
        b.on_request(flow)
        self.emit(b.packet)                  # 发当前进度版本

    def response(self, flow):
        b = self._builders[flow.id]
        b.on_response(flow)
        self.emit(b.packet)                  # 发完整版本
        del self._builders[flow.id]          # 构建完即释放
```

每次 emit 的是 Packet 的当前状态，UI 收到即刷新。所有面板订阅同一 Packet，天然同步。

## 六、UI 层：只渲染不解析

### 6.1 面板与 Packet 字段映射

| 面板 | 数据来源 | 高亮器 | 折叠 |
|------|---------|--------|------|
| 原始（请求） | `packet.raw_request_text` | HTTPHighlighter | body 段复用 `req_body.fold_regions` |
| 原始（响应） | `packet.raw_response_text` | HTTPHighlighter | body 段复用 `res_body.fold_regions` |
| 请求头 | `packet.req_headers` | KVHighlighter | 否 |
| 参数 | `packet.params` | KVHighlighter | 否 |
| 请求体 | `packet.req_body` | JSON/XML/Text Highlighter | `req_body.fold_regions` |
| 响应体 | `packet.res_body` | JSON/XML/Text Highlighter | `res_body.fold_regions` |
| Cookies | `packet.req_headers["Cookie"]` 解析 | KVHighlighter | 否 |

### 6.2 BodyView 消费示例

```python
class BodyView:
    def show(self, body: Body):
        if isinstance(body, JsonBody):
            self.editor.set_text(body.pretty)
            self.editor.enable_fold(body.fold_regions)   # 直接用，不重算
            self.editor.use_highlighter(JSONHighlighter)
        elif isinstance(body, XmlBody):
            self.editor.set_text(body.raw_text)
            self.editor.enable_fold(body.fold_regions)
            self.editor.use_highlighter(XMLHighlighter)
        elif isinstance(body, BinaryBody):
            self.editor.show_binary_placeholder(body.size, body.mime)
        else:  # TextBody
            self.editor.set_text(body.raw_text)
            self.editor.use_highlighter(TextHighlighter)
```

UI 中**没有任何** `json.loads` / `decode_body` / `parse_qs` —— 全在 mitmproxy 阶段完成。

### 6.3 高亮器职责收敛

| 高亮器 | 职责 | 不负责 |
|--------|------|--------|
| `HTTPHighlighter` | HTTP 外壳（请求行 + header + body 分界） | 不解析 JSON 结构 |
| `JSONHighlighter` | 纯 JSON 词法 + 消费 fold_regions | 不解析 HTTP |
| `XmlHighlighter` | 纯 XML 词法 + 消费 fold_regions | 不解析 HTTP |
| `KVHighlighter` | `key: value` 文本高亮 | 不解析 JSON / HTTP |

### 6.4 折叠渲染流程

```
Packet.req_body.fold_regions (mitmproxy 阶段算好)
   │
   ▼
BodyView.enable_fold(regions)
   │
   ▼
Editor 绘制折叠图标（行号左侧 ▶/▼）
   │
   ▼ 用户点击
toggle_fold(start_line):
   - 折叠: 文档中删除 [start+1, end] 行，首行替换为 "{ ... }" / "[ ... ]"
   - 展开: 从原始文本恢复
   - 折叠占位符作为伪 token 参与高亮
```

折叠操作只在 body 编辑器内做文档替换，原始面板如需折 body 则复用同一份 regions（加 header 行偏移）。
