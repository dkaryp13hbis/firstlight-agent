@echo off
title Hotel Morning Briefing Server
echo Starting Hotel Morning Briefing...
echo Open http://localhost:8765 in your browser
echo Press Ctrl+C to stop
"%USERPROFILE%\anaconda3\python.exe" "%~dp0server.py"
pause
