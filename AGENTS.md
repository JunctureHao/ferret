# Agent 运行规范 (System Instructions)

## 1. 工具调用协议 (CRITICAL)
- 你必须严格使用标准的 JSON 格式进行工具调用。
- 禁止使用任何 XML 标签 (如 <tool_call>)。
- 每一个 `read_file` 或 `write_file` 必须是一个独立的 JSON 对象。

## 2. 行为约束
- **禁止重复**：如果一次工具调用返回了错误，请分析原因并尝试不同的方法。禁止连续输出 3 次以上相同的描述文字。
- **禁止过度验证**：修改文件后，除非我明确要求，否则不要反复使用 `read_file` 来验证修改。相信你的写入操作。
- **简洁性**：不要道歉，不要解释你“正在尝试小心”，直接执行任务。

## 3. 错误处理
- 如果 JSON 解析持续失败，请停止调用工具，直接将你认为正确的代码块以 Markdown 格式发送给我，让我手动复制。
- 如果遇到 SyntaxError，优先检查导入语句 (Imports) 和括号闭合情况。

## 4. 上下文管理
- 在处理大型项目时，优先使用 `@` 引用特定文件。
- 如果对话过长，提醒我开启 New Thread 以节省 Token。

# Ferret 项目开发规范

## 语言要求

- 所有回复必须使用中文
- 所有对话必须使用标准的json 而不是xml

## 项目概述

Ferret 是一个基于 PySide6 + qfluentwidgets 的桌面抓包工具，使用 mitmproxy 作为核心代理引擎。

- Python 3.12.13，包管理使用 uv
- UI 框架：PySide6 6.10.3 + pyside6-fluent-widgets[full] 1.11.2

## 代码风格

- 使用 Python type hint
- 类方法命名：双下划线前缀表示私有（如 `__init_widget`、`__init_layout`、`__connect_signal_to_slot`）
- 每个 Widget 类遵循三段式初始化：`__init_widget` → `__init_layout` → `__connect_signal_to_slot`
- Signal 定义放在类体顶部，命名使用驼峰风格（如 `captureToggled`、`filterChanged`）



### 信号与槽

- 内部信号连接使用 `lambda` 传递上下文（如 `lambda: self.remove_row(row)`）
- 对外暴露业务信号，组件内部管理事件
- 使用 `@Slot()` 装饰器标注槽函数

## qfluentwidgets 使用规范

- 主题切换使用 `setTheme(Theme.DARK)` / `setTheme(Theme.LIGHT)`
- 图标优先使用 `FluentIcon`，自定义图标放在 `BaseIcon` 中
- 按钮组件选择：
  - `TransparentToolButton`：无背景图标按钮
  - `TransparentTogglePushButton`：带文字的切换按钮
  - `TransparentToggleToolButton`：无背景的切换图标按钮

## 多行组件管理

- 限制最大行数（如 FilterRow 最多 5 行），达到上限时禁用添加按钮
- 删除行时检查剩余行数，只剩一行时触发面板关闭信号
- 增删行后更新所有行的按钮状态（如 `_update_add_buttons`）
