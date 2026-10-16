@echo off
cd /d "%~dp0"
where pythonw >nul 2>&1
if %errorlevel% equ 0 (
    start "" pythonw "%~dp0reader.py"
    exit /b
)
where pyw >nul 2>&1
if %errorlevel% equ 0 (
    start "" pyw -3 "%~dp0reader.py"
    exit /b
)
echo Python 3.10+ with Tkinter is required. See README.md.
pause
