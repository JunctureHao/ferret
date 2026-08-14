"""Export mitmproxy HTTP flows to commands and raw messages."""

import re
import shlex
import sys

from ferret.core.mitm.bindings import (
    HTTPFlow,
    Request,
    Response,
    assemble_request,
    assemble_response,
)


class FlowExporter:
    """Export flows without depending on mitmproxy's runtime context."""

    @staticmethod
    def curl_command(flow: HTTPFlow) -> str:
        request = FlowExporter._cleanup_request(flow)
        FlowExporter._pop_headers(request)
        args = ["curl"]
        for key, value in request.headers.items(multi=True):
            if key.lower() == "accept-encoding":
                args.append("--compressed")
            else:
                args += ["-H", f"{key}: {value}"]
        if request.method != "GET":
            if not request.content:
                args += ["-H", "content-length: 0"]
            args += ["-X", request.method]
        args.append(request.pretty_url)
        command = " ".join(shlex.quote(argument) for argument in args)
        if request.content:
            command += f" -d {FlowExporter._request_content_for_console(request)}"
        if sys.platform == "win32":
            command = FlowExporter._to_windows_curl(command)
        return command

    @staticmethod
    def httpie_command(flow: HTTPFlow) -> str:
        request = FlowExporter._cleanup_request(flow)
        FlowExporter._pop_headers(request)
        args = ["http", request.method, request.pretty_url]
        for key, value in request.headers.items(multi=True):
            args.append(f"{key}: {value}")
        command = " ".join(shlex.quote(argument) for argument in args)
        if request.content:
            command += " <<< " + FlowExporter._request_content_for_console(request)
        return command

    @staticmethod
    def raw_request(flow: HTTPFlow) -> bytes:
        request = FlowExporter._cleanup_request(flow)
        if request.raw_content is None:
            raise ValueError("Request content missing.")
        return assemble_request(request)

    @staticmethod
    def raw_response(flow: HTTPFlow) -> bytes:
        response = FlowExporter._cleanup_response(flow)
        if response.raw_content is None:
            raise ValueError("Response content missing.")
        return assemble_response(response)

    @staticmethod
    def raw(flow: HTTPFlow, separator: bytes = b"\r\n\r\n") -> bytes:
        request_present = bool(flow.request and flow.request.raw_content is not None)
        response_present = bool(flow.response and flow.response.raw_content is not None)
        if request_present and response_present:
            parts = [
                FlowExporter.raw_request(flow),
                FlowExporter.raw_response(flow),
            ]
            if flow.websocket:
                parts.append(flow.websocket._get_formatted_messages())
            return separator.join(parts)
        if request_present:
            return FlowExporter.raw_request(flow)
        if response_present:
            return FlowExporter.raw_response(flow)
        raise ValueError("Can't export flow with no request or response.")

    @staticmethod
    def _cleanup_request(flow: HTTPFlow) -> Request:
        if not flow.request:
            raise ValueError("Can't export flow with no request.")
        request = flow.request.copy()
        request.decode(strict=False)
        return request

    @staticmethod
    def _cleanup_response(flow: HTTPFlow) -> Response:
        if not flow.response:
            raise ValueError("Can't export flow with no response.")
        response = flow.response.copy()
        response.decode(strict=False)
        return response

    @staticmethod
    def _pop_headers(request: Request) -> None:
        request.headers.pop("content-length", None)
        if request.headers.get("host", "") == request.host:
            request.headers.pop("host")
        if request.headers.get(":authority", "") == request.host:
            request.headers.pop(":authority")

    @staticmethod
    def _request_content_for_console(request: Request) -> str:
        try:
            text = request.get_text(strict=True)
        except ValueError:
            raise ValueError("Request content must be valid unicode") from None
        if not text:
            raise ValueError("Request content must be valid unicode")
        escape_control_chars = {chr(index): f"\\x{index:02x}" for index in range(32)}
        escaped_text = "".join(escape_control_chars.get(char, char) for char in text)
        if any(char in escape_control_chars for char in text):
            return f'"$(printf {shlex.quote(escaped_text)})"'
        return shlex.quote(escaped_text)

    @staticmethod
    def _to_windows_curl(command: str) -> str:
        def replace_quotes(match):
            content = match.group(1)
            escaped = content.replace('"', r"\"")
            return f'"{escaped}"'

        return re.sub(r"'([^']*)'", replace_quotes, command)
