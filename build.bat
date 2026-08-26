@echo off
cd /d "%~dp0"
echo [1/3] Installing PySide6 + PyInstaller...
python -m pip install --quiet --upgrade PySide6 pyinstaller
echo [2/3] Building one-file exe (with UAC admin manifest)...
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin --name T-MobileBypass tmobile_bypass.py
echo [3/3] BUILD_DONE
