@echo off
REM Build T-Mobile Bypass into a single windowed .exe (auto-requests admin)
cd /d "%~dp0"
python -m pip install --upgrade pip pyinstaller PySide6
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin --name "T-MobileBypass" tmobile_bypass.py
echo.
echo Output: dist\T-MobileBypass.exe
pause
