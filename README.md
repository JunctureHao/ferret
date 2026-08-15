# 打包指令
```sh
nuitka .\src\ferret\
```
## 打包说明
* 详细指令存放 .\src\ferret\__main__.py 文件中

# 运行指令
```sh
uv run ferret
```

# 内置 Addon 功能对照（对比 mitmproxy）

状态图例：✅ 已实现　🟡 未实现（GUI 场景通常不需要）　❌ 未实现（功能缺口）

## 核心运行 Addon（已装载）

| Addon | 功能 | 状态 |
|-------|------|------|
| Core | 核心事件循环 | ✅ |
| Proxyserver | 代理服务器 | ✅ |
| NextLayer | 协议探测（HTTP/TLS/…） | ✅ |
| DnsResolver | DNS 解析 | ✅ |
| View | 流量视图与存储 | ✅ |
| ClientPlayback | 客户端重放 | ✅ |
| Save | 保存流量文件 | ✅ |
| FerretTlsConfig | 证书配置（自定义名称） | ✅ |
| LogAddon | 连接/HTTP 生命周期日志 | ✅ |
| FlowExporter | curl/httpie/raw 导出 | ✅ |

## 流量修改类（功能缺口）

| Addon | 功能 | 状态 |
|-------|------|------|
| intercept | 拦截/断点修改 | ❌ |
| modifyheaders | 修改请求/响应头 | ❌ |
| modifybody | 修改请求/响应体 | ❌ |
| maplocal | 本地文件映射（mock 响应） | ❌ |
| mapremote | 远程 URL 映射重写 | ❌ |
| stickycookie | 固化 Cookie | ❌ |
| stickyauth | 固化认证 | ❌ |
| anticache | 去除缓存头强制走源站 | ❌ |
| anticomp | 去除压缩头看明文 | ❌ |
| block / blocklist | 屏蔽请求 | ❌ |
| cut | 截断大 body | ❌ |
| disable_h2c | 禁用 h2c 升级 | ❌ |
| strip_dns_https_records | 剥离 DNS HTTPS 记录 | ❌ |
| update_alt_svc | 更新 alt-svc | ❌ |

## 重放 / 导入导出类

| Addon | 功能 | 状态 |
|-------|------|------|
| serverplayback | 服务端重放（mock 整响应） | ❌ |
| readfile | 读取 .flow 文件重放 | ❌ |
| savehar | 导出 HAR | ❌ |
| dumper | 流式 dump 到文件 | ❌ |
| export | mitmproxy 自带导出命令 | ❌ |
| asgiapp | 内嵌 ASGI 应用 | ❌ |

## 认证 / 代理链

| Addon | 功能 | 状态 |
|-------|------|------|
| proxyauth | 代理层认证 | ❌ |
| upstream_auth | 上游代理认证 | ❌ |

## 界面 / 辅助类（GUI 场景通常不需要）

| Addon | 功能 | 状态 |
|-------|------|------|
| onboarding / onboardingapp | Web 引导页 | 🟡 |
| termlog | 终端日志 | 🟡 |
| command_history | 命令历史 | 🟡 |
| comment | 流量备注 | 🟡 |
| eventstore | 事件存储 | 🟡 |
| browser | 打开浏览器 | 🟡 |
| script | 加载 Python 脚本 | 🟡 |
| keepserving | 保持运行 | 🟡 |
| errorcheck | 错误检查 | 🟡 |
| server_side_events | SSE 支持 | 🟡 |

