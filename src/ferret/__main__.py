# ═══════════════════════════════════════════════════════════════════════
# Nuitka 基础配置
# ═══════════════════════════════════════════════════════════════════════
# nuitka-project-set: STAMP = __import__("time").strftime("%Y%m%d_%H%M")
# nuitka-project: --mode=standalone
# nuitka-project: --output-dir=dist/{STAMP}
# nuitka-project: --output-filename=Ferret
# nuitka-project: --output-folder-name=Ferret
# nuitka-project: --windows-icon-from-ico=src/ferret/resources/icon.ico
# nuitka-project: --report=dist/{STAMP}/report.xml
# nuitka-project: --msvc=latest
# nuitka-project: --lto=no
# nuitka-project: --remove-output
# nuitka-project: --windows-console-mode=force
# nuitka-project: --python-flag=no_docstrings
# nuitka-project: --python-flag=no_asserts

# ═══════════════════════════════════════════════════════════════════════
# PySide6 / Qt 相关
# ═══════════════════════════════════════════════════════════════════════
# nuitka-project: --enable-plugins=pyside6

# nuitka-project: --include-package=PySide6.QtXml
# nuitka-project: --include-package=PySide6.QtSvg

# 不用的 Qt 模块，静态分析不可达，显式排除防误打包
# nuitka-project: --nofollow-import-to=PySide6.QtWebEngineCore
# nuitka-project: --nofollow-import-to=PySide6.QtMultimedia
# nuitka-project: --nofollow-import-to=PySide6.QtOpenGL
# nuitka-project: --nofollow-import-to=PySide6.QtPdf
# nuitka-project: --nofollow-import-to=PySide6.QtSpatialAudio
# nuitka-project: --nofollow-import-to=PySide6.QtNetwork

# 对应 Qt DLL / QML 运行时，零代码引用
# nuitka-project: --noinclude-dlls=qt6network*
# nuitka-project: --noinclude-dlls=qt6quick*
# nuitka-project: --noinclude-dlls=qt6pdf*
# nuitka-project: --noinclude-dlls=qt6qml*
# nuitka-project: --noinclude-dlls=qt6qmlmodels*
# nuitka-project: --noinclude-dlls=qt6qmlmeta*
# nuitka-project: --noinclude-dlls=qt6qmlworkerscript*
# nuitka-project: --noinclude-dlls=qt6virtualkeyboard*
# nuitka-project: --noinclude-dlls=qt6opengl*
# nuitka-project: --noinclude-dlls=*shiboken6*msvcp*
# nuitka-project: --noinclude-dlls=*qdirect2d*
# nuitka-project: --noinclude-dlls=*qminimal*
# nuitka-project: --noinclude-dlls=*qoffscreen*

# Qt 插件 / 翻译
# nuitka-project: --noinclude-qt-plugins=imageformats
# nuitka-project: --noinclude-qt-plugins=styles
# nuitka-project: --noinclude-qt-plugins=tls
# nuitka-project: --include-qt-plugins=platforms
# nuitka-project: --noinclude-qt-translations

# ═══════════════════════════════════════════════════════════════════════
# mitmproxy 及关联依赖
# ═══════════════════════════════════════════════════════════════════════
# sysproxy 是 uv workspace 成员（editable 安装），显式声明防剪枝误裁
# nuitka-project: --include-package=sysproxy

# 永不加载的 mitmproxy 命令行 addon（配合 core/mitm/bindings.py 的桩）
# 注意：pyasn1 不能排除（aioquic → service_identity 运行时硬链）
# nuitka-project: --nofollow-import-to=mitmproxy.addons.onboarding
# nuitka-project: --nofollow-import-to=mitmproxy.addons.onboardingapp
# nuitka-project: --nofollow-import-to=mitmproxy.addons.proxyauth
# nuitka-project: --nofollow-import-to=mitmproxy.addons.cut
# nuitka-project: --nofollow-import-to=mitmproxy.addons.browser
# nuitka-project: --nofollow-import-to=mitmproxy.addons.command_history
# nuitka-project: --nofollow-import-to=mitmproxy.addons.comment
# nuitka-project: --nofollow-import-to=mitmproxy.addons.termlog

# mitmproxy 关联但 ferret 不用的重型依赖
# nuitka-project: --nofollow-import-to=flask
# nuitka-project: --nofollow-import-to=jinja2
# nuitka-project: --nofollow-import-to=asgiref
# nuitka-project: --nofollow-import-to=click
# nuitka-project: --nofollow-import-to=blinker
# nuitka-project: --nofollow-import-to=itsdangerous
# nuitka-project: --nofollow-import-to=ldap3
# nuitka-project: --nofollow-import-to=bcrypt
# nuitka-project: --nofollow-import-to=pyperclip
# nuitka-project: --nofollow-import-to=zstandard.backend_cffi
# nuitka-project: --noinclude-dlls=*zstandard*_cffi*
# nuitka-project: --nofollow-import-to=wcwidth
# nuitka-project: --nofollow-import-to=tornado

# mitmproxy 只在 maplocal.py 用 werkzeug.safe_join，bindings.py 已用等价实现顶掉。
# 整包排掉，colorama / markupsafe 也随之不可达（它们只有 werkzeug 引用），不必单列。
# nuitka-project: --nofollow-import-to=werkzeug

# ═══════════════════════════════════════════════════════════════════════
# pywin32 相关
# ═══════════════════════════════════════════════════════════════════════
# Windows 事件日志 / WMI，ferret 不引用
# nuitka-project: --nofollow-import-to=win32evtlog
# nuitka-project: --nofollow-import-to=win32evtlogutil
# nuitka-project: --nofollow-import-to=_wmi

# pythoncom312.dll：.dll 走标准 DLL 收集，--noinclude-dlls 能管
# nuitka-project: --noinclude-dlls=pythoncom*

# ═══════════════════════════════════════════════════════════════════════
# 标准库零引用者（按字母序维护）
# ═══════════════════════════════════════════════════════════════════════
# nuitka-project: --nofollow-import-to=__hello__
# nuitka-project: --nofollow-import-to=__phello__
# nuitka-project: --nofollow-import-to=_aix_support
# nuitka-project: --nofollow-import-to=_markupbase
# nuitka-project: --nofollow-import-to=_osx_support
# nuitka-project: --nofollow-import-to=_pyio
# nuitka-project: --nofollow-import-to=_pydecimal
# nuitka-project: --nofollow-import-to=_pydatetime
# nuitka-project: --nofollow-import-to=aifc
# nuitka-project: --nofollow-import-to=bdb
# nuitka-project: --nofollow-import-to=cgi
# nuitka-project: --nofollow-import-to=cgitb
# nuitka-project: --nofollow-import-to=chunk
# nuitka-project: --nofollow-import-to=cmd
# nuitka-project: --nofollow-import-to=code
# nuitka-project: --nofollow-import-to=codeop
# nuitka-project: --nofollow-import-to=colorsys
# nuitka-project: --nofollow-import-to=configparser
# nuitka-project: --nofollow-import-to=difflib
# nuitka-project: --nofollow-import-to=email._header_value_parser
# nuitka-project: --nofollow-import-to=email.contentmanager
# nuitka-project: --nofollow-import-to=email.headerregistry
# nuitka-project: --nofollow-import-to=email.policy
# nuitka-project: --nofollow-import-to=filecmp
# nuitka-project: --nofollow-import-to=fileinput
# nuitka-project: --nofollow-import-to=getopt
# nuitka-project: --nofollow-import-to=graphlib
# nuitka-project: --nofollow-import-to=html.parser
# nuitka-project: --nofollow-import-to=imghdr
# nuitka-project: --nofollow-import-to=imaplib
# nuitka-project: --nofollow-import-to=importlib.simple
# nuitka-project: --nofollow-import-to=mailbox
# nuitka-project: --nofollow-import-to=mailcap
# nuitka-project: --nofollow-import-to=modulefinder
# nuitka-project: --nofollow-import-to=netrc
# nuitka-project: --nofollow-import-to=nntplib
# nuitka-project: --nofollow-import-to=optparse
# nuitka-project: --nofollow-import-to=pdb
# nuitka-project: --nofollow-import-to=pickletools
# nuitka-project: --nofollow-import-to=pipes
# nuitka-project: --nofollow-import-to=pkgutil
# nuitka-project: --nofollow-import-to=poplib
# nuitka-project: --nofollow-import-to=pstats
# nuitka-project: --nofollow-import-to=pyclbr
# nuitka-project: --nofollow-import-to=rlcompleter
# nuitka-project: --nofollow-import-to=sched
# nuitka-project: --nofollow-import-to=sndhdr
# nuitka-project: --nofollow-import-to=sre_compile
# nuitka-project: --nofollow-import-to=sre_constants
# nuitka-project: --nofollow-import-to=sre_parse
# nuitka-project: --nofollow-import-to=sunau
# nuitka-project: --nofollow-import-to=symtable
# nuitka-project: --nofollow-import-to=statistics
# nuitka-project: --nofollow-import-to=sysconfig
# nuitka-project: --nofollow-import-to=tarfile
# nuitka-project: --nofollow-import-to=timeit
# nuitka-project: --nofollow-import-to=tomllib
# nuitka-project: --nofollow-import-to=trace
# nuitka-project: --nofollow-import-to=turtle
# nuitka-project: --nofollow-import-to=uu
# nuitka-project: --nofollow-import-to=webbrowser
# nuitka-project: --nofollow-import-to=xml.sax.expatreader
# nuitka-project: --nofollow-import-to=xdrlib

# ═══════════════════════════════════════════════════════════════════════
# 杂项
# ═══════════════════════════════════════════════════════════════════════
# concurrent.futures.process：ferret 不用 multiprocessing
# nuitka-project: --nofollow-import-to=concurrent.futures.process
# pyparsing.diagram：create_diagram() 函数体里的 import，外层 except ImportError 吞掉，
# Nuitka 却照样编进去
# nuitka-project: --nofollow-import-to=pyparsing.diagram


from ferret.core.application import Application


def main():
    Application().run()


if __name__ == "__main__":
    main()
