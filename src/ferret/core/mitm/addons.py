"""Ferret's reusable mitmproxy addons."""

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    HTTPFlow,
    TlsConfig,
    connection,
    human,
    server_hooks,
    status_codes,
    tlsconfig_module,
)
from ferret.core.settings import APP_NAME


class FerretTlsConfig(TlsConfig):
    """Use Ferret's name for generated certificate files."""

    def configure(self, updated):
        original = tlsconfig_module.CONF_BASENAME
        tlsconfig_module.CONF_BASENAME = APP_NAME
        try:
            super().configure(updated)
        finally:
            tlsconfig_module.CONF_BASENAME = original


class LogAddon:
    """Log the proxy connection and HTTP lifecycle."""

    def __init__(self) -> None:
        self._log = get_logger("mitmproxy")

    def client_connected(self, client: connection.Client) -> None:
        address = f"{client.peername[0]}:{client.peername[1]}"
        self._log.info("[%s] client connect", address)

    def server_connected(self, data: server_hooks.ServerConnectionHookData):
        client = data.client
        server = data.server
        client_address = f"{client.peername[0]}:{client.peername[1]}"
        server_address = (
            f"{server.address[0]}:{server.address[1]}" if server.address else "unknown"
        )
        ip_port = (
            f"{server.peername[0]}:{server.peername[1]}"
            if server.peername
            else "unknown"
        )
        self._log.info(
            "[%s] server connect %s (%s)",
            client_address,
            server_address,
            ip_port,
        )

    def request(self, flow: HTTPFlow) -> None:
        request = flow.request
        if request is None:
            return
        conn = flow.client_conn
        client_address = f"{conn.peername[0]}:{conn.peername[1]}"
        self._log.info(
            "%s %s %s %s",
            client_address,
            request.method,
            request.pretty_url,
            request.http_version,
            extra={"raw": True},
        )

    def response(self, flow: HTTPFlow) -> None:
        response = flow.response
        if response is None:
            return
        status = response.status_code
        reason = response.reason or status_codes.RESPONSES.get(status, "")
        friendly_size = human.pretty_size(
            len(response.content) if response.content else 0
        )
        self._log.info(
            "      << %s %s %s %s",
            response.http_version,
            status,
            reason,
            friendly_size,
            extra={"raw": True},
        )

    def error(self, flow: HTTPFlow) -> None:
        if flow.error is not None:
            self._log.info("      << %s", flow.error.msg, extra={"raw": True})

    def http_connect_error(self, flow: HTTPFlow) -> None:
        request = flow.request
        if request is None:
            return
        conn = flow.client_conn
        client_address = f"{conn.peername[0]}:{conn.peername[1]}"
        self._log.info(
            "%s %s %s %s",
            client_address,
            request.method,
            request.pretty_url,
            request.http_version,
            extra={"raw": True},
        )
        message = flow.error.msg if flow.error else "connection failed"
        self._log.info("      << %s", message, extra={"raw": True})


__ALL__ = [
    "LogAddon",
    "FerretTlsConfig",
]
