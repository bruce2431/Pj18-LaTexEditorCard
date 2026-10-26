@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 首选随后端携带的便携运行时（pdf/backend/runtime，与 tex/ 同理）
if exist "runtime\python.exe" (
  "runtime\python.exe" -B server.py
  goto :end
)
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
echo  [错误] 找不到 runtime\python.exe，也没有可用的系统 Python。请检查 runtime\ 是否完整。
echo.
pause
exit /b 1
:end
echo.
echo  服务器已停止。
pause
