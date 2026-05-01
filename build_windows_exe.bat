@echo off
setlocal

cd /d %~dp0

if not exist .venv (
  py -3 -m venv .venv
)

call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm --clean --onedir --name whatsapp_downloader --collect-all playwright main.py
pyinstaller --noconfirm --clean --onefile --windowed --name whatsapp_downloader_config config_ui.py

if exist dist\whatsapp_downloader (
  copy /Y dist\whatsapp_downloader_config.exe dist\whatsapp_downloader\whatsapp_downloader_config.exe >nul
)

echo.
echo Build complete.
echo Output executables:
echo dist\whatsapp_downloader\whatsapp_downloader.exe
echo dist\whatsapp_downloader\whatsapp_downloader_config.exe
echo.
echo Notes:
echo - Google Chrome must be installed on the target machine.
echo - First run requires WhatsApp Web login (QR code).
echo - Run whatsapp_downloader_config.exe for a user-friendly settings UI.
echo.
pause
