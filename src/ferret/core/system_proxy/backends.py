"""Platform implementations for reading and changing the system proxy."""

from __future__ import annotations

import contextlib
import ctypes
import subprocess
import sys
from abc import ABC, abstractmethod

from ferret.core.system_proxy.models import ProxyEndpoint, ProxySnapshot


class SystemProxyBackend(ABC):
    @abstractmethod
    def snapshot(self) -> ProxySnapshot: ...

    @abstractmethod
    def set(self, endpoint: ProxyEndpoint) -> bool: ...

    @abstractmethod
    def restore(self, snapshot: ProxySnapshot) -> bool: ...

    @abstractmethod
    def owns(self, endpoint: ProxyEndpoint) -> bool: ...


class WindowsSystemProxyBackend(SystemProxyBackend):
    _KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    _NAMES = (
        "ProxyEnable",
        "ProxyServer",
        "ProxyOverride",
        "AutoConfigURL",
        "AutoDetect",
    )

    @staticmethod
    def _winreg():
        import winreg

        return winreg

    def snapshot(self) -> ProxySnapshot:
        winreg = self._winreg()
        values = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._KEY_PATH) as key:
            for name in self._NAMES:
                try:
                    value, value_type = winreg.QueryValueEx(key, name)
                    values[name] = (True, value, value_type)
                except FileNotFoundError:
                    values[name] = (False, None, None)
        return ProxySnapshot(values)

    def set(self, endpoint: ProxyEndpoint) -> bool:
        winreg = self._winreg()
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self._KEY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(
                    key, "ProxyServer", 0, winreg.REG_SZ, endpoint.address
                )
                winreg.SetValueEx(
                    key, "ProxyOverride", 0, winreg.REG_SZ, "<-loopback>"
                )
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, "AutoConfigURL")
                winreg.SetValueEx(key, "AutoDetect", 0, winreg.REG_DWORD, 0)
            self._refresh()
            with contextlib.suppress(OSError):
                subprocess.run(
                    ["CheckNetIsolation.exe", "LoopbackExempt", "-a", "-alluser"],
                    capture_output=True,
                    check=False,
                )
            return True
        except OSError:
            return False

    def restore(self, snapshot: ProxySnapshot) -> bool:
        winreg = self._winreg()
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self._KEY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                for name in self._NAMES:
                    exists, value, value_type = snapshot.values.get(
                        name, (False, None, None)
                    )
                    if exists:
                        winreg.SetValueEx(key, name, 0, value_type, value)
                    else:
                        with contextlib.suppress(FileNotFoundError):
                            winreg.DeleteValue(key, name)
            self._refresh()
            return True
        except OSError:
            return False

    def owns(self, endpoint: ProxyEndpoint) -> bool:
        winreg = self._winreg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._KEY_PATH) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                override, _ = winreg.QueryValueEx(key, "ProxyOverride")
                try:
                    auto_detect, _ = winreg.QueryValueEx(key, "AutoDetect")
                except FileNotFoundError:
                    # WinINet may remove AutoDetect after it is set to 0. A
                    # missing value therefore still means auto-detect is off.
                    auto_detect = 0
                try:
                    auto_config_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
                except FileNotFoundError:
                    auto_config_url = ""
            return (
                bool(enabled)
                and str(server) == endpoint.address
                and str(override) == "<-loopback>"
                and not bool(auto_detect)
                and not str(auto_config_url)
            )
        except OSError:
            return False

    @staticmethod
    def _refresh() -> None:
        with contextlib.suppress(OSError, AttributeError):
            wininet = ctypes.windll.Wininet
            wininet.InternetSetOptionW(0, 39, 0, 0)
            wininet.InternetSetOptionW(0, 37, 0, 0)


class UnsupportedSystemProxyBackend(SystemProxyBackend):
    def snapshot(self) -> ProxySnapshot:
        return ProxySnapshot()

    def set(self, endpoint: ProxyEndpoint) -> bool:
        return False

    def restore(self, snapshot: ProxySnapshot) -> bool:
        return True

    def owns(self, endpoint: ProxyEndpoint) -> bool:
        return False


def create_system_proxy_backend() -> SystemProxyBackend:
    if sys.platform == "win32":
        return WindowsSystemProxyBackend()
    return UnsupportedSystemProxyBackend()
