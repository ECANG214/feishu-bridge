@echo off
rem 切换到本脚本所在目录（%~dp0），适配任意 clone 位置
cd /d "%~dp0"
if not exist logs mkdir logs
echo Starting Feishu Bridge...
python bridge.py >> logs\bridge.log 2>&1
