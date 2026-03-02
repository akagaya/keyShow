@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo 仮想環境を作成しています...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    echo 依存パッケージをインストールしています...
    pip install -r requirements.txt
)

start "" .venv\Scripts\pythonw.exe main.py
