@echo off
setlocal

cd /d "%~dp0.."
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0..\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Python environment not found. Follow the setup steps in README.md first.
    pause
    exit /b 1
)

"%PYTHON%" -m streamlit run "%~dp0app.py" --server.address 127.0.0.1
