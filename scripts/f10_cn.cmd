@echo off
cd /d "%~dp0.."
set PYTHONUNBUFFERED=1
desktop\.venv\Scripts\python.exe -m market_monitor f10 --data-root data_control --market CN --limit-details 6000 --detail-delay-seconds 1.0 --skip-quotes >> data_control\f10\logs\sched_cn.log 2>&1
echo exit=%errorlevel% %date% %time% >> data_control\f10\logs\sched_cn.log
