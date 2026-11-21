@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "LINK=%~1"
if "%LINK%"=="" (
    echo ============================================
    echo        医疗影像云  一键下载工具
    echo ============================================
    echo.
    set /p "LINK=请粘贴影像分享链接，然后按回车: "
)
set "LINK=%LINK:"=%"
if "%LINK%"=="" goto :end

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] 没有检测到 Python。
    echo     请先安装 Python 3.9 或更高版本：https://www.python.org/downloads/
    echo     安装时务必勾选 "Add python.exe to PATH"。
    echo.
    pause
    goto :end
)

python -c "import requests, pydicom, numpy, PIL" >nul 2>&1
if errorlevel 1 (
    echo 首次运行，正在安装依赖，请稍候...
    python -m pip install --quiet requests pydicom numpy pillow
)

echo.
echo 开始下载，请勿关闭窗口...
echo.
python "%~dp0yjp_dl.py" "%LINK%" -o "%~dp0下载结果"
echo.
echo 输出目录: %~dp0下载结果
echo.
pause
:end
