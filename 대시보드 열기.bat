@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

echo 데이터를 받아 대시보드를 만드는 중입니다. (30초~1분)
echo.
python kr_macro\build.py
if errorlevel 1 (
  echo.
  echo 대시보드를 만들지 못했습니다. 위 메시지를 확인하세요.
  pause
  exit /b 1
)

rem 확인용으로 실행할 때는 "대시보드 열기.bat --no-open" 으로 브라우저를 열지 않는다.
if /i "%~1"=="--no-open" exit /b 0
start "" "%~dp0kr_macro\output\dashboard.html"
