@echo off
chcp 65001 >nul
echo === Vipon Publisher (Local Run) ===
echo Posts ONE reel from the sheet per run.
echo Run this again each hour to post the next one.
echo.

set SECRETS_DIR=C:\Users\ehaba
set PYTHONUTF8=1
cd /d C:\Users\ehaba\vipon-affiliate-bot

python vipon_publisher.py

echo.
echo === Done ===
pause
