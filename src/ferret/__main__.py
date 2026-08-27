# ── 产物按构建时刻分目录 dist/YYYYmmdd_HHMM/，历史构建可并存
# {STAMP} 须先 -set 再引用；表达式在 Nuitka 自身命名空间 eval，那里没有 time，必须 __import__
# nuitka-project-set: STAMP = __import__("time").strftime("%Y%m%d_%H%M")
# nuitka-project: --mode=standalone
# nuitka-project: --output-dir=dist/{STAMP}
# .build 脚手架约 480MB/次，产出 exe 后删除；C 编译缓存是全局的，不影响增量速度
# nuitka-project: --remove-output
# nuitka-project: --windows-console-mode=force
# nuitka-project: --output-filename=Ferret
# nuitka-project: --output-folder-name=Ferret
# nuitka-project: --windows-icon-from-ico=src/ferret/resources/icon.ico
# nuitka-project: --report=dist/{STAMP}/report.xml
# nuitka-project: --msvc=latest
# nuitka-project: --lto=no
# nuitka-project: --enable-plugins=pyside6
# nuitka-project: --python-flag=no_docstrings
# nuitka-project: --python-flag=no_asserts
# nuitka-project: --nofollow-import-to=PySide6.QtWebEngineCore
# nuitka-project: --nofollow-import-to=PySide6.QtMultimedia
# nuitka-project: --nofollow-import-to=PySide6.QtOpenGL
# nuitka-project: --nofollow-import-to=PySide6.QtPdf
# nuitka-project: --nofollow-import-to=PySide6.QtSpatialAudio
# nuitka-project: --nofollow-import-to=PySide6.QtNetwork
# nuitka-project: --noinclude-dlls=qt6network*
# nuitka-project: --noinclude-dlls=qt6quick*
# nuitka-project: --noinclude-dlls=qt6pdf*
# nuitka-project: --noinclude-dlls=qt6qml*
# nuitka-project: --noinclude-dlls=qt6qmlmodels*
# nuitka-project: --noinclude-dlls=qt6qmlmeta*
# nuitka-project: --noinclude-dlls=qt6qmlworkerscript*
# nuitka-project: --noinclude-dlls=qt6virtualkeyboard*
# nuitka-project: --noinclude-dlls=qt6opengl*
# nuitka-project: --noinclude-dlls=msvcp*
# nuitka-project: --noinclude-qt-plugins=imageformats
# nuitka-project: --noinclude-qt-plugins=styles
# nuitka-project: --noinclude-qt-plugins=tls
# nuitka-project: --include-qt-plugins=platforms
# nuitka-project: --noinclude-dlls=*qdirect2d*
# nuitka-project: --noinclude-dlls=*qminimal*
# nuitka-project: --noinclude-dlls=*qoffscreen*
# nuitka-project: --noinclude-qt-translations
# ── 瘦身：排除永不加载的 mitmproxy addon 及其重型依赖（配合 core/mitm/bindings.py 的桩）
# 注意：pyasn1 不能排除（aioquic → service_identity 运行时硬链）
# nuitka-project: --nofollow-import-to=mitmproxy.addons.onboarding
# nuitka-project: --nofollow-import-to=mitmproxy.addons.onboardingapp
# nuitka-project: --nofollow-import-to=mitmproxy.addons.proxyauth
# nuitka-project: --nofollow-import-to=mitmproxy.addons.cut
# nuitka-project: --nofollow-import-to=mitmproxy.addons.browser
# nuitka-project: --nofollow-import-to=mitmproxy.addons.command_history
# nuitka-project: --nofollow-import-to=mitmproxy.addons.comment
# nuitka-project: --nofollow-import-to=mitmproxy.addons.termlog
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
# # nuitka-project: --nofollow-import-to=urwid
# nuitka-project: --nofollow-import-to=wcwidth
# nuitka-project: --nofollow-import-to=tornado
# nuitka-project: --nofollow-import-to=win32evtlog
# nuitka-project: --nofollow-import-to=win32evtlogutil
# nuitka-project: --nofollow-import-to=_wmi
# mitmproxy 只在 maplocal.py 里用了 werkzeug 的 safe_join 一个函数，bindings.py 已经
# 用等价实现顶掉。整包排掉的同时，colorama / markupsafe 也随之不可达（它们只有
# werkzeug 引用），不必单列。
# nuitka-project: --nofollow-import-to=werkzeug
# nuitka-project: --nofollow-import-to=concurrent.futures.process
# pyparsing/core.py:2567 的 `from .diagram import ...` 在 create_diagram() 函数体里，
# 外面就套着 except ImportError（提示 "pip install pyparsing[diagrams]"）。railroad 本来就
# 没装，这条路运行期必然 ImportError 并被吞掉 —— Nuitka 却照样把 diagram.py 编了进去。
# nuitka-project: --nofollow-import-to=pyparsing.diagram
# ── 瘦身：standalone 默认「把没被排除的标准库全塞进 __bytecode.const」，下面这些在
# 整个模块图里零引用者（report.xml 的 module_usages 反查，只有 Nuitka 记在 __main__
# 名下的那条伪引用），实测跑完 ferret 全量 import + FerretMaster 装配后也不进
# sys.modules。字节码 blob 基本不压缩（input 5,882,087 → blob 5,854,988），所以这里
# 省下的 ~695 KiB 是 1:1 落到 exe 上的，比编译模块的 0.35 折算划算。
# 刻意留着的两个：_sitebuiltins（site.py 启动时就加载）、_pylong（CPython 的 C 层在
# 超大整数 ↔ 字符串转换时自己 import，静态图里看不见引用者）。
# nuitka-project: --nofollow-import-to=_pyio
# nuitka-project: --nofollow-import-to=pickletools
# nuitka-project: --nofollow-import-to=configparser
# nuitka-project: --nofollow-import-to=imaplib
# nuitka-project: --nofollow-import-to=difflib
# nuitka-project: --nofollow-import-to=pstats
# nuitka-project: --nofollow-import-to=cgi
# nuitka-project: --nofollow-import-to=tomllib
# nuitka-project: --nofollow-import-to=trace
# nuitka-project: --nofollow-import-to=modulefinder
# nuitka-project: --nofollow-import-to=webbrowser
# nuitka-project: --nofollow-import-to=symtable
# nuitka-project: --nofollow-import-to=cgitb
# nuitka-project: --nofollow-import-to=_osx_support
# nuitka-project: --nofollow-import-to=fileinput
# nuitka-project: --nofollow-import-to=poplib
# nuitka-project: --nofollow-import-to=pkgutil
# nuitka-project: --nofollow-import-to=cmd
# nuitka-project: --nofollow-import-to=filecmp
# nuitka-project: --nofollow-import-to=pyclbr
# nuitka-project: --nofollow-import-to=xdrlib
# nuitka-project: --nofollow-import-to=mailcap
# nuitka-project: --nofollow-import-to=sndhdr
# nuitka-project: --nofollow-import-to=timeit
# nuitka-project: --nofollow-import-to=netrc
# nuitka-project: --nofollow-import-to=code
# nuitka-project: --nofollow-import-to=pipes
# nuitka-project: --nofollow-import-to=uu
# nuitka-project: --nofollow-import-to=graphlib
# nuitka-project: --nofollow-import-to=imghdr
# nuitka-project: --nofollow-import-to=rlcompleter
# nuitka-project: --nofollow-import-to=chunk
# nuitka-project: --nofollow-import-to=sched
# nuitka-project: --nofollow-import-to=colorsys
# nuitka-project: --nofollow-import-to=_aix_support
# nuitka-project: --nofollow-import-to=__phello__
# nuitka-project: --nofollow-import-to=__hello__
# nuitka-project: --nofollow-import-to=sre_constants
# nuitka-project: --nofollow-import-to=sre_compile
# nuitka-project: --nofollow-import-to=sre_parse
# nuitka-project: --noinclude-dlls=pythoncom*


from ferret.core.application import Application


def main():
    Application().run()


if __name__ == "__main__":
    main()
