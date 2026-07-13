#!/usr/bin/env python3
"""
bt_v13_matrix.py — 情景化因子矩阵回测框架（V2: 截面Rank加分方案）
2026-07-13 v2 by Main

评分方案（方案E）：
  final_score = M1_composite_score + alpha_bonus
  
  alpha_bonus = Σ(rank(alpha_factor_i) × w_i) × SCALE - OFFSET
  
  其中 rank() 是当日所有股票的截面百分位排名 [0,1]
  SCALE=30, OFFSET=10 使 rank=0.5 时 bonus=0
  上限+15，下限-8

情景配置（基于MAY方案B+方案E截面Rank）：
  S1 牛市:      + α052r×0.35 + α122r×0.25 + α093r×0.20
  S2 震荡暖:    + α062r×0.35 + α001r×0.25 + α052r×0.20
  S3 震荡(主):  + α169r×0.35 + α013r×0.30 + α052r×0.35
  S4 震荡弱:    + α031r×0.35 + α162r×0.30 + α168r×0.25
  S5 弱市:      + α062r×0.40（保守，只加一个最强的）
  
  注：r后缀表示截面Rank百分位 [0,1]
"""

import pymysql
import numpy as np
from collections import defaultdict
from datetime import datetime, date
import sys
import json
import warnings
warnings.filterwarnings('ignore')

# ==================== 配置 ====================
MYSQL_PASS = None

SCENARIO_CONFIG = {
    'summer': {
        'name': 'S1 牛市',
        'buy_threshold': 68,
        'stop_loss': 0.08,
        'position_base': 0.50,
        'per_stock': 0.30,
        'cooldown': 0,
        'alpha_ranks': {'alpha052': 0.35, 'alpha122': 0.25, 'alpha093': 0.20},
        'bonus_scale': 30,
        'bonus_offset': 10,
        'bonus_max': 15,
        'bonus_min': -8,
    },
    'chaos_spring': {
        'name': 'S2 震荡暖',
        'buy_threshold': 70,
        'stop_loss': 0.08,
        'position_base': 0.40,
        'per_stock': 0.25,
        'cooldown': 0,
        'alpha_ranks': {'alpha062': 0.35, 'alpha001': 0.25, 'alpha052': 0.20},
        'bonus_scale': 30,
        'bonus_offset': 10,
        'bonus_max': 15,
        'bonus_min': -8,
    },
    'spring': {
        'name': 'S2 震荡暖(spring)',
        'buy_threshold': 70,
        'stop_loss': 0.07,
        'position_base': 0.40,
        'per_stock': 0.25,
        'cooldown': 0,
        'alpha_ranks': {'alpha062': 0.30, 'alpha001': 0.30, 'alpha052': 0.20},
        'bonus_scale': 30,
        'bonus_offset': 10,
        'bonus_max': 15,
        'bonus_min': -8,
    },
    'chaos': {
        'name': 'S3 震荡',
        'buy_threshold': 72,
        'stop_loss': 0.08,
        'position_base': 0.35,
        'per_stock': 0.20,
        'cooldown': 3,
        'alpha_ranks': {'alpha169': 0.35, 'alpha013': 0.30, 'alpha052': 0.35},
        'bonus_scale': 30,
        'bonus_offset': 10,
        'bonus_max': 15,
        'bonus_min': -8,
    },
    'chaos_autumn': {
        'name': 'S4 震荡弱',
        'buy_threshold': 72,
        'stop_loss': 0.06,
        'position_base': 0.20,
        'per_stock': 0.12,
        'cooldown': 5,
        'alpha_ranks': {'alpha031': 0.35, 'alpha162': 0.30, 'alpha168': 0.25},
        'bonus_scale': 30,
        'bonus_offset': 10,
        'bonus_max': 12,
        'bonus_min': -8,
    },
    'weak_autumn': {
        'name': 'S5 弱市(weak_autumn)',
        'buy_threshold': 75,
        'stop_loss': 0.05,
        'position_base': 0.15,
        'per_stock': 0.08,
        'cooldown': 7,
        'alpha_ranks': {'alpha062': 0.40},
        'bonus_scale': 25,
        'bonus_offset': 10,
        'bonus_max': 10,
        'bonus_min': -6,
    },
    'autumn': {
        'name': 'S5 弱市(autumn)',
        'buy_threshold': 78,
        'stop_loss': 0.05,
        'position_base': 0.10,
        'per_stock': 0.05,
        'cooldown': 10,
        'alpha_ranks': {'alpha062': 0.40},
        'bonus_scale': 20,
        'bonus_offset': 8,
        'bonus_max': 8,
        'bonus_min': -5,
    },
    'winter': {
        'name': 'S5 弱市(winter)',
        'buy_threshold': 85,
        'stop_loss': 0.03,
        'position_base': 0.05,
        'per_stock': 0.03,
        'cooldown': 15,
        'alpha_ranks': {},
        'bonus_scale': 0,
        'bonus_offset': 0,
        'bonus_max': 0,
        'bonus_min': 0,
    },
}


def get_mysql_pass():
    global MYSQL_PASS
    if MYSQL_PASS:
        return MYSQL_PASS
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if line.strip().startswith('password'):
                    MYSQL_PASS = line.split('=')[1].strip()
                    return MYSQL_PASS
    except:
        pass
    MYSQL_PASS = os.environ.get('MYSQL_PASS', 'iXve1rVBXfdA4tL9')
    return MYSQL_PASS


def get_db():
    return pymysql.connect(
        host='localhost',
        user='debian-sys-maint',
        password=get_mysql_pass(),
        database='stock_db_v2',
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=120,
    )


def get_season_cache(db):
    """加载季节判定缓存"""
    from collections import defaultdict as dd
    cache = {}
    with db.cursor() as c:
        c.execute("SELECT trade_date, season, confidence, regime FROM season_state WHERE trade_date >= '2023-01-01' ORDER BY trade_date")
        for r in c.fetchall():
            cache[str(r['trade_date'])] = {
                'season': r['season'],
                'confidence': float(r.get('confidence') or 0.5),
                'regime': r.get('regime', ''),
            }
    return cache


def get_scenario(trade_date, season_cache):
    day = str(trade_date)
    if day not in season_cache:
        return None, SCENARIO_CONFIG['chaos']
    season = season_cache[day]['season']
    mapping = {
        'summer': 'summer', 'spring': 'spring', 'weak_spring': 'spring',
        'chaos_spring': 'chaos_spring', 'chaos': 'chaos',
        'chaos_autumn': 'chaos_autumn', 'weak_autumn': 'weak_autumn',
        'autumn': 'autumn', 'winter': 'winter',
    }
    mapped = mapping.get(season, 'chaos')
    return season, SCENARIO_CONFIG[mapped]


def compute_cross_section_rank(score_dict):
    """计算截面百分位排名 [0,1]"""
    if not score_dict:
        return {}
    keys = list(score_dict.keys())
    vals = np.array([score_dict[k] for k in keys], dtype=float)
    order = np.argsort(vals)
    ranks = np.empty(len(keys))
    ranks[order] = np.arange(len(keys)) / max(len(keys) - 1, 1)
    return {keys[i]: float(ranks[i]) for i in range(len(keys))}


def load_m1_scores(db, trade_date, stock_codes):
    """加载M1综合分"""
    if not stock_codes:
        return {}
    results = {}
    for i in range(0, len(stock_codes), 200):
        chunk = stock_codes[i:i+200]
        ids = ','.join(["'%s'" % c for c in chunk])
        with db.cursor() as c:
            c.execute("SELECT ts_code, composite_score FROM strategy_signal WHERE trade_date='%s' AND ts_code IN (%s)" % (trade_date, ids))
            for r in c.fetchall():
                results[r['ts_code']] = float(r['composite_score'] or 50)
    return results


def load_alpha_scores_bulk(db, trade_date, stock_codes, factor_names):
    """批量加载alpha因子评分。alpha062从strategy_signal表读取，其余从alpha_factor_score表读取"""
    if not stock_codes or not factor_names:
        return {}
    result_map = {fn: {} for fn in factor_names}
    
    # 分离alpha062（在strategy_signal表）和其他因子（在alpha_factor_score表）
    afs_factors = [fn for fn in factor_names if fn != 'alpha062']
    
    # 1. 从alpha_factor_score表读取
    if afs_factors:
        fn_list_str = ','.join(["'%s'" % fn for fn in afs_factors])
        for i in range(0, len(stock_codes), 300):
            chunk = stock_codes[i:i+300]
            ids = ','.join(["'%s'" % c for c in chunk])
            with db.cursor() as c:
                c.execute("SELECT ts_code, factor_name, factor_score FROM alpha_factor_score WHERE trade_date='%s' AND ts_code IN (%s) AND factor_name IN (%s)" % (trade_date, ids, fn_list_str))
                for r in c.fetchall():
                    result_map[r['factor_name']][r['ts_code']] = float(r['factor_score'])
    
    # 2. 从strategy_signal表读取alpha062
    if 'alpha062' in factor_names:
        for i in range(0, len(stock_codes), 300):
            chunk = stock_codes[i:i+300]
            ids = ','.join(["'%s'" % c for c in chunk])
            with db.cursor() as c:
                c.execute("SELECT ts_code, alpha062_score FROM strategy_signal WHERE trade_date='%s' AND ts_code IN (%s) AND alpha062_score IS NOT NULL" % (trade_date, ids))
                for r in c.fetchall():
                    result_map['alpha062'][r['ts_code']] = float(r['alpha062_score'])
    
    return result_map
    return result_map


def compute_alpha_bonus(alpha_rank_map, config):
    """
    计算alpha加分。
    alpha_rank_map: {factor_name: {ts_code: rank_0_1}}
    config: 情景配置，含 alpha_ranks, bonus_scale, bonus_offset, bonus_max, bonus_min
    """
    alpha_ranks = config['alpha_ranks']
    scale = config['bonus_scale']
    offset = config['bonus_offset']
    bmax = config['bonus_max']
    bmin = config['bonus_min']
    
    if not alpha_ranks or not alpha_rank_map:
        return {}
    
    # 收集所有涉及的ts_code
    all_ts = set()
    for fn in alpha_ranks:
        if fn in alpha_rank_map:
            all_ts.update(alpha_rank_map[fn].keys())
    
    bonuses = {}
    for ts in all_ts:
        bonus_raw = 0
        weight_sum = 0
        for fn, w in alpha_ranks.items():
            rank = alpha_rank_map.get(fn, {}).get(ts, 0.5)
            bonus_raw += rank * w
            weight_sum += w
        
        if weight_sum > 0:
            bonus_raw /= weight_sum  # 归一化
        bonus = bonus_raw * scale - offset
        bonus = max(bmin, min(bmax, bonus))
        bonuses[ts] = round(bonus, 1)
    
    return bonuses


# ==================== 回测 ====================

def backtest_matrix_simple(start_date='2023-07-01', end_date='2026-07-10'):
    """简易版回测：计算每5个交易日的情景矩阵 vs M1基准"""
    db = get_db()
    try:
        season_cache = get_season_cache(db)
        print("季节数据: %d 天" % len(season_cache))
        
        dates = sorted([d for d in season_cache.keys() if d >= start_date and d <= end_date])
        print("回测日期范围: %s ~ %s (%d 天)" % (dates[0], dates[-1], len(dates)))
        
        # 股票池
        with db.cursor() as c:
            c.execute("SELECT ts_code, name FROM watch_pool")
            pool = {r['ts_code']: r['name'] or '' for r in c.fetchall()}
        print("股票池: %d 只" % len(pool))
        
        # 采样日期（每5天）
        sample_dates = dates[::5]
        print("采样评估日: %d 个" % len(sample_dates))
        
        # 情景分布统计
        scenario_counts = defaultdict(int)
        for d in dates:
            _, config = get_scenario(d, season_cache)
            scenario_counts[config['name']] += 1
        print("\n情景分布:")
        for name, cnt in sorted(scenario_counts.items(), key=lambda x: -x[1]):
            print("  %-20s: %d天 (%.1f%%)" % (name, cnt, cnt/len(dates)*100))
        
        # 回测主循环
        results_by_scenario = defaultdict(lambda: {
            'name': '', 'days': 0, 'm1_scores': [], 'matrix_scores': [],
            'm1_above_threshold': 0, 'matrix_above_threshold': 0,
        })
        
        for idx, d in enumerate(sample_dates):
            season, config = get_scenario(d, season_cache)
            if not config:
                continue
            sname = config['name']
            if not results_by_scenario[season]['name']:
                results_by_scenario[season]['name'] = sname
            
            rs = results_by_scenario[season]
            rs['days'] += 1
            
            stock_codes = list(pool.keys())
            if not stock_codes:
                continue
            
            # 加载M1综合分
            m1_scores = load_m1_scores(db, d, stock_codes)
            if not m1_scores:
                continue
            
            # 加载Alpha因子评分
            alpha_factor_names = list(config['alpha_ranks'].keys())
            alpha_scores = {}
            if alpha_factor_names:
                alpha_map = load_alpha_scores_bulk(db, d, stock_codes, alpha_factor_names)
            
            # 计算截面Rank
            alpha_ranks = {}
            for fn in alpha_factor_names:
                if fn in alpha_map:
                    alpha_ranks[fn] = compute_cross_section_rank(alpha_map[fn])
            
            # 计算加分
            bonuses = compute_alpha_bonus(alpha_ranks, config)
            
            # 遍历每只股票
            for ts, m1_score in m1_scores.items():
                rs['m1_scores'].append(m1_score)
                bonus = bonuses.get(ts, 0)
                matrix_score = m1_score + bonus
                matrix_score = max(0, min(100, matrix_score))
                rs['matrix_scores'].append(matrix_score)
                
                if m1_score >= config['buy_threshold']:
                    rs['m1_above_threshold'] += 1
                if matrix_score >= config['buy_threshold']:
                    rs['matrix_above_threshold'] += 1
            
            if (idx + 1) % 30 == 0:
                print("  进度 %d/%d..." % (idx + 1, len(sample_dates)))
        
        # 输出结果
        print("\n" + "=" * 70)
        print("📊 情景化因子矩阵回测结果（截面Rank加分方案E）")
        print("=" * 70)
        
        for season in sorted(results_by_scenario.keys()):
            rs = results_by_scenario[season]
            if rs['days'] == 0:
                continue
            m1_avg = np.mean(rs['m1_scores']) if rs['m1_scores'] else 0
            mx_avg = np.mean(rs['matrix_scores']) if rs['matrix_scores'] else 0
            m1_top5 = np.mean(np.sort(rs['m1_scores'])[-5:]) if len(rs['m1_scores']) >= 5 else 0
            mx_top5 = np.mean(np.sort(rs['matrix_scores'])[-5:]) if len(rs['matrix_scores']) >= 5 else 0
            m1_pass = rs['m1_above_threshold']
            mx_pass = rs['matrix_above_threshold']
            total = len(rs['m1_scores'])
            
            print("\n" + "-" * 70)
            print("  %s (%s) — %d天" % (rs['name'], season, rs['days']))
            print("-" * 70)
            print("  %-20s %10s %10s %10s" % ('指标', '情景矩阵', 'M1基础', '差值'))
            print("  %-20s %10.2f %10.2f %+10.2f" % ('综合均分', mx_avg, m1_avg, mx_avg - m1_avg))
            print("  %-20s %10.2f %10.2f %+10.2f" % ('Top5均分', mx_top5, m1_top5, mx_top5 - m1_top5))
            print("  %-20s %10d %10d %+10d" % ('过线股票数', mx_pass, m1_pass, mx_pass - m1_pass))
            print("  %-20s %10.1f%% %10.1f%% %+10.1f%%" % ('过线比例', mx_pass/max(total,1)*100, m1_pass/max(total,1)*100, (mx_pass-m1_pass)/max(total,1)*100))
        
        # 整体
        all_m1 = []
        all_mx = []
        total_m1_pass = 0
        total_mx_pass = 0
        total_samples = 0
        for rs in results_by_scenario.values():
            all_m1.extend(rs['m1_scores'])
            all_mx.extend(rs['matrix_scores'])
            total_m1_pass += rs['m1_above_threshold']
            total_mx_pass += rs['matrix_above_threshold']
            total_samples += len(rs['m1_scores'])
        
        print("\n" + "=" * 70)
        print("📈 整体汇总")
        print("=" * 70)
        print("  回测天数: %d" % sum(rs['days'] for rs in results_by_scenario.values()))
        print("  样本总数: %d" % total_samples)
        print("  情景矩阵均分: %.2f" % np.mean(all_mx))
        print("  M1基础均分:   %.2f" % np.mean(all_m1))
        print("  增益: +%.2f" % (np.mean(all_mx) - np.mean(all_m1)))
        print("  M1过线数: %d (%.1f%%)" % (total_m1_pass, total_m1_pass/max(total_samples,1)*100))
        print("  矩阵过线数: %d (%.1f%%)" % (total_mx_pass, total_mx_pass/max(total_samples,1)*100))
        
    finally:
        db.close()


def main():
    print("=" * 60)
    print("bt_v13_matrix.py v2 — 情景化因子矩阵回测")
    print("方案: M1综合分 + 截面Rank Alpha加分")
    print("时间: %s" % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("=" * 60)
    
    backtest_matrix_simple()


if __name__ == '__main__':
    main()
