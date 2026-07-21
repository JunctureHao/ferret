"""
封装 mitmproxy 的导出 API，支持多平台自动适配
"""

import sys
import re
from mitmproxy.flow import Flow
from mitmproxy.addons.export import (
    curl_command,
    httpie_command,
    raw,
    raw_request,
    raw_response,
)


class FlowExporter:
    """流量导出器 - 使用 mitmproxy 原生 API"""

    @staticmethod
    def to_curl(flow_obj: Flow) -> str:
        """导出为 curl 命令，自动适配当前操作系统
        
        平台适配规则：
        - Windows (win32): 将单引号转换为双引号，兼容 cmd.exe
        - macOS (darwin): 使用原生格式（单引号），兼容 bash/zsh
        - Linux (linux): 使用原生格式（单引号），兼容 bash/zsh
        """
        cmd = curl_command(flow_obj)
        
        # 根据平台选择适配方案
        platform = sys.platform
        if platform == "win32":
            # Windows: cmd.exe 不支持单引号，转换为双引号
            cmd = FlowExporter._to_windows_curl(cmd)
        return cmd
    
    @staticmethod
    def _to_windows_curl(cmd: str) -> str:
        """将 Unix curl 命令转为 Windows cmd 兼容格式（单引号→双引号）"""
        # 提取单引号内容，转义内部双引号，再用双引号包裹
        def replace_quotes(match):
            content = match.group(1)
            escaped = content.replace('"', r'\"')
            return f'"{escaped}"'
        
        return re.sub(r"'([^']*)'", replace_quotes, cmd)

    @staticmethod
    def to_httpie(flow_obj: Flow) -> str:
        """导出为 httpie 命令"""
        return httpie_command(flow_obj)

    @staticmethod
    def to_raw_request(flow_obj: Flow) -> bytes:
        """导出原始请求"""
        return raw_request(flow_obj)

    @staticmethod
    def to_raw_response(flow_obj: Flow) -> bytes:
        """导出原始响应"""
        return raw_response(flow_obj)

    @staticmethod
    def to_raw(flow_obj: Flow) -> bytes:
        """导出原始请求和响应"""
        return raw(flow_obj)
