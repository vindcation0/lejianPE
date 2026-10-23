@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title LeJian Grabber
python menu.py
if not "%errorlevel%"=="0" pause
