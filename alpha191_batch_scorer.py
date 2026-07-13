#!/usr/bin/env python3
"""
Alpha191 因子批量评分器（情景化因子矩阵第一步）
2026-07-13 by Main, 基于MAY方案B

功能：
1. 读取 MySQL daily_kline 数据
2. 计算MAY推荐的Top候选因子的每日评分
3. 输出到 alpha_factor_score 表（新建）
4. 支持增量更新（只算缺失日期）

候选因子选择（基于MAY方案B的五情景矩阵）：
  S1 牛市：α052 + α122 + α093
  S2 震荡暖：α062 + α001 + α052
  S3 震荡：α169 + α013 + α052(+α031)
  S4 震荡弱：α031 + α162 + α168
  S5 弱市：α062（风险模式，不做Alpha增强）

全局备选：α062(已有) α052 α031 α169 α013 α122 α093 α001 α162 α168
"""

import pymysql
import numpy as np
from scipy import stats
import json
import sys
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# ==================== 配置 ====================
MYSQL_USER = 'debian-sys-maint'
MYSQL_PASS = None  # 从/etc/mysql/debian.cnf读取
MYSQL_HOST = 'localhost'
MYSQL_DB = 'stock_db_v2'

TABLE_NAME = 'alpha_factor_score'  # 目标表名

# ==================== 数据连接 ====================

def get_mysql_pass():
    """从debian.cnf读取密码"""
    global MYSQL_PASS
    if MYSQL_PASS:
        return MYSQL_PASS
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                line = line.strip()
                if line.startswith('password'):
                    MYSQL_PASS = line.split('=')[1].strip()
                    return MYSQL_PASS
    except:
        pass
    # 尝试从环境变量读取
    MYSQL_PASS = os.environ.get('MYSQL_PASS', 'iXve1rVBXfdA4tL9')
    return MYSQL_PASS


def get_db():
    return pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=get_mysql_pass(),
        database=MYSQL_DB,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60
    )


# ==================== Alpha因子定义 ====================
# 每个因子定义为一个函数，输入一段日K线数据，输出评分 [0,100]

ALPHA_FACTORS = {
    'alpha052': {
        'name': 'α052 量价同步-(Low/Open)mean5',
        'desc': 'mean(low,5)与mean(open,5)的差值经标准差归一化→量价同步低时(低开)打分高',
        'requires': ['open', 'low'],
        'min_days': 5,
        'fn': None,  # 动态定义
    },
    'alpha122': {
        'name': 'α122 缩量波动',
        'desc': '(mean(volume,5)-volume)/std(volume,5)→缩量且波动适中为正',
        'requires': ['volume'],
        'min_days': 5,
        'fn': None,
    },
    'alpha093': {
        'name': 'α093 中价偏离',
        'desc': 'std(low,20)/(close+(high-low)/2)→波动紧凑时高分',
        'requires': ['close', 'high', 'low'],
        'min_days': 20,
        'fn': None,
    },
    'alpha001': {
        'name': 'α001 短期反转',
        'desc': 'rank(delay(close-delay(close,5),1)) - rank(close-delay(close,5))->滞后后的反转信号',
        'requires': ['close'],
        'min_days': 7,
        'fn': None,
    },
    'alpha169': {
        'name': 'α169 价量一致性',
        'desc': 'corr(volume,close,5)+close/open→量价一致性+低开',
        'requires': ['close', 'open', 'volume'],
        'min_days': 6,
        'fn': None,
    },
    'alpha013': {
        'name': 'α013 短期波动冲击',
        'desc': 'rank(cov(return,volume,5))→收益与量的协方差→冲击成本',
        'requires': ['close', 'volume'],
        'min_days': 6,
        'fn': None,
    },
    'alpha031': {
        'name': 'α031 振幅乖离',
        'desc': '(close-mean(close,12))/mean(close,12)*100-(close-open)/open*sign(delay(close,1))->价格乖离修正',
        'requires': ['close', 'open'],
        'min_days': 13,
        'fn': None,
    },
    'alpha162': {
        'name': 'α162 量价共振',
        'desc': 'corr(volume,close,20)*corr(high,volume,10)→量价双重相关性',
        'requires': ['close', 'high', 'volume'],
        'min_days': 21,
        'fn': None,
    },
    'alpha168': {
        'name': 'α168 总波动率',
        'desc': '(high+low-2*close)/close*100+rank(delay(volume,1))→日内波动+量',
        'requires': ['close', 'high', 'low', 'volume'],
        'min_days': 3,
        'fn': None,
    },
    'alpha089': {
        'name': 'α089 逆转动量',
        'desc': '(-1)*corr(close,volume,13)-rank(ts_min(low,5))→负量价相关+新低',
        'requires': ['close', 'low', 'volume'],
        'min_days': 14,
        'fn': None,
    },
}


def calc_alpha052(close, open_p, high, low, volume, start_idx, end_idx):
    """α052: mean(low,5)与mean(open,5)的差值归一化"""
    rows = end_idx - start_idx
    n = min(rows, 5)
    if n < 5:
        return 50
    mean_low = np.mean(low[end_idx-n:end_idx])
    mean_open = np.mean(open_p[end_idx-n:end_idx])
    std_low = np.std(low[end_idx-n:end_idx]) + 1e-10
    val = (mean_low - mean_open) / std_low
    # val∈[-3,3]映射到[0,100] 负值=低开=打分高
    score = max(0, min(100, 50 + (-val) * 10))
    return round(score, 1)


def calc_alpha122(close, open_p, high, low, volume, start_idx, end_idx):
    """α122: (mean(volume,5)-volume)/std(volume,5)"""
    rows = end_idx - start_idx
    n = min(rows, 5)
    if n < 5:
        return 50
    mean_vol = np.mean(volume[end_idx-n:end_idx-1])  # 前4日均值
    curr_vol = volume[end_idx-1] if end_idx-1 >= start_idx else volume[end_idx-1]
    std_vol = np.std(volume[end_idx-n:end_idx-1]) + 1e-10
    val = (mean_vol - curr_vol) / std_vol
    score = max(0, min(100, 50 + val * 8))
    return round(score, 1)


def calc_alpha093(close, open_p, high, low, volume, start_idx, end_idx):
    """α093: std(low,20)/(close+(high-low)/2) 波动紧凑时高分"""
    n = min(end_idx - start_idx, 20)
    if n < 20:
        return 50
    mid_price = close[end_idx-1] + (high[end_idx-1] - low[end_idx-1]) / 2
    std_low = np.std(low[end_idx-n:end_idx])
    val = std_low / (mid_price + 1e-10)
    # val通常在[0.005,0.05]，越小（波动紧凑）分越高
    score = max(0, min(100, 50 + (0.02 - val) * 1500))
    return round(score, 1)


def calc_alpha001(close, open_p, high, low, volume, start_idx, end_idx):
    """α001: rank(delay(close-delay(close,5),1)) - rank(close-delay(close,5))
    简化为：近6日收益的变动→反转信号"""
    n = min(end_idx - start_idx, 7)
    if n < 7:
        return 50
    rets = []
    for i in range(end_idx-7+1, end_idx):
        rets.append((close[i] - close[i-1]) / close[i-1])
    # 取最近两段收益差
    ret_prev = sum(rets[-3:-1])  # 前2~3天
    ret_last = sum(rets[-2:])    # 最近2天
    diff = ret_prev - ret_last  # 前强后弱=买入反转信号
    score = max(0, min(100, 50 + diff * 200))
    return round(score, 1)


def calc_alpha169(close, open_p, high, low, volume, start_idx, end_idx):
    """α169: corr(volume,close,5)+close/open 量价一致性+低开"""
    n = min(end_idx - start_idx, 6)
    if n < 6:
        return 50
    vol_5 = list(volume[end_idx-5:end_idx])
    close_5 = list(close[end_idx-5:end_idx])
    if np.std(vol_5) > 0 and np.std(close_5) > 0:
        corr = np.corrcoef(vol_5, close_5)[0, 1]
    else:
        corr = 0
    co_ratio = close[end_idx-1] / open_p[end_idx-1] if open_p[end_idx-1] > 0 else 1
    val = corr + (1 / co_ratio)  # 低开=1/co_ratio大
    score = max(0, min(100, 50 + (val - 1.0) * 30))
    return round(score, 1)


def calc_alpha013(close, open_p, high, low, volume, start_idx, end_idx):
    """α013: rank(cov(return,volume,5)) 收益与量的协方差"""
    n = min(end_idx - start_idx, 6)
    if n < 6:
        return 50
    rets = []
    for i in range(end_idx-5, end_idx):
        rets.append((close[i] - close[i-1]) / close[i-1])
    vol_5 = list(volume[end_idx-5:end_idx])
    if len(rets) >= 5 and len(vol_5) >= 5:
        # 协方差值高度依赖量纲, 改用相关系数
        if np.std(rets) > 0 and np.std(vol_5) > 0:
            corr = np.corrcoef(rets, vol_5)[0, 1]
        else:
            corr = 0
    else:
        corr = 0
    # corr∈[-1,1], corr>0收益与量正相关→动量持续(好); corr<0→反向(差)
    score = max(0, min(100, 50 + corr * 25))
    return round(score, 1)


def calc_alpha031(close, open_p, high, low, volume, start_idx, end_idx):
    """α031: (close-mean(close,12))/mean(close,12)*100 - (close-open)/open
    价格乖离修正日内涨跌"""
    n = min(end_idx - start_idx, 13)
    if n < 13:
        return 50
    mean_c12 = np.mean(close[end_idx-12:end_idx])
    price_dev = (close[end_idx-1] - mean_c12) / mean_c12 * 100
    intraday = (close[end_idx-1] - open_p[end_idx-1]) / open_p[end_idx-1] * 100
    val = price_dev - intraday
    # 修正后偏高=买入信号(适度上涨但日内不冲高=健康)
    score = max(0, min(100, 50 + val * 5))
    return round(score, 1)


def calc_alpha162(close, open_p, high, low, volume, start_idx, end_idx):
    """α162: corr(volume,close,20)*corr(high,volume,10) 量价双重共振"""
    n = min(end_idx - start_idx, 21)
    if n < 21:
        return 50
    # corr(volume,close,20)
    vol_20 = list(volume[end_idx-20:end_idx])
    close_20 = list(close[end_idx-20:end_idx])
    # corr(high,volume,10)
    high_10 = list(high[end_idx-10:end_idx])
    vol_10 = list(volume[end_idx-10:end_idx])
    corr1 = np.corrcoef(vol_20, close_20)[0, 1] if np.std(vol_20) > 0 and np.std(close_20) > 0 else 0
    corr2 = np.corrcoef(high_10, vol_10)[0, 1] if np.std(high_10) > 0 and np.std(vol_10) > 0 else 0
    val = corr1 * corr2
    # 双正=强共振→高分
    score = max(0, min(100, 50 + val * 50))
    return round(score, 1)


def calc_alpha168(close, open_p, high, low, volume, start_idx, end_idx):
    """α168: (high+low-2*close)/close*100 + rank(delay(volume,1))
    日内波动+量"""
    n = min(end_idx - start_idx, 3)
    if n < 3:
        return 50
    vol_ratio = volume[end_idx-2] / (np.mean(volume[max(start_idx, end_idx-10):end_idx-1]) + 1)
    daily_range = (high[end_idx-1] + low[end_idx-1] - 2*close[end_idx-1]) / close[end_idx-1] * 100
    # 日内波动适中最优(太大=不安定，太小=不活跃)
    range_opt = 2 - abs(daily_range - 2)  # 最优日内波动~2%
    score = max(0, min(100, 50 + range_opt * 8 + vol_ratio * 3))
    return round(score, 1)


def calc_alpha089(close, open_p, high, low, volume, start_idx, end_idx):
    """α089: (-1)*corr(close,volume,13) - rank(ts_min(low,5))
    负量价相关+新低"""
    n = min(end_idx - start_idx, 14)
    if n < 14:
        return 50
    close_13 = list(close[end_idx-13:end_idx])
    vol_13 = list(volume[end_idx-13:end_idx])
    low_5 = np.min(low[end_idx-5:end_idx]) if (end_idx - start_idx) >= 5 else low[end_idx-1]
    mean_low_10 = np.mean(low[max(start_idx, end_idx-10):end_idx])
    if np.std(close_13) > 0 and np.std(vol_13) > 0:
        corr = np.corrcoef(close_13, vol_13)[0, 1]
    else:
        corr = 0
    val = -corr  # 负相关=高分
    # low低于近期均值=加分（超跌）
    low_dev = max(0, (mean_low_10 - low_5) / close[end_idx-1])
    score = max(0, min(100, 50 + val * 30 + low_dev * 200))
    return round(score, 1)


# 绑定函数引用
ALPHA_FACTORS['alpha052']['fn'] = calc_alpha052
ALPHA_FACTORS['alpha122']['fn'] = calc_alpha122
ALPHA_FACTORS['alpha093']['fn'] = calc_alpha093
ALPHA_FACTORS['alpha001']['fn'] = calc_alpha001
ALPHA_FACTORS['alpha169']['fn'] = calc_alpha169
ALPHA_FACTORS['alpha013']['fn'] = calc_alpha013
ALPHA_FACTORS['alpha031']['fn'] = calc_alpha031
ALPHA_FACTORS['alpha162']['fn'] = calc_alpha162
ALPHA_FACTORS['alpha168']['fn'] = calc_alpha168
ALPHA_FACTORS['alpha089']['fn'] = calc_alpha089

# 最终候选因子列表（去重）
CANDIDATE_FACTORS = ['alpha052', 'alpha122', 'alpha093', 'alpha001',
                     'alpha169', 'alpha013', 'alpha031', 'alpha162',
                     'alpha168', 'alpha089']


# ==================== 建表 ====================

def ensure_table():
    """确保 alpha_factor_score 表存在"""
    db = get_db()
    try:
        with db.cursor() as c:
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
                    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
                    `ts_code` VARCHAR(20) NOT NULL COMMENT '股票代码',
                    `trade_date` DATE NOT NULL COMMENT '交易日',
                    `factor_name` VARCHAR(30) NOT NULL COMMENT '因子名称',
                    `factor_score` DECIMAL(6,1) NOT NULL DEFAULT 50.0 COMMENT '因子评分 0-100',
                    `calc_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '计算时间',
                    UNIQUE KEY `uk_factor_date` (`ts_code`, `trade_date`, `factor_name`),
                    KEY `idx_date` (`trade_date`),
                    KEY `idx_factor` (`factor_name`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 
                  COMMENT='Alpha191候选因子日度评分（情景化因子矩阵基础数据）'
            """)
            db.commit()
    finally:
        db.close()


# ==================== 核心计算 ====================

def get_stock_list(db):
    """获取需要评分的股票列表（daily_kline表）"""
    with db.cursor() as c:
        c.execute("""
            SELECT DISTINCT k.ts_code, w.name 
            FROM daily_kline k
            JOIN watch_pool w ON k.ts_code = w.ts_code
            WHERE k.ts_code IS NOT NULL AND k.ts_code != ''
            ORDER BY k.ts_code
        """)
        stocks = c.fetchall()
        if not stocks:
            # 如果watch_pool为空，取daily_kline中所有有数据的
            c.execute("""
                SELECT DISTINCT ts_code, '' as name FROM daily_kline 
                WHERE ts_code IS NOT NULL AND ts_code != ''
                ORDER BY ts_code
            """)
            stocks = c.fetchall()
    return stocks


def get_date_range(db):
    """获取需要计算的数据日期范围"""
    with db.cursor() as c:
        # 最新交易日
        c.execute("SELECT MAX(trade_date) as max_date FROM daily_kline")
        r = c.fetchone()
        max_date = r['max_date'] if r['max_date'] else datetime.now().date()
        
        # 已经有评分的最新日期
        c.execute(f"""
            SELECT MIN(trade_date) as start, MAX(trade_date) as end 
            FROM `{TABLE_NAME}`
        """)
        r2 = c.fetchone()
    
    return max_date, r2['start'] if r2 else None, r2['end'] if r2 else None


def load_kline(db, ts_code, start_date, end_date):
    """加载单个股票的日K线"""
    with db.cursor() as c:
        c.execute("""
            SELECT trade_date, open, high, low, close, vol, amount 
            FROM daily_kline 
            WHERE ts_code = %s AND trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date
        """, (ts_code, start_date, end_date))
        rows = c.fetchall()
    return rows


def process_stock(db, ts_code, start_date, end_date, force_all=False):
    """处理单只股票的所有候选因子"""
    klines = load_kline(db, ts_code, start_date, end_date)
    if not klines or len(klines) < 25:  # 最少需要25天数据
        return 0
    
    # 提取numpy数组
    dates = [r['trade_date'] for r in klines]
    close = np.array([float(r['close']) for r in klines], dtype=float)
    open_p = np.array([float(r['open']) for r in klines], dtype=float)
    high = np.array([float(r['high']) for r in klines], dtype=float)
    low = np.array([float(r['low']) for r in klines], dtype=float)
    volume = np.array([float(r['vol']) for r in klines], dtype=float)
    
    n = len(klines)
    count = 0
    
    for factor_name in CANDIDATE_FACTORS:
        factor = ALPHA_FACTORS[factor_name]
        fn = factor['fn']
        min_days = factor['min_days']
        
        # 遍历每个交易日
        values = []
        trade_dates = []
        
        for i in range(min(min_days + 1, n - 1), n):
            d = dates[i]
            try:
                score = fn(close, open_p, high, low, volume, max(0, i-30), i+1)
                values.append(score)
                trade_dates.append(d)
            except Exception as e:
                pass
        
        if values:
            # 批量写入DB（批量VALUES，大幅减少SQL次数）
            BATCH_SIZE = 100
            with db.cursor() as c:
                for batch_start in range(0, len(values), BATCH_SIZE):
                    batch_end = min(batch_start + BATCH_SIZE, len(values))
                    batch_dates = trade_dates[batch_start:batch_end]
                    batch_scores = values[batch_start:batch_end]
                    
                    # 构建批量SQL
                    value_sqls = []
                    params = []
                    for j in range(len(batch_dates)):
                        value_sqls.append('(%s, %s, %s, %s)')
                        params.extend([ts_code, batch_dates[j], factor_name, batch_scores[j]])
                    
                    try:
                        c.execute(f"""
                            INSERT INTO `{TABLE_NAME}` 
                            (ts_code, trade_date, factor_name, factor_score)
                            VALUES {','.join(value_sqls)}
                            ON DUPLICATE KEY UPDATE factor_score = VALUES(factor_score),
                                                     calc_time = CURRENT_TIMESTAMP
                        """, params)
                    except Exception as e:
                        print(f'    [WARN] 批量写入失败({ts_code}/{factor_name}/{batch_start}): {e}')
                        # 逐条fallback
                        for j in range(len(batch_dates)):
                            try:
                                c.execute(f"""
                                    INSERT INTO `{TABLE_NAME}` 
                                    (ts_code, trade_date, factor_name, factor_score)
                                    VALUES (%s, %s, %s, %s)
                                    ON DUPLICATE KEY UPDATE factor_score = VALUES(factor_score),
                                                             calc_time = CURRENT_TIMESTAMP
                                """, (ts_code, batch_dates[j], factor_name, batch_scores[j]))
                            except Exception:
                                pass
            db.commit()
            count += len(values)
    
    return count


# ==================== 主流程 ====================

def main():
    print(f"=== Alpha191 候选因子批量评分器 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 建表
    ensure_table()
    print(f"✅ 表 {TABLE_NAME} 已就绪")
    
    # 连接
    db = get_db()
    try:
        stocks = get_stock_list(db)
        print(f"📊 待评分股票: {len(stocks)} 只")
        
        max_date, _, _ = get_date_range(db)
        # 计算时间窗口：从2025-01-01开始（2年数据足够因子计算需要的历史）
        start_date = datetime(2025, 1, 1).date()
        end_date = max_date
        
        print(f"📅 时间范围: {start_date} ~ {end_date}")
        
        total_records = 0
        failed = 0
        
        for i, stock in enumerate(stocks):
            ts_code = stock['ts_code']
            name = stock.get('name', '')
            
            try:
                count = process_stock(db, ts_code, start_date, end_date)
                total_records += count
                if (i + 1) % 20 == 0:
                    print(f"  进度 {i+1}/{len(stocks)} | 累计 {total_records} 条")
            except Exception as e:
                print(f"  ❌ {ts_code} {name}: {e}")
                failed += 1
        
        print(f"\n{'='*50}")
        print(f"✅ 完成！共处理 {len(stocks)} 只股票")
        print(f"📊 写入 {total_records} 条因子评分数据")
        print(f"❌ 失败: {failed} 只")
        print(f"⏱ 因子分布:")
        
        with db.cursor() as c:
            c.execute(f"""
                SELECT factor_name, COUNT(*) as cnt, ROUND(AVG(factor_score),1) as avg,
                       ROUND(MIN(factor_score),1) as min_s, ROUND(MAX(factor_score),1) as max_s
                FROM `{TABLE_NAME}` 
                GROUP BY factor_name ORDER BY factor_name
            """)
            for r in c.fetchall():
                print(f"  {r['factor_name']:12s}: {r['cnt']:>8d}条  avg={r['avg']:5.1f}  [{r['min_s']:5.1f}~{r['max_s']:5.1f}]")
        
    finally:
        db.close()


if __name__ == '__main__':
    main()
