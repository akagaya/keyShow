@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo 仮想環境を作成しています...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    echo 依存パッケージをインストールしています...
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

pip install pyinstaller >nul 2>&1

echo ビルドしています...
pyinstaller --noconsole --onefile --name KeyShow --icon KeyShow.ico main.py

if exist "dist\KeyShow.exe" (
    echo.
    echo ビルド成功: dist\KeyShow.exe
) else (
    echo.
    echo ビルド失敗
)
pause
