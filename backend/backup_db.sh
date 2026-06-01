#!/bin/bash
# 数据库备份脚本 v2 — mysqldump+gzip → git push
set -e

BACKUP_DIR="/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现/db_backup"
MAIN_REPO="/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现"
DUMP_FILE_BASE="${BACKUP_DIR}/stock_db_$(date +%Y%m%d)"
DUMP_GZ="${DUMP_FILE_BASE}.sql.gz"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "========================================"
echo "📦 数据库备份开始: ${TIMESTAMP}"
echo "========================================"

mkdir -p "${BACKUP_DIR}"

# 读MySQL密码
MYSQL_PASS=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
MYSQL_USER="debian-sys-maint"

# 删除旧备份（保留7天）
find "${BACKUP_DIR}" -name "stock_db_*.sql.gz" -mtime +7 -delete 2>/dev/null || true

echo "📤 导出 stock_db 数据库 (gzip压缩)..."
mysqldump -u"${MYSQL_USER}" -p"${MYSQL_PASS}" \
  --single-transaction \
  --routines \
  --triggers \
  --set-gtid-purged=OFF \
  --complete-insert \
  --skip-lock-tables \
  stock_db | gzip > "${DUMP_GZ}"

DUMP_SIZE=$(stat --format=%s "${DUMP_GZ}" 2>/dev/null || stat -f%z "${DUMP_GZ}" 2>/dev/null)
DUMP_SIZE_HUMAN=$(numfmt --to=iec ${DUMP_SIZE} 2>/dev/null || echo "${DUMP_SIZE} bytes")

# 查表数（兼容debian-sys-maint）
TABLE_COUNT=$(mysql -u"${MYSQL_USER}" -p"${MYSQL_PASS}" -N -e \
  "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='stock_db' AND TABLE_TYPE='BASE TABLE'" 2>/dev/null)

echo "✅ 导出完成: ${DUMP_SIZE_HUMAN} (${TABLE_COUNT}张表)"

# 删除未压缩版本
rm -f "${DUMP_FILE_BASE}.sql"

# ── Git 提交 ──
cd "${MAIN_REPO}"

git add "db_backup/stock_db_$(date +%Y%m%d).sql.gz"

if git diff --cached --quiet -- db_backup/ 2>/dev/null; then
  echo "ℹ️ 数据库无变更，跳过提交"
else
  echo "📤 提交到 stock-analyzer 仓库..."
  git commit -m "chore: 数据库备份 ${TIMESTAMP}

数据库: stock_db (${TABLE_COUNT}张表)
文件: stock_db_$(date +%Y%m%d).sql.gz
大小: ${DUMP_SIZE_HUMAN}
备份方式: mysqldump | gzip"

  echo "📤 推送到 GitHub..."
  git push origin master 2>&1 | tail -5
fi

echo ""
echo "✅ 备份完成!"
echo "   文件: stock_db_$(date +%Y%m%d).sql.gz"
echo "   大小: ${DUMP_SIZE_HUMAN}"
echo "   时间: $(date '+%Y-%m-%d %H:%M:%S')"
