@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo 安装完成，请运行“启动项目.cmd”。
pause
exit /b 0
:failed
echo 安装失败，请检查 Python 和网络连接。
pause
exit /b 1
