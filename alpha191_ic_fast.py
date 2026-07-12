#!/usr/bin/env python3
"""
Alpha191 因子 IC 快速验证版
=============================
对于每个交易日，取当日所有股票K线 + 前N日历史数据，
批量计算因子并算IC

采用按日循环（非按股票循环），大幅提高效率
"""

import os, sys, json, math, time
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from collections import defaultdict
sys.path.insert(0, '/opt/stock-analyzer')
import db_config

START = '2024-01-02'
END = '2026-07-10'

# 改为用ssht pass
import sys

ALPHAS = [
    ('alpha001', '量价背离', True, 7),
    ('alpha002', '日内振幅变化', True, 2),
    ('alpha011', '量价位置', True, 7),
    ('alpha014', '5日涨幅', False, 6),
    ('alpha018', '5日收盘比', False, 6),
    ('alpha020', '6日涨幅%', False, 7),
    ('alpha031', '12日偏离度', False, 13),
    ('alpha034', '12日均线比', False, 13),
    ('alpha040', '量比功率', False, 27),
    ('alpha043', '净量因子', False, 7),
    ('alpha046', '多均线位置', False, 25),
    ('alpha055', '随机指标', False, 13),
    ('alpha062', '高量负相关', True, 6),
    ('alpha064', '量价强度', False, 1),
    ('alpha084', '累积上涨', False, 21),
]


def compute_alphas_for_stock(df):
    """对单只股票的DataFrame计算所有alpha因子，返回 {date: {alpha: value}}"""
    n = len(df)
    if n < 30:
        return {}
    
    # 数据准备
    for col in ['open','high','low','close','vol','amount']:
        df[col] = df[col].astype(float)
    df['vwap'] = df['amount'] / (df['vol'] * 100 + 1)
    df['returns'] = df['close'].pct_change()
    
    results = {}
    
    for i in range(29, n):
        td = df['trade_date'].iloc[i]
        row = df.iloc[i]
        vals = {}
        
        c = row['close']; o = row['open']; h = row['high']
        l = row['low']; cv = row['vol']; ca = row['amount']; cw = row['vwap']
        
        # 取窗口数据
        w5 = df.iloc[i-4:i+1]   # i-4 ~ i
        w6 = df.iloc[i-5:i+1]   # i-5 ~ i
        w7 = df.iloc[i-6:i+1]   # i-6 ~ i
        w13 = df.iloc[i-12:i+1] # i-12 ~ i
        w21 = df.iloc[i-20:i+1] # i-20 ~ i
        w25 = df.iloc[i-24:i+1] # i-24 ~ i
        w27 = df.iloc[i-26:i+1] # i-26 ~ i
        
        try:
            # alpha014: close - delay(close,5)
            if len(w6) == 6:
                vals['alpha014'] = float(c - w6['close'].iloc[0])
            
            # alpha018: close/delay(close,5)
            if len(w6) == 6 and w6['close'].iloc[0] > 0:
                vals['alpha018'] = float(c / w6['close'].iloc[0])
            
            # alpha020 (6日涨幅)
            if len(w7) == 7 and w7['close'].iloc[0] > 0:
                vals['alpha020'] = float((c / w7['close'].iloc[0] - 1) * 100)
            
            # alpha031 (12日偏离度)
            if len(w13) >= 12:
                ma12 = w13['close'].mean()
                if ma12 > 0:
                    vals['alpha031'] = float((c - ma12) / ma12 * 100)
            
            # alpha034 (12日均线比)
            if len(w13) >= 12 and c > 0:
                vals['alpha034'] = float(w13['close'].mean() / c)
            
            # alpha040 (量比功率)
            if len(w27) >= 26:
                up_vol = w27[w27['close'] > w27['close'].shift(1)]['vol'].sum()
                dn_vol = w27[w27['close'] <= w27['close'].shift(1)]['vol'].sum()
                vals['alpha040'] = float(up_vol / max(dn_vol, 1) * 100)
            
            # alpha043 (净量因子)
            if len(w7) == 7:
                net = 0
                for j in range(6):
                    if float(w7['close'].iloc[j]) > float(w7['close'].iloc[j-1] if j>0 else df.iloc[i-7]['close']):
                        net += float(w7['vol'].iloc[j])
                    elif float(w7['close'].iloc[j]) < float(w7['close'].iloc[j-1] if j>0 else df.iloc[i-7]['close']):
                        net -= float(w7['vol'].iloc[j])
                vals['alpha043'] = net
            
            # alpha046 (多均线位置)
            if len(w25) >= 24 and c > 0:
                ma3 = df.iloc[i-2:i+1]['close'].mean()
                ma6 = df.iloc[i-5:i+1]['close'].mean() if i >= 5 else w13['close'].mean()
                ma12 = w13['close'].mean()
                ma24 = w25['close'].mean()
                vals['alpha046'] = float((ma3 + ma6 + ma12 + ma24) / (4 * c))
            
            # alpha055 (随机指标)
            if len(w13) >= 12:
                hi12 = w13['high'].max()
                lo12 = w13['low'].min()
                if hi12 > lo12:
                    vals['alpha055'] = float((c - lo12) / (hi12 - lo12) * 100)
            
            # alpha062 (高量负相关)
            if len(w6) == 6:
                if np.std(w6['high']) > 0 and np.std(w6['vol']) > 0:
                    corr = np.corrcoef(w6['high'], w6['vol'])[0, 1]
                    vals['alpha062'] = float(-corr)
            
            # alpha064 (量价强度)
            if o > 0 and cv > 0:
                vals['alpha064'] = float((c - o) / o * cv)
            
            # alpha084 (累积上涨)
            if len(w21) >= 20:
                up_sum = 0
                for j in range(19):
                    diff = float(w21['close'].iloc[j+1] - w21['close'].iloc[j])
                    if diff > 0:
                        up_sum += diff
                vals['alpha084'] = up_sum
            
            # alpha001 (需要滑动窗口)
            if len(w7) == 7:
                dvol = np.log(np.maximum(w7['vol'].values, 1))
                dvol_delta = np.diff(dvol)
                ret_w7 = (w7['close'].values - w7['open'].values) / np.maximum(w7['open'].values, 0.001)
                if len(dvol_delta) == 6:
                    r1 = pd.Series(dvol_delta).rank(pct=True).values
                    r2 = pd.Series(ret_w7).rank(pct=True).values
                    corr = np.corrcoef(r1, r2)[0, 1] if len(r1) > 1 else 0
                    vals['alpha001'] = float(-corr)
            
            # alpha002 (日内振幅变化)
            if len(w6) == 6:
                def _hl(x): return (x['high'].values - x['low'].values)
                hl_now = (h - l) / max((h - l), 0.001)
                hl_prev = (float(w6['high'].iloc[-2]) - float(w6['low'].iloc[-2])) / max((float(w6['high'].iloc[-2]) - float(w6['low'].iloc[-2])), 0.001)
                val = ((c - l) - (h - c)) / max(h - l, 0.001)
                prev_val = ((float(w6['close'].iloc[-2]) - float(w6['low'].iloc[-2])) - (float(w6['high'].iloc[-2]) - float(w6['close'].iloc[-2]))) / max(float(w6['high'].iloc[-2]) - float(w6['low'].iloc[-2]), 0.001)
                vals['alpha002'] = float(-1 * (val - prev_val))
            
            # alpha011 (量价位置)
            if len(w7) == 7:
                parts = []
                for j in range(6):
                    hl_w = max(float(w7['high'].iloc[j]) - float(w7['low'].iloc[j]), 0.001)
                    parts.append(((float(w7['close'].iloc[j]) - float(w7['low'].iloc[j])) - (float(w7['high'].iloc[j]) - float(w7['close'].iloc[j]))) / hl_w * float(w7['vol'].iloc[j]))
                vals['alpha011'] = sum(parts)
            
        except Exception as e:
            pass
        
        if vals:
            results[td] = vals
    
    return results


def main():
    pwd = db_config._get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2', charset='utf8mb4',
                           cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    
    print(f"⏳ 加载数据 {START}~{END}...", end='', flush=True)
    cur.execute("SELECT ts_code FROM backtest_pool")
    codes = [r['ts_code'] for r in cur.fetchall()]
    
    cur.execute('''
        SELECT dk.ts_code, dk.trade_date, dk.open, dk.high, dk.low, dk.close, dk.vol, dk.amount
        FROM daily_kline dk
        INNER JOIN backtest_pool bp ON dk.ts_code = bp.ts_code
        WHERE dk.trade_date>=%s AND dk.trade_date<=%s
        ORDER BY dk.ts_code, dk.trade_date
    ''', (START, END))
    rows = cur.fetchall()
    conn.close()
    print(f" {len(rows)}条/{len(codes)}只")
    
    # 按股票分组
    stock_data = defaultdict(list)
    for r in rows:
        r['trade_date'] = r['trade_date'].strftime('%Y-%m-%d')
        stock_data[r['ts_code']].append(r)
    
    # 构建日期-收盘价索引（用于计算未来收益）
    date_close = defaultdict(dict)
    for code, klines in stock_data.items():
        for r in klines:
            date_close[r['trade_date']][code] = float(r['close'])
    alld = sorted(date_close.keys())
    print(f"  {len(alld)}个交易日")
    
    # 按股票计算因子
    print(f"\n⏳ 计算Alpha因子...")
    alpha_records = defaultdict(lambda: defaultdict(list))
    
    total = len(stock_data)
    for idx, (code, klines) in enumerate(stock_data.items()):
        if idx % 50 == 0:
            print(f"  [{idx}/{total}]")
        
        df = pd.DataFrame(klines)
        if len(df) < 30:
            continue
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df = df.sort_values('trade_date').reset_index(drop=True)
        
        alpha_vals = compute_alphas_for_stock(df)
        
        for td, factors in alpha_vals.items():
            td_idx = alld.index(td) if td in alld else -1
            if td_idx < 0 or td_idx + 5 >= len(alld):
                continue
            future_date = alld[td_idx + 5]
            future_close = date_close[future_date].get(code)
            today_close = date_close[td].get(code)
            if today_close is None or future_close is None or today_close == 0:
                continue
            future_ret = (future_close / today_close - 1) * 100
            
            for aname, aval in factors.items():
                if aval is not None and not math.isnan(aval) and not math.isinf(aval):
                    alpha_records[aname][td].append((aval, future_ret))
    
    # 计算IC
    print(f"\n\n{'='*70}")
    print(f"  📊 Alpha191 因子 IC (5日持有期)")
    print(f"{'='*70}")
    print(f"  {'因子':12s} {'中文名':12s} {'平均IC':>8s} {'IR':>5s} {'正IC%':>6s} {'样本':>8s}")
    print(f"  {'─'*54}")
    
    ic_results = {}
    for aname, cname, *_ in ALPHAS:
        records = alpha_records.get(aname, {})
        if not records:
            continue
        
        daily_ics = []
        total_pairs = 0
        for td, pairs in records.items():
            if len(pairs) < 10:
                continue
            vals, rets = zip(*pairs)
            try:
                rho, _ = spearmanr(vals, rets)
                if not math.isnan(rho):
                    daily_ics.append(rho)
                    total_pairs += len(pairs)
            except:
                pass
        
        if len(daily_ics) < 5:
            continue
        
        ic_arr = np.array(daily_ics)
        mean_ic = np.mean(ic_arr)
        std_ic = np.std(ic_arr)
        ir = mean_ic / std_ic if std_ic > 0 else 0
        pos_pct = sum(1 for ic in daily_ics if ic > 0) / len(daily_ics) * 100
        
        icon = '✅' if abs(mean_ic) > 0.03 else ('⚡' if abs(mean_ic) > 0.01 else '⚠️')
        print(f"  {aname:12s} {cname:12s} {mean_ic:+7.4f}{icon} {ir:5.2f} {pos_pct:5.0f}% {total_pairs:>8,}")
        
        ic_results[aname] = {
            'name': cname, 'ic': round(mean_ic, 4), 'ir': round(ir, 2),
            'pos_pct': round(pos_pct, 1), 'n_days': len(daily_ics), 'n_pairs': total_pairs
        }
    
    assert sorted(ic_results.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    fp = f'/tmp/alpha191_ic_{time.strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp, 'w') as f:
        json.dump(ic_results, f, indent=2)
    print(f"📁 {fp}")


if __name__ == '__main__':
    import pymysql
    main()
