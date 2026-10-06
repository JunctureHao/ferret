---
name: ferret-i18n
description: 维护 Ferret 的中文界面源文案与 en_GB 英文翻译，排查英文界面漏译，并重建和验证翻译资源。用于新增或修改界面文案、补英文译文、诊断 i18n 问题；仅修改 README、开发注释或日志时不使用。
---

# Ferret 翻译维护

让请求涉及的界面文案能被提取、准确翻译，并从应用实际使用的资源中读回。
先遵守仓库 [AGENTS.md](../../../AGENTS.md)；本文件中的命令均在仓库根目录运行，本机抓包时为 uv 加 `--system-certs`。
只做诊断时停在原因与证据，不修改源文案、翻译目录或生成物。

## 定位范围

1. 用 `git status --short` 和相关 diff 确认已有改动，避免覆盖用户正在修改的译文。
2. 从用户给出的页面、中文原句或错误提示，用 `rg` 找到调用点与 [en_GB.ts](../../../src/ferret/resources/i18n/en_GB.ts) 条目。按 **context + source** 核对；相同中文在其他 context 有译文，不能证明这里可用。
3. 修改源文案前读该调用点：普通 `tr()` 的类上下文、显式 `translate()` 的 context，或 [标记表助手](../../../src/ferret/utils/i18n.py) 的 `QT_TRANSLATE_NOOP` 与 `resolve_marker` 是否对应。处理当前任务涉及的条目，不顺带改写全站术语。

英文仍显示中文时，按现象缩小范围：

| 证据 | 下一步 |
| --- | --- |
| TS 没有对应条目 | 检查源文案是否可静态提取，然后重新提取；不要仅手工向 TS 添一条掩盖提取失败 |
| 条目存在但译文为空、未完成或语义过时 | 阅读调用位置与相邻译文，补齐或修正该条目 |
| TS 正确而编译资源不一致 | 完整运行资源流水线；只改 TS 或只生成 QM 都不足以更新应用 |
| 资源校验通过，页面仍异常 | 核对运行时 context、标记求值时机，以及 [应用翻译器初始化](../../../src/ferret/core/application.py)；标记在模块导入时求值会冻结中文 |

## 提取、补译、构建

1. 按任务修改中文源文案，保持可提取的调用形式。涉及模块级或类级文案表时复用现有标记助手；具体约束以根 AGENTS.md 的 i18n 节为准。
2. 源文案或 context 有变化、目录缺条目时，先调用 [现有流水线](../../../src/ferret/utils/scripts.py) 的提取函数：

   ```powershell
   uv run python -c "from ferret.utils.scripts import pyside6_lupdate; raise SystemExit(0 if pyside6_lupdate() else 1)"
   ```

   提取失败先解决失败原因。复用函数能保留生成物排除与 `-no-obsolete` 参数；不另写全目录递归提取命令。
3. 编辑 `en_GB.ts` 中相关译文。lupdate 只更新目录，**不会自动生成英文**；不要把流水线成功当成补译完成。逐条确认：
   - 参考相同功能的现有英文术语与英式拼写，按实际动作翻译完整句子。
   - 保留格式占位符的名称、数量、索引及格式规格；保留需要的换行、富文本结构和 XML 转义，不能让英文 `.format()` 失败。
   - 核实 context 与 source，复查 lupdate 复用的旧译文；只有译文审核完成才移除 `type="unfinished"`，不能批量去标记掩盖漏译。
4. 补译后运行完整流水线，把 TS 编为 QM 并嵌入应用资源：

   ```powershell
   uv run python -m ferret.utils.scripts
   ```

   任一步失败先修复再重跑。QM 与 [resources_rc.py](../../../src/ferret/core/resources_rc.py) 由流水线生成，不手改二进制或资源字节。

## 验证与完成

运行 [i18n 守卫](../../../tests/core/test_i18n.py)：

```powershell
uv run python -m unittest discover -s tests/core -p test_i18n.py
```

- `ExtractableTests` 失败：修正源代码调用形式；`CatalogTests` 失败：检查提取、漏译、未完成条目、旧条目或失效 location；实现注释漏成 `<extracomment>` 时回查调用点前的 `#:`。
- `CompiledCatalogTests` 失败：核对完整流水线输出及资源加载路径。测试通过仍不能保证英文语义、占位符与布局正确；结合变更内容复核，涉及布局或运行时求值时检查对应页面。
- 查看相关 diff，确认源文案、TS、QM 和资源模块一致。lupdate 可能更新 location，rcc 可能产生较大生成差异；核对来源，不手工裁剪资源字节，也不撤销用户原有改动。

修改任务完成时，相关译文已确认、流水线及 i18n 测试通过，并执行根 AGENTS.md 要求的相应门禁。说明改动位置、验证结果和未验证的页面行为。
诊断任务完成时，给出故障所在环节、对应调用点或测试证据，以及明确的修复步骤；尚无证据的假设要标明。
