# nuitka-project: --mode=standalone
# nuitka-project: --output-dir=dist
# nuitka-project: --windows-console-mode=force
# nuitka-project: --output-filename=ferret
# nuitka-project: --output-folder-name=ferret
# nuitka-project: --report=dist/report.xml
# nuitka-project: --msvc=latest
# nuitka-project: --enable-plugins=pyside6
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
# nuitka-project: --noinclude-dlls=qdirect2d*
# nuitka-project: --noinclude-dlls=qminimal*
# nuitka-project: --noinclude-dlls=qoffscreen*
# nuitka-project: --noinclude-qt-translations
# ── 瘦身：排除永不加载的 mitmproxy addon 及其重型依赖（配合 services.py 的 sys.modules 桩）
# 注意：pyasn1 不能排除（aioquic → service_identity 运行时硬链）
# nuitka-project: --nofollow-import-to=mitmproxy.addons.onboarding
# nuitka-project: --nofollow-import-to=mitmproxy.addons.onboardingapp
# nuitka-project: --nofollow-import-to=mitmproxy.addons.proxyauth
# nuitka-project: --nofollow-import-to=mitmproxy.addons.cut
# nuitka-project: --nofollow-import-to=mitmproxy.addons.export
# nuitka-project: --nofollow-import-to=flask
# nuitka-project: --nofollow-import-to=werkzeug
# nuitka-project: --nofollow-import-to=jinja2
# nuitka-project: --nofollow-import-to=asgiref
# nuitka-project: --nofollow-import-to=click
# nuitka-project: --nofollow-import-to=blinker
# nuitka-project: --nofollow-import-to=itsdangerous
# nuitka-project: --nofollow-import-to=markupsafe
# nuitka-project: --nofollow-import-to=ldap3
# nuitka-project: --nofollow-import-to=bcrypt
# nuitka-project: --nofollow-import-to=pyperclip

from ferret.core.application import Application


def main():
    Application().run()


if __name__ == "__main__":
    main()
