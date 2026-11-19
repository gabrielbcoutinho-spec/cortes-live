@echo off
title Cortes ao vivo
cd /d "%~dp0"
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
start "" "J:\cortes-live\prontos\revisar.html"
py -W ignore cortes.py %*
pause
