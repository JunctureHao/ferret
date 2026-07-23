# nuitka-project: --mode=standalone
# nuitka-project: --output-dir=dist
# nuitka-project: --windows-console-mode=force
# nuitka-project: --output-filename=ferret
# nuitka-project: --output-folder-name=ferret
# nuitka-project: --report=dist/report.xml
# nuitka-project: --msvc=latest
# nuitka-project: --enable-plugins=pyside6
# nuitka-project: --include-qt-plugins=platforms
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
# nuitka-project: --noinclude-qt-plugins=iconengines
# nuitka-project: --noinclude-qt-plugins=imageformats
# nuitka-project: --noinclude-qt-plugins=styles
# nuitka-project: --noinclude-qt-plugins=tls
# nuitka-project: --noinclude-qt-translations

from ferret.core.application import Application


def main():
    Application().run()


if __name__ == "__main__":
    main()
