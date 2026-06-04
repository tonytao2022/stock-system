#!/bin/bash
# fetch_factors_cron.sh — 每日凌晨4:00自动拉取 daily_basic + moneyflow
# 配置在 cron 中运行，避开 Tushare 晚高峰

cd /opt/stock-analyzer
LOG="/var/log/fetch_factors_$(date +%Y%m%d).log"

echo "[$(date)] 开始拉取 daily_basic + moneyflow" >> "$LOG" 2>&1

# 批量拉取：每次50只，间隔1秒，超时120秒
python3 /opt/stock-analyzer/fetch_factors.py >> "$LOG" 2>&1

echo "[$(date)] 完成" >> "$LOG" 2>&1
