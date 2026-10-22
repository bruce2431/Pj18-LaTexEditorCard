@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py
  goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
  goto :end
)
echo.
echo  [错误] 没有找到 Python。请先安装 Python 3（python.org，勾选 Add to PATH），或告诉我来处理。
echo.
pause
exit /b 1
:end
echo.
echo  服务器已停止。
pause
