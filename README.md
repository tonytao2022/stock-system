# 股票智能分析管理系统

基于量化策略的A股投资决策支持系统。集成Tushare数据源、缠论分析、季节评分模型、阶梯动态持有策略。

## 系统架构

```
stock-manager (前端 SPA) ← Nginx (80端口)
    ├── manager_server (8887) — 管理API + 持仓/策略/配置
    ├── signal_server (8888) — 趋势评分/季节判定
    └── signal_server (8889) — 策略信号/BUY/SELL/HOLD
        └── MySQL (stock_db) — 31张表
```

## 快速部署

```bash
cd scripts
./deploy.sh
```

## 手动部署步骤

### 1. 系统依赖
```bash
apt install python3 nginx mysql-server
pip3 install flask flask-cors pymysql pandas numpy tushare
```

### 2. 复制文件
```bash
mkdir -p /opt/stock-system/engine
cp backend/*.py /opt/stock-system/
cp -r backend/engine/* /opt/stock-system/engine/
mkdir -p /var/www/html/stock-manager
cp frontend/* /var/www/html/stock-manager/
```

### 3. 配置Nginx
```bash
cp config/nginx-default.conf /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

### 4. 注册服务
```bash
cp config/stock-manager-8887.service /etc/systemd/system/
cp config/stock-manager-8889.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable stock-manager-8887.service
systemctl enable stock-manager-8889.service
systemctl start stock-manager-8887.service
systemctl start stock-manager-8889.service
```

### 5. 初始化数据库
```bash
# 导入 stock_db DDL
mysql -u root -p < scripts/init_db.sql
```

## API文档
| 服务 | 端口 | 路由前缀 | 说明 |
|:----|:----:|:---------|:-----|
| manager_server | 8887 | `/api/v1/management/*` | 持仓/策略/配置/邮件 |
| trend_server | 8888 | `/api/v1/trend/*` | 季节判定/市场状态 |
| signal_server | 8889 | `/api/v1/signal/*` | 策略信号/评分 |

## 定时任务
| 时间 | 任务 | 说明 |
|:----|:-----|:-----|
| 工作日 15:30 | daily_pipeline.py | 收盘数据拉取+评分+信号 |
| 工作日 16:00 | step_strategy_engine.py | 阶梯策略评估 |

## 技术栈
- 后端: Python3 + Flask + MySQL
- 前端: 原生HTML + ECharts + Chart.js
- 数据: Tushare Pro
- 运行: systemd + Nginx

