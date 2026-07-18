[app]

# title of your application
title = ferret

# project root directory. default = The parent directory of input_file
project_dir = .

# source file entry point path. default = main.py
input_file = main.py

# directory where the executable output is generated
exec_directory = .\dist

# path to the project file relative to project_dir
project_file = pyproject.toml

# application icon
icon = .\.venv\Lib\site-packages\PySide6\scripts\deploy_lib\pyside_icon.ico

[python]

# python path
python_path = D:\workspace\0_dev\ferret\.venv\Scripts\python.exe

# python packages to install
packages = Nuitka==4.1.3

# buildozer = for deploying Android application
android_packages = buildozer==1.5.0,cython==0.29.33

[qt]

# paths to required qml files. comma separated
# normally all the qml files required by the project are added automatically
# design studio projects include the qml files using qt resources
qml_files = 

# excluded qml plugin binaries
excluded_qml_plugins = 

# qt modules used. comma separated
modules = 

# qt plugins used by the application. only relevant for desktop deployment
# for qt plugins used in android application see [android][plugins]
plugins = accessiblebridge,egldeviceintegrations,generic,iconengines,imageformats,platforminputcontexts,platforms,platforms/darwin,platformthemes,styles,wayland-decoration-client,wayland-graphics-integration-client,wayland-shell-integration,xcbglintegrations

[android]

# path to pyside wheel
wheel_pyside = 

# path to shiboken wheel
wheel_shiboken = 

# plugins to be copied to libs folder of the packaged application. comma separated
plugins = 

[nuitka]

# usage description for permissions requested by the app as found in the info.plist file
# of the app bundle. comma separated
# eg = extra_args = --show-modules --follow-stdlib
macos.permissions = 

# mode of using nuitka. accepts standalone or onefile. default = onefile
mode = standalone

# specify any extra nuitka arguments
extra_args = 
	--enable-plugin=anti-bloat
	--noinclude-qt-translations
	--report=report
	--jobs=9
	--msvc=latest
	--nofollow-import-to=PySide6.QtWebEngineCore
	--nofollow-import-to=PySide6.QtMultimedia
	--nofollow-import-to=PySide6.QtOpenGL
	--nofollow-import-to=PySide6.QtPdf
	--nofollow-import-to=PySide6.QtSpatialAudio
	--nofollow-import-to=PySide6.QtNetwork
	--noinclude-dlls=qt6network*.dll
	--noinclude-dlls=qt6quick*.dll
	--noinclude-dlls=qt6pdf*.dll
	--noinclude-dlls=qt6qml*.dll
	--noinclude-dlls=qt6qmlmodels*.dll
	--noinclude-dlls=qt6qmlmeta*.dll
	--noinclude-dlls=qt6qmlworkerscript*.dll
	--noinclude-dlls=qt6virtualkeyboard*.dll
	--noinclude-dlls=qt6opengl.dll
	
	--include-module=pygments.lexers.data
	--include-module=pygments.lexers.html
	--include-module=pygments.lexers.textfmts
	--include-module=pygments.lexers.css
	--include-module=pygments.lexers.javascript
	--include-module=pygments.lexers.jvm
	--include-module=pygments.lexers.ruby
	--include-module=pygments.styles.material
	--include-module=pygments.token
	
	--nofollow-import-to=mitmproxy.tools.web
	--nofollow-import-to=ldap3

[buildozer]

# build mode
# possible values = ["aarch64", "armv7a", "i686", "x86_64"]
# release creates a .aab, while debug creates a .apk
mode = debug

# path to pyside6 and shiboken6 recipe dir
recipe_dir = 

# path to extra qt android .jar files to be loaded by the application
jars_dir = 

# if empty, uses default ndk path downloaded by buildozer
ndk_path = 

# if empty, uses default sdk path downloaded by buildozer
sdk_path = 

# other libraries to be loaded at app startup. comma separated.
local_libs = 

# architecture of deployed platform
arch = 

