# 开发决策与实现入口

本文保留需要随源码分发的设计依据。具体选项与链位以实现和测试为准，开发红线见 [AGENTS.md](../AGENTS.md)。

## capture

五种接入模式由 mitmproxy 原生 mode 组合。系统代理只负责把本机客户端指向 regular；上游 HTTP(S) 代理替换 regular 的出口。应用启动允许 regular 底座监听，「开始」才接通所选通道并接收外部流量记录。「停止」保留内核供 Compose 使用。界面入表集合与内核 View 分开维护，刷新、统计和导出遵循同一入表集合。

入口：`core/mitm/modes.py`、`runtime.py`、`apps/capture/controllers.py`；验证：`test_modes.py`、capture 控制器测试。

## auth

代理认证管接入权限，网关管抓取和转发策略。ProxyAuth 在网关、脚本和 Mock 前执行；认证挑战不能被 Mock 作答覆盖。凭证在认证后从 flow metadata 清除。SOCKS5 支持 TCP CONNECT 与同一代理认证；local/WireGuard/reverse 与认证的兼容闸门由 `_effective_proxyauth` 统一决定。

系统代理 attach 前持久化原配置并持有所有者锁。恢复时在同一所有者锁内读取、应用与清理 journal，避免使用其他实例更换前的旧快照。恢复失败、无法读取当前状态或部分恢复，都必须保留恢复依据，成功恢复后才清理 journal。

入口：`core/mitm/master.py`、`addons.py`、`packages/sysproxy/src/sysproxy/service.py`。

## tls

客户端信任 Ferret CA、Ferret 信任上游根证书、Ferret 向上游出示 mTLS 证书是三个独立方向。上游校验复用原生 TLS 选项；公共根与用户根按内容指纹合并。热更失败必须保留旧选项引用的信任文件。同路径替换 mTLS 证书后清原生 TLS context 缓存。

证书详情只展示用于排查握手的有限链字段。运行数据放 Roaming，不能与 Velopack 安装目录共用生命周期。

入口：`core/mitm/certificate.py`、`runtime.py`、`apps/certificate/`。

## rewrite

同一钩子内按规则行序执行。响应头在 responseheaders 修改，响应体在 response 修改。流式正文不能在已交付字节后伪装为修改成功。体替换文本按字面量处理，MAP_REMOTE 保留正则替换模板语义；读取文件失败不能留下半修改报文。

入口：`core/mitm/rewrite.py`、`addons.py::FerretRewriteAddon`；验证：`test_rewrite.py`。

## scripts

脚本以应用权限执行，默认不开总开关。应用内新建脚本可编辑、保存与选择删除文件；导入脚本只引用原路径，移除条目不删除用户文件。保存失败保留正文和选区。加载模块采用每个路径独立命名，登记 sys.modules，失败清理，重载不复用旧 pyc。

装载/卸载由 Ferret 管理，流量钩子借原生 addon 派发；脚本看到重写后的报文，位于网关之后、Mock 之前。加载失败的脚本不参与派发，其他脚本仍可运行。

入口：`core/mitm/scripts.py`、`addons.py::FerretScriptAddon`、`apps/scripts/`。

## mock

持久素材池和一次性剩余播放队列是两个集合。总开关重新打开会装载完整素材池；删除素材不能复活其他已消费条目。Mock 复用原生 ServerPlayback，位于脚本后、断点前。认证挑战保留；脚本可先改请求，Mock 响应仍过响应期处理。

未命中策略只在原生播放队列非空时生效；队列耗尽后直连。素材采用原生 .flow 文件保存，元数据不塞入 config.json。

入口：`core/mitm/master.py`、`facade.py`、`apps/mock/`；验证：`test_serverplayback.py`。

## sse

SSE tee 观察并原样转发字节，观察失败不能中断传输。压缩体增量解码；不支持的编码降级为只转发。response、error 与内核停止统一幂等收尾；静态 Mock/脚本响应也必须产生完整终态。

正文缓冲、事件档案和消息控件分别限制容量，截断需留下标记。界面心跳与普通气泡共同计数。WebSocket 使用原生帧状态，不另造传输层。

入口：`core/mitm/sse.py`、`apps/common/flow/messages.py`、`chat.py`。

## editing

未编辑的 URL、头部和正文保留原字节。收集表格值前先提交活动单元格；脏标记判断必须发生在提交之后。正文 None 表示不修改，显式空字节表示清空；Content-Length 由 mitmproxy 在实际修改正文时处理。Compose 与回放沿原生 replay 语义，不自动命中排除 replay 的断点。

头部行身份随文本撤销/重做保存，不能从显示相同的文字猜测原字节；装载源数据时清除程序设置格式产生的撤销历史。

入口：`apps/common/edit/`、`apps/intercept/editors.py`、`apps/compose/views.py`。

## ui

搜索使用一条原生 flowfilter 表达式；显示标记与过滤字段保持独立。列布局按稳定字段 key 保存，平铺与连接树共享偏好，窗口变窄导致的临时隐藏不落盘。列宽连续变化防抖写盘，退出前冲刷。

一次性菜单在关闭后释放；阻塞对话框读取结果后在 finally 中释放。排序只搬动现有控件，不重复连接信号。耗时业务使用 FunctionTask 并持有到 finished，完成时断开捕获任务的回调。

页面与重型视图按需创建，不在启动后自动预热。详情折叠时，普通选择只传行标识；双击、Enter 或展开分割栏才读取轻量摘要并创建当前标签。各侧 Body 在首次打开时解码、美化，Raw 在显示时取原始报文；换流后隐藏标签标记为待刷新。完整详情接口保留给导出与 Compose 等消费者。连接树首次使用从当前行快照重建，未创建时不积压事件，统计只取当前模式。JSON 树是文本的派生视图，只在显示时解析。

消息页未打开时只维护类型和计数，首次打开通过门面读取一次完整快照；接收与渲染分别记录消息序号，既过滤快照已包含的排队信号，也能在同一流量切回消息页时只补未显示的增量，保留过滤和清空显示状态。换流或更换控制器才重建消息内容。概览解压后大小在读取 Body 后补齐；存在原生 backup 的精确修改状态只在概览显示时比较。

入口：`apps/common/flow/`、`apps/common/tasks.py`、`apps/common/qfw_patch.py`。主题补丁的版本条件和原因保留在代码注释与测试里。

## export

HAR 和命令导出复用原生 API，CSV 是 GUI 字段抽取能力，不挂命令行 cut/export addon。Windows curl 面向 cmd.exe，保留命令行引号与元字符适配。流量备注写回经 facade，导出读取完整快照。时间瀑布只使用实际存在且有序的协议时间戳，不补造缺失阶段。

历史导入不暂停全局录制；只排除当前导入任务的历史事件，派生网络任务仍正常录制。导入读取打开时的文件前缀，同 ID 最后版本在原非活跃对象上更新，不能覆盖正在运行或拦截的请求；该版本被网关排除时保留原记录。停止录制失败必须向调用方报告并保留重试依据，不能把 addon 的日志当成成功关闭。

入口：`core/mitm/export.py`、`apps/common/flow/csv_export.py`、`timing.py`。

## update

安装版通过 Velopack 检查、下载和应用更新；源码与便携版转到发布页。应用更新会终止进程，必须先完成正常 shutdown，清理失败不调用 SDK。更新任务强引用和信号连接在 finished 后释放。

发布门禁覆盖主套件和独立 sysproxy 包，PR 只验证。比较所有非草稿 release 的 SemVer，API 失败不能当首次发布；发布上传串行且不自动取消。打包干跑不依赖已有编译产物。

入口：`core/update.py`、`apps/settings/controllers.py`、`apps/window.py`、`scripts/package.py`、`.github/workflows/release.yml`。
