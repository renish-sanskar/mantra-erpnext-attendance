@echo off
cd /d "%~dp0"
net session >nul 2>&1 || (echo Right-click this file and choose "Run as administrator" & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv || (echo Python 3 not found. Install it from python.org first & pause & exit /b 1)
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
schtasks /Create /TN "MantraSync" /TR "\"%~dp0start_server.bat\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
netsh advfirewall firewall add rule name="MantraSync" dir=in action=allow protocol=TCP localport=5001 >nul
schtasks /Run /TN "MantraSync"
echo.
echo Installed. The server now starts with Windows. Open http://localhost:5001
pause
