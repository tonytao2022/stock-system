#!/bin/bash
# 股票智能分析系统 一键部署脚本
set -e

echo "===== 股票智能分析系统 部署脚本 ====="
echo ""

# 检查依赖
echo "检查系统依赖..."
which python3 || { echo "❌ 需要 Python3"; exit 1; }
which nginx || { echo "❌ 需要 Nginx"; exit 1; }
which mysql || { echo "❌ 需要 MySQL"; exit 1; }

# 安装Python依赖
echo ""
echo "安装Python依赖..."
pip3 install flask flask-cors pymysql pandas numpy tushare 2>&1 | grep -i "error\|success\|already" || true

# 复制后端
echo ""
echo "部署后端..."
mkdir -p /opt/stock-system/
cp -r ../backend/* /opt/stock-system/
mkdir -p /opt/stock-system/engine
cp -r ../backend/engine /opt/stock-system/

# 复制前端
echo ""
echo "部署前端..."
mkdir -p /var/www/html/stock-manager
cp -r ../frontend/* /var/www/html/stock-manager/

# 复制systemd配置
echo ""
echo "注册systemd服务..."
cp ../config/stock-manager-8887.service /etc/systemd/system/
cp ../config/stock-manager-8888.service /etc/systemd/system/ 2>/dev/null || true
cp ../config/stock-manager-8889.service /etc/systemd/system/
systemctl daemon-reload

# 复制Nginx配置
echo ""
echo "部署Nginx配置..."
cp ../config/nginx-default.conf /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# 启动服务
echo ""
echo "启动服务..."
systemctl enable stock-manager-8887.service
systemctl enable stock-manager-8888.service 2>/dev/null || true
systemctl enable stock-manager-8889.service
systemctl restart stock-manager-8887.service
systemctl restart stock-manager-8888.service 2>/dev/null || true
systemctl restart stock-manager-8889.service

echo ""
echo "✅ 部署完成!"
echo "前端访问: http://localhost/stock-manager/"
echo "后端API: http://localhost:8887/health"

# 注册P6双轨评分定时器
echo ""
echo "注册P6双轨评分服务..."
cp ../config/p6_pipeline.service /etc/systemd/system/
cp ../config/p6_pipeline.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable p6_pipeline.timer
systemctl start p6_pipeline.timer

echo ""
echo "P6服务: $(systemctl is-active p6_pipeline.timer)"
echo ""
echo "✅ 部署完成!"
echo "前端访问: http://localhost/stock-manager/"
echo "后端API: http://localhost:8887/health"
echo "P6定时器: 工作日15:35自动执行"
