-- stock_db 数据库初始化（最小DDL）
CREATE DATABASE IF NOT EXISTS stock_db DEFAULT CHARACTER SET utf8mb4;
USE stock_db;

-- 监控池
CREATE TABLE IF NOT EXISTS watch_pool (
  ts_code VARCHAR(32) PRIMARY KEY,
  name VARCHAR(64),
  industry VARCHAR(64),
  market VARCHAR(16),
  is_active TINYINT DEFAULT 1,
  sort_order INT DEFAULT 0,
  notes TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 日K线
CREATE TABLE IF NOT EXISTS daily_kline (
  ts_code VARCHAR(32) NOT NULL,
  trade_date DATE NOT NULL,
  open DECIMAL(12,4), high DECIMAL(12,4), low DECIMAL(12,4), close DECIMAL(12,4),
  pre_close DECIMAL(12,4), change_pct DECIMAL(10,4), vol DECIMAL(20,2), amount DECIMAL(20,2),
  PRIMARY KEY (ts_code, trade_date)
);

-- 复权K线
CREATE TABLE IF NOT EXISTS daily_kline_qfq LIKE daily_kline;

-- 策略信号
CREATE TABLE IF NOT EXISTS strategy_signal_daily (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  strategy_id INT DEFAULT 1, ts_code VARCHAR(32), trade_date DATE,
  action VARCHAR(32), holding_status VARCHAR(32),
  buy_score DECIMAL(8,2), cur_price DECIMAL(12,4),
  UNIQUE KEY uk_ssd (strategy_id, ts_code, trade_date)
);

-- 持仓
CREATE TABLE IF NOT EXISTS portfolio_holdings (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(32) DEFAULT 'tony', ts_code VARCHAR(32), name VARCHAR(64),
  trade_date DATE, qty INT, avail_qty INT DEFAULT 0,
  cost_price DECIMAL(12,4), current_price DECIMAL(12,4),
  market_value DECIMAL(16,4), profit_amount DECIMAL(16,4), profit_pct DECIMAL(10,4),
  status VARCHAR(16) DEFAULT 'HOLDING', source VARCHAR(16) DEFAULT 'MANUAL',
  advice VARCHAR(64), advice_reason TEXT,
  lock_active TINYINT DEFAULT 0, lock_until DATE,
  buy_date DATE, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 季节状态
CREATE TABLE IF NOT EXISTS season_state (
  trade_date DATE, index_code VARCHAR(32),
  season VARCHAR(32), raw_score DECIMAL(8,4),
  market_season VARCHAR(32), market_confidence DECIMAL(8,4),
  PRIMARY KEY (trade_date, index_code)
);

-- 系统配置
CREATE TABLE IF NOT EXISTS system_config (
  config_key VARCHAR(64) PRIMARY KEY,
  config_value TEXT, description VARCHAR(200)
);

INSERT IGNORE INTO system_config VALUES
  ('default_user_id', 'tony', '默认用户'),
  ('tushare_token', '', 'Tushare Token(部署时填写)');
