@echo off
cd /d "%~dp0"
python main.py >> logs\run.log 2>&1
