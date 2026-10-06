---
name: ferret-capture-debug
description: 诊断与修复 Ferret 开始后无流量、通道启动失败、代理恢复失败、停止卡住、断点无法释放或流量未显示的问题。用于抓包链路异常；不用于单纯 UI 布局、翻译维护或新增抓包功能。
---

# Ferret 抓包异常诊断

以可核查证据定位故障所在层，按用户要求提供诊断或实施修复。先读仓库根 [AGENTS.md](../../../AGENTS.md) 和改动目录内适用的约定；本文中的命令均在仓库根运行。具体行为以代码及测试为准，按下面的症状入口读取相关实现。

## 确认失败发生在哪里

- 从现有描述、日志和测试取得：客户端及使用的通道、请求协议、开始/停止阶段、实际表现与预期、是否稳定复现。只追问会影响定位的缺失信息。
- 区分四种状态：内核是否运行、通道意图是否接通、客户端请求是否进入内核、流量是否进入界面。regular 监听存在不能证明正在抓包；`options.update` 成功不能证明通道实例已就绪。
- 对齐时间戳与可用的 flow id，保留最早的异常及其后续状态。日志位置从 [core/log.py](../../../src/ferret/core/log.py) 和配置目录函数确认；不要仅凭最后一条报错判断根因。

## 按证据选择入口

从已经失败的层开始；一项假设得到证据后再追踪相关下游，不要求每次遍历整表。

| 症状或证据 | 优先检查 | 实现入口与相关测试 |
| --- | --- | --- |
| 客户端无连接，系统代理开关/恢复异常 | 客户端实际代理与目标端口、会话状态、attach/detach 结果及 journal 所有权；客户端可能不使用系统代理 | [capture/controllers.py](../../../src/ferret/apps/capture/controllers.py)、[sysproxy/service.py](../../../packages/sysproxy/src/sysproxy/service.py)；`tests/core/test_system_proxy.py`、`tests/apps/capture/test_controllers.py` |
| 模式提交成功但通道稍后失败 | `_mode_specs` 的意图与接通位、`channel_health` 的延迟结果、控制器健康检查及最早上游告警 | [mitm/runtime.py](../../../src/ferret/core/mitm/runtime.py)、[mitm/modes.py](../../../src/ferret/core/mitm/modes.py)、[capture/controllers.py](../../../src/ferret/apps/capture/controllers.py)；`test_modes.py`、`test_runtime.py`、capture 控制器测试 |
| 已进入代理，但 CONNECT/HTTP 请求失败 | 先按状态码和日志区分入站 `proxyauth`、上游 `upstream_auth`、TLS 和目标连接失败；再查实际 addon 钩子和选项 | [mitm/master.py](../../../src/ferret/core/mitm/master.py)、[mitm/addons.py](../../../src/ferret/core/mitm/addons.py)；`tests/core/mitm/test_proxyauth.py`、`test_upstream.py` |
| 仅 HTTPS 失败或换证后仍出示旧证 | 客户端对 Ferret CA 的信任、代理对上游的信任、mTLS 客户端证书分别定位；核对已下发路径和缓存更新 | [mitm/certificate.py](../../../src/ferret/core/mitm/certificate.py)、[mitm/runtime.py](../../../src/ferret/core/mitm/runtime.py)；`tests/core/mitm/test_certificate.py`、`test_upstream_tls.py`、`test_client_certs.py` |
| 请求被屏蔽、绕过、挂起，或重写/mock 结果不符 | 用同一条流量追踪实际钩子阶段、规则命中和 addon 顺序，区分网关挂起与断点账本 | [mitm/master.py](../../../src/ferret/core/mitm/master.py)、[mitm/gateway.py](../../../src/ferret/core/mitm/gateway.py)、[mitm/intercept.py](../../../src/ferret/core/mitm/intercept.py)；`tests/core/mitm/` 中对应 gateway、rewrite、serverplayback、intercept 测试 |
| 内核有流量但界面缺行、刷新后更新丢失 | `UiBridgeAddon` 分流、控制器 `_on_flow_added` 写入闸门、Compose 记录意图、可见过滤和模型的实例身份匹配 | [mitm/runtime.py](../../../src/ferret/core/mitm/runtime.py)、[mitm/facade.py](../../../src/ferret/core/mitm/facade.py)、[flow/models.py](../../../src/ferret/apps/common/flow/models.py)；`tests/apps/common/flow/test_models.py`、capture 控制器测试 |
| 停止卡住、代理未恢复、断点无法释放 | 区分 `stop_capture` 与内核 `stop`；查看 detach 结果、挂起流放行、redirector 清理和线程退出的先后及返回值 | [capture/controllers.py](../../../src/ferret/apps/capture/controllers.py)、[mitm/runtime.py](../../../src/ferret/core/mitm/runtime.py)、[mitm/intercept.py](../../../src/ferret/core/mitm/intercept.py)；`tests/core/mitm/test_intercept.py`、`test_runtime.py`、capture 控制器测试 |

表中省略目录的 mitm 测试文件位于 `tests/core/mitm/`。先检索相关测试名称与夹具，再选择复现范围。

## 复现与修复

- 优先复用测试中的临时目录、本地服务、动态端口和清理逻辑；主动验证客户端接入时用显式代理请求区分“代理可用”和“客户端采用系统代理”。对现有系统代理、CA、系统级重定向或持久设置的变更，以用户本次任务的授权范围为准。
- 针对一个假设取得能区分原因的证据。不要用关闭认证/TLS 校验、清空 journal、替换 CA 或清空 core View 来掩盖故障，也不要把既定的停止后 regular 监听当作泄漏修复。
- 运行态查询和修改沿现有 facade/线程通道；表格的活引用与完整快照有不同用途，按相关方法的 docstring 判断，避免修复时破坏身份匹配。
- 修改行为时优先复用或补充能复现原问题的针对性回归；异步等待复用 [tests/core/mitm/_qt.py](../../../tests/core/mitm/_qt.py)，不要靠加长固定 sleep 或嵌套事件循环让测试偶然通过。
- 没有新证据时停止重复同一启停尝试，转向日志、状态与代码定位；环境无法复现时明确待验证假设，不凭猜测改动不相关层。

按定位结果选测试，例如通道晚报错问题可运行：

```powershell
uv run python -m unittest tests.apps.capture.test_controllers tests.core.mitm.test_modes
```

其他症状改用对应模块；再按根 AGENTS.md 完成适用门禁。仅诊断请求不因此扩展为代码修改或全量系统试验。

## 完成标准

报告触发条件、定位证据、根因或尚待证实的假设；实施修复时说明改动如何作用于该原因，以及实际运行的验证结果。区分自动化回归通过与真实客户端已验证，未执行的系统级场景明确列出。
