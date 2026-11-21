@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   HD2 大厅搜索器 - 打包脚本
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [X] 没找到 python，请先安装 Python 3.9+ 并勾选 Add to PATH
    pause
    exit /b 1
)

echo [1/3] 安装依赖...
python -m pip install --upgrade pyinstaller pywebview
if errorlevel 1 goto fail

echo.
echo [2/3] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo [3/3] 打包中（约 30 秒）...
python -m PyInstaller --noconfirm --onefile --noconsole --name HD2LobbyBrowser ^
  --add-data "源码\planets.json;." ^
  --add-data "源码\planets_zh.json;." ^
  --add-data "源码\sectors_zh.json;." ^
  --add-data "源码\mission_names.json;." ^
  --add-data "源码\factions.json;." ^
  --add-data "源码\sdr_pops.json;." ^
  --collect-all webview --collect-all clr_loader --collect-all pythonnet ^
  --hidden-import webview.platforms.edgechromium "源码\hd2lobby_app.py"
if errorlevel 1 goto fail

echo.
echo ============================================
echo   完成：dist\HD2LobbyBrowser.exe
echo ============================================
echo   下一步：把这个 exe 上传到 GitHub Releases 的附件里
echo   详见「发布指南.md」
echo.
pause
exit /b 0

:fail
echo.
echo [X] 打包失败，请把上面的报错发我
pause
exit /b 1
