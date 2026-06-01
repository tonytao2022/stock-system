#!/bin/bash
# P6 分季评分双轨引擎 — 每日自动调度 v1.1
# ==============================
# 依赖: daily_pipeline (原数据管道)
# 时序: 数据管道→P6评分 (每日15:30后执行)

set -e

BASE_DIR="/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现"
cd "$BASE_DIR"

source venv/bin/activate 2>/dev/null || true

MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
export MYSQL_PASS="$MYSQL_PWD"

LOG_FILE="/tmp/p6_pipeline_$(date +%Y%m%d).log"
echo "============================================================" | tee -a $LOG_FILE
echo "📡 P6双轨评分调度启动: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE

# 1. 检查数据
echo "" | tee -a $LOG_FILE
echo "🔍 检查今日数据..." | tee -a $LOG_FILE

# 2. P6全量评分+入库
echo "" | tee -a $LOG_FILE
echo "🏃 P6双轨评分(监控池)..." | tee -a $LOG_FILE

cd "$BASE_DIR"
python3 -c "
import sys, os, time
sys.path.insert(0, '$BASE_DIR')
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', '')
from season_engine import SeasonEngine
from p6_dual_track_engine import daily_pipeline

start = time.time()
results = daily_pipeline(mode='watch_pool')
elapsed = time.time() - start
print(f'⏱️ 总用时: {elapsed:.0f}s')
" 2>&1 | tee -a $LOG_FILE

echo "" | tee -a $LOG_FILE
echo "✅ P6双轨评分管道完成: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
