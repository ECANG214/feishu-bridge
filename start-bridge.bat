@echo off
cd /d D:\CC\feishu-bridge
echo Starting Feishu Bridge...
python bridge.py >> logs\bridge.log 2>&1
