@echo off
rem Запуск AquaLocal из папки, где лежит этот .bat — путь не захардкожен.
cd /d "%~dp0"
start "" /B ".venv\Scripts\pythonw.exe" "app.py"
