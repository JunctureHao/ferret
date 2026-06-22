import subprocess
import sys


class SystemProxyManager:
    @staticmethod
    def _run_ps(ps_script: str):
        """专门执行 PowerShell 脚本的方法"""
        try:
            # 不使用 shell=True，直接将列表传给 subprocess，避开引号转义地狱
            subprocess.run(
                ["powershell", "-Command", ps_script], check=True, capture_output=True
            )
            return True
        except subprocess.CalledProcessError as e:
            # 打印详细的错误输出，方便调试
            print(f"❌ PowerShell 执行失败: {e.stderr.decode(errors='ignore')}")
            return False

    @staticmethod
    def set_proxy(host="127.0.0.1", port=8080):
        platform = sys.platform
        proxy_addr = f"{host}:{port}"

        if platform == "win32":
            # 将所有内部引号改为单引号 '
            ps_cmd = (
                f"Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' -Name 'ProxyEnable' -Value 1; "
                f"Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' -Name 'ProxyServer' -Value '{proxy_addr}'; "
                f"Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' -Name 'ProxyOverride' -Value '<-loopback>'"
            )

            if SystemProxyManager._run_ps(ps_cmd):
                # 解除 UWP 回环限制
                try:
                    subprocess.run(
                        ["CheckNetIsolation.exe", "LoopbackExempt", "-a", "-alluser"],
                        capture_output=True,
                    )

                    # 强力刷新系统设置 (这几行代码 IDE 不会报语法错误，很安全)
                    import ctypes

                    wininet = ctypes.windll.Wininet
                    wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
                    wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
                except Exception:
                    pass
                print(f"✅ Windows 代理已开启: {proxy_addr}")
                return True

        elif platform == "darwin":
            # macOS 保持不变，逻辑很稳
            get_service = "networksetup -listnetworkserviceorder | grep -B 1 $(route -n get default | grep interface | awk '{print $2}') | head -n 1 | cut -d ' ' -f 2-"
            service = subprocess.getoutput(get_service).strip() or "Wi-Fi"

            # 使用列表执行命令最安全
            try:
                subprocess.run(
                    ["networksetup", "-setwebproxy", service, host, str(port)],
                    check=True,
                )
                subprocess.run(
                    ["networksetup", "-setsecurewebproxy", service, host, str(port)],
                    check=True,
                )
                subprocess.run(
                    ["networksetup", "-setproxybypassdomains", service, ""], check=True
                )
                subprocess.run(
                    ["networksetup", "-setwebproxystate", service, "on"], check=True
                )
                subprocess.run(
                    ["networksetup", "-setsecurewebproxystate", service, "on"],
                    check=True,
                )
                return True
            except Exception:
                return False

        elif platform.startswith("linux"):
            # Linux 直接执行也通常没问题
            base = "org.gnome.system.proxy"
            try:
                subprocess.run(["gsettings", "set", base, "mode", "manual"], check=True)
                subprocess.run(
                    ["gsettings", "set", f"{base}.http", "host", host], check=True
                )
                subprocess.run(
                    ["gsettings", "set", f"{base}.http", "port", str(port)], check=True
                )
                subprocess.run(
                    ["gsettings", "set", f"{base}.https", "host", host], check=True
                )
                subprocess.run(
                    ["gsettings", "set", f"{base}.https", "port", str(port)], check=True
                )
                subprocess.run(
                    ["gsettings", "set", base, "ignore-hosts", "[]"], check=True
                )
                return True
            except Exception:
                return False

        return False

    @staticmethod
    def unset_proxy():
        platform = sys.platform
        if platform == "win32":
            ps_cmd = "Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' -Name 'ProxyEnable' -Value 0"
            if SystemProxyManager._run_ps(ps_cmd):
                try:
                    import ctypes

                    ctypes.windll.Wininet.InternetSetOptionW(0, 39, 0, 0)
                    ctypes.windll.Wininet.InternetSetOptionW(0, 37, 0, 0)
                except Exception:
                    pass
                print("✅ Windows 代理已关闭")
                return True

        elif platform == "darwin":
            get_service = "networksetup -listnetworkserviceorder | grep -B 1 $(route -n get default | grep interface | awk '{print $2}') | head -n 1 | cut -d ' ' -f 2-"
            service = subprocess.getoutput(get_service).strip() or "Wi-Fi"
            try:
                subprocess.run(
                    ["networksetup", "-setwebproxystate", service, "off"], check=True
                )
                subprocess.run(
                    ["networksetup", "-setsecurewebproxystate", service, "off"],
                    check=True,
                )
                return True
            except Exception:
                return False

        elif platform.startswith("linux"):
            try:
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy", "mode", "none"],
                    check=True,
                )
                return True
            except Exception:
                return False

        return False
