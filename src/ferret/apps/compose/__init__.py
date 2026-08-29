"""手工请求编辑页：用户自己拼 method/URL/headers/body，交给 mitmproxy 内核发出。

发送走 `MitmFacade.send_custom_request` —— 与抓包列表的「重放」同一条路
（重写 / 网关 / 断点规则照常命中），「进入流量列表」只是输入框里的一枚开关图标。
响应落地后经 `MitmRuntime.compose_result` 信号回来，右侧直接消费
`build_flow_detail` 的详情字典，与抓包详情面板同一份口径。
"""
