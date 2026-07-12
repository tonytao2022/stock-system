#!/usr/bin/env python3
"""
Alpha191 因子 IC 验证 v3 — 用 daily_kline（非复权）跑全量
===========================================================
对 backtest_pool 的847只，2024-01-02 ~ 2026-07-10
计算28个Alpha因子的 RankIC（5日持有期）
"""

import os, sys, json, math, time
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from collections import defaultdict
sys.path.insert(0, '/opt/stock-analyzer')
import db_config
import pymysql

START = '2024-01-02'
END = '2026-07-10'

# 全部待计算因子（只含不依赖benchmark的）
ALPHA_NAMES = {
    'alpha001': '量价背离',     'alpha002': '日内振幅变化',
    'alpha004': '收盘组合判断', 'alpha005': '量价时序相关',
    'alpha011': '量价位置因子', 'alpha014': '5日涨幅',
    'alpha018': '5日收盘比',    'alpha019': '5日涨跌幅条件',
    'alpha020': '6日涨幅',      'alpha031': '12日偏离度',
    'alpha032': '高中量相关',   'alpha034': '12日均线比',
    'alpha036': '量价秩相关',   'alpha040': '量比功率',
    'alpha043': '净量因子',     'alpha046': '多均线位置',
    'alpha048': '方向变化+量',  'alpha055': '随机指标',
    'alpha056': '开盘位置',     'alpha062': '高量负相关',
    'alpha064': '量价强度',     'alpha084': '累积上涨',
    'alpha087': '日内波动',     'alpha089': '高低量相关',
    'alpha092': '衰减量价',     'alpha094': '相对强度+量',
    'alpha102': '三维量价',     'alpha108': '相对波动',
}

MAX_STOCKS = 847  # backtest_pool 总股票数


def compute_alphas_for_stock(df):
    """对单只股票的DataFrame计算所有alpha因子，返回 {date: {alpha: value}}"""
    n = len(df)
    if n < 30:
        return {}
    
    for col in ['open','high','low','close','vol','amount']:
        df[col] = df[col].astype(float)
    T = df[['trade_date','open','high','low','close','vol','amount']].copy()
    
    results = {}
    
    for i in range(29, n):
        td = T['trade_date'].iloc[i]
        vals = {}
        
        c = float(T['close'].iloc[i]); o = float(T['open'].iloc[i])
        h = float(T['high'].iloc[i]); l = float(T['low'].iloc[i])
        cv = float(T['vol'].iloc[i])
        
        try:
            # --- 基础指标 ---
            # alpha014: close - delay(close,5)
            if i >= 5:
                c5 = float(T['close'].iloc[i-5])
                vals['alpha014'] = c - c5
            
            # alpha018: close / delay(close,5)
            if i >= 5:
                c5 = float(T['close'].iloc[i-5])
                if c5 > 0:
                    vals['alpha018'] = c / c5
            
            # alpha019: if close=delay(close,5) then -1 else close-delay(close,5)
            if i >= 5:
                c5 = float(T['close'].iloc[i-5])
                vals['alpha019'] = -1 if abs(c - c5) < 0.001 else (c - c5) / max(abs(c - c5), 0.001)
            
            # alpha020: (close-delay(close,6))/delay(close,6)*100
            if i >= 6:
                c6 = float(T['close'].iloc[i-6])
                if c6 > 0:
                    vals['alpha020'] = (c - c6) / c6 * 100
            
            # alpha031: (close - mean(close,12)) / mean(close,12) * 100
            if i >= 11:
                ma12 = float(T['close'].iloc[i-11:i+1].mean())
                if ma12 > 0:
                    vals['alpha031'] = (c - ma12) / ma12 * 100
            
            # alpha034: mean(close,12) / close
            if i >= 11 and c > 0:
                ma12 = float(T['close'].iloc[i-11:i+1].mean())
                vals['alpha034'] = ma12 / c
            
            # alpha046: (ma3+ma6+ma12+ma24)/(4*close)
            if i >= 23 and c > 0:
                ma3 = float(T['close'].iloc[i-2:i+1].mean())
                ma6 = float(T['close'].iloc[i-5:i+1].mean())
                ma12 = float(T['close'].iloc[i-11:i+1].mean())
                ma24 = float(T['close'].iloc[i-23:i+1].mean())
                vals['alpha046'] = (ma3 + ma6 + ma12 + ma24) / (4 * c)
            
            # alpha055: (close-tsmin(low,12))/(tsmax(high,12)-tsmin(low,12))*100
            if i >= 11:
                hi12 = float(T['high'].iloc[i-11:i+1].max())
                lo12 = float(T['low'].iloc[i-11:i+1].min())
                if hi12 > lo12:
                    vals['alpha055'] = (c - lo12) / (hi12 - lo12) * 100
            
            # alpha056: (close-tsmin(low,12))/(tsmax(high,12)-tsmin(low,12)) - rank(volume)
            if i >= 11:
                hi12 = float(T['high'].iloc[i-11:i+1].max())
                lo12 = float(T['low'].iloc[i-11:i+1].min())
                if hi12 > lo12:
                    v1 = (c - lo12) / (hi12 - lo12)
                    vals['alpha056'] = v1  # rank(vol) 需要横截面，此处只给原始值
                else:
                    vals['alpha056'] = 0
            
            # alpha087: (high - low) / close — 日内波动
            if c > 0:
                vals['alpha087'] = (h - l) / c
            
            # --- 需要 vol 的逻辑 ---
            # alpha040: Sum(up_vol,26)/Sum(dn_vol,26)*100
            if i >= 26:
                up_vol = 0.0; dn_vol = 0.0
                for j in range(i-25, i+1):
                    if float(T['close'].iloc[j]) > float(T['close'].iloc[j-1]):
                        up_vol += float(T['vol'].iloc[j])
                    else:
                        dn_vol += float(T['vol'].iloc[j])
                vals['alpha040'] = up_vol / max(dn_vol, 1) * 100
            
            # alpha043: 净量因子 (close>prev close?vol:-vol, 6日累加)
            if i >= 6:
                net = 0.0
                for j in range(i-5, i+1):
                    if float(T['close'].iloc[j]) > float(T['close'].iloc[j-1]):
                        net += float(T['vol'].iloc[j])
                    else:
                        net -= float(T['vol'].iloc[j])
                vals['alpha043'] = net
            
            # alpha062: -1 * corr(high, volume, 5)
            if i >= 4:
                h5 = T['high'].iloc[i-4:i+1].values.astype(float)
                v5 = T['vol'].iloc[i-4:i+1].values.astype(float)
                if np.std(h5) > 0 and np.std(v5) > 0:
                    corr = np.corrcoef(h5, v5)[0, 1]
                    vals['alpha062'] = float(-corr)
            
            # alpha064: (close-open)/open * volume
            if o > 0 and cv > 0:
                vals['alpha064'] = (c - o) / o * cv
            
            # alpha084: Sum(positive_close_diff, 20)
            if i >= 20:
                up_sum = 0.0
                for j in range(i-19, i+1):
                    diff = float(T['close'].iloc[j] - T['close'].iloc[j-1])
                    if diff > 0:
                        up_sum += diff
                vals['alpha084'] = up_sum
            
            # alpha094: -1 * ((close-open)/open) * volume (cross-section rank 简化)
            if o > 0 and cv > 0:
                vals['alpha094'] = -1 * ((c-o)/o) * cv
            
            # alpha108: -1 * ((high-low)/close) * (high/low)
            if c > 0 and l > 0:
                vals['alpha108'] = -1 * ((h-l)/c) * (h/l)
            
            # --- 需要 rank / 多周期 ---
            # alpha001: -1 * Corr(Rank(Delta(Log(vol),1)), Rank((close-open)/open), 6)
            if i >= 5:
                v7 = T['vol'].iloc[i-5:i+1].values.astype(float)
                v7 = np.maximum(v7, 1)
                dvol = np.diff(np.log(v7))
                close7 = T['close'].iloc[i-5:i+1].values.astype(float)
                open7 = T['open'].iloc[i-5:i+1].values.astype(float)
                ret7 = (close7 - open7) / np.maximum(open7, 0.001)
                if len(dvol) == 5:
                    r1 = pd.Series(dvol).rank(pct=True).values
                    r2 = pd.Series(ret7).rank(pct=True).values
                    corr = np.corrcoef(r1, r2)[0, 1] if len(r1) > 1 and np.std(r1) > 0 and np.std(r2) > 0 else 0
                    vals['alpha001'] = float(-corr)
            
            # alpha002: -1 * delta((((close-low)-(high-close))/(high-low)), 1)
            if i >= 1:
                hl = max(h - l, 0.001)
                v_now = ((c - l) - (h - c)) / hl
                c_prev = float(T['close'].iloc[i-1]); h_prev = float(T['high'].iloc[i-1])
                l_prev = float(T['low'].iloc[i-1])
                hl_prev = max(h_prev - l_prev, 0.001)
                v_prev = ((c_prev - l_prev) - (h_prev - c_prev)) / hl_prev
                vals['alpha002'] = float(-(v_now - v_prev))
            
            # alpha004: (close - open) * (high - low) / (open + 1)
            if o > 0:
                vals['alpha004'] = (c - o) * (h - l) / (o + 0.001)
            
            # alpha005: -1 * corr(rank(close), rank(volume), 5)
            if i >= 4:
                c5 = T['close'].iloc[i-4:i+1].values.astype(float)
                v5 = T['vol'].iloc[i-4:i+1].values.astype(float)
                r_c5 = pd.Series(c5).rank(pct=True).values
                r_v5 = pd.Series(v5).rank(pct=True).values
                if np.std(r_c5) > 0 and np.std(r_v5) > 0:
                    corr = np.corrcoef(r_c5, r_v5)[0, 1]
                    vals['alpha005'] = float(-corr)
            
            # alpha011: Sum(((close-low)-(high-close))/(high-low)*volume, 6)
            if i >= 5:
                s = 0.0
                for j in range(i-5, i+1):
                    cj = float(T['close'].iloc[j]); hj = float(T['high'].iloc[j])
                    lj = float(T['low'].iloc[j]); vj = float(T['vol'].iloc[j])
                    hlj = max(hj - lj, 0.001)
                    s += ((cj - lj) - (hj - cj)) / hlj * vj
                vals['alpha011'] = s
            
            # alpha032: (ma(close,7) - close) / close * volume  — 高中量相关简化
            if i >= 6 and c > 0 and cv > 0:
                ma7 = float(T['close'].iloc[i-6:i+1].mean())
                vals['alpha032'] = (ma7 - c) / c * cv
            
            # alpha036: corr(rank(close), rank(volume), 6)  — 量价秩相关
            if i >= 5:
                c6 = T['close'].iloc[i-5:i+1].values.astype(float)
                v6 = T['vol'].iloc[i-5:i+1].values.astype(float)
                r_c6 = pd.Series(c6).rank(pct=True).values
                r_v6 = pd.Series(v6).rank(pct=True).values
                if np.std(r_c6) > 0 and np.std(r_v6) > 0:
                    corr = np.corrcoef(r_c6, r_v6)[0, 1]
                    vals['alpha036'] = float(corr)
            
            # alpha048: rank(corr(close,volume,5)) * rank(delta(close,1)) 简版
            if i >= 5 and i >= 1:
                c5arr = T['close'].iloc[i-4:i+1].values.astype(float)
                v5arr = T['vol'].iloc[i-4:i+1].values.astype(float)
                if np.std(c5arr) > 0 and np.std(v5arr) > 0:
                    corr = np.corrcoef(c5arr, v5arr)[0, 1]
                    delta_c = c - float(T['close'].iloc[i-1])
                    vals['alpha048'] = float(corr * delta_c)
            
            # alpha089: 1 - corr(close, vol, 13)  — 高低量相关
            if i >= 12:
                c13 = T['close'].iloc[i-12:i+1].values.astype(float)
                v13 = T['vol'].iloc[i-12:i+1].values.astype(float)
                if np.std(c13) > 0 and np.std(v13) > 0:
                    corr = np.corrcoef(c13, v13)[0, 1]
                    vals['alpha089'] = float(1 - corr)
            
            # alpha092: max(close,13) - min(close,13) / close * volume 简版
            if i >= 12 and c > 0 and cv > 0:
                hi13 = float(T['close'].iloc[i-12:i+1].max())
                lo13 = float(T['close'].iloc[i-12:i+1].min())
                vals['alpha092'] = (hi13 - lo13) / c * cv
            
            # alpha102: volume * (high - low) / close — 三维量价
            if c > 0 and cv > 0:
                vals['alpha102'] = cv * (h - l) / c
            
        except Exception:
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
    
    print(f"⏳ 加载 daily_kline {START}~{END}...", end='', flush=True)
    cur.execute("SELECT ts_code FROM backtest_pool")
    codes = [r['ts_code'] for r in cur.fetchall()]
    
    cur.execute(f"""
        SELECT dk.ts_code, dk.trade_date, dk.`open`, dk.high, dk.low, dk.`close`, dk.vol, dk.amount
        FROM daily_kline dk
        INNER JOIN backtest_pool bp ON dk.ts_code = bp.ts_code
        WHERE dk.trade_date>=%s AND dk.trade_date<=%s
        ORDER BY dk.ts_code, dk.trade_date
    """, (START, END))
    rows = cur.fetchall()
    conn.close()
    print(f" {len(rows)}条/{len(codes)}只")
    
    stock_data = defaultdict(list)
    for r in rows:
        r['trade_date'] = r['trade_date'].strftime('%Y-%m-%d')
        stock_data[r['ts_code']].append(r)
    
    # 日期-收盘价索引
    date_close = defaultdict(dict)
    for code, klines in stock_data.items():
        for r in klines:
            date_close[r['trade_date']][code] = float(r['close'])
    alld = sorted(date_close.keys())
    print(f"  {len(alld)}个交易日")
    
    # 逐股票计算因子
    print(f"\n⏳ 逐日计算Alpha因子...")
    alpha_records = defaultdict(lambda: defaultdict(list))
    
    total = len(stock_data)
    t0 = time.time()
    for idx, (code, klines) in enumerate(stock_data.items()):
        if idx % 100 == 0:
            elapsed = time.time() - t0
            print(f"  [{idx}/{total}] {elapsed:.0f}s")
        
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
    print(f"  📊 Alpha191 因子 IC 验证（5日持有期）")
    print(f"  数据源: daily_kline | {len(stock_data)}只 | {len(alld)}交易日 | {START}~{END}")
    print(f"{'='*70}")
    print(f"  {'因子':12s} {'中文名':10s} {'平均IC':>8s} {'IR':>5s} {'正IC%':>6s} {'有效天':>6s}")
    print(f"  {'─'*52}")
    
    ic_results = {}
    for aname in sorted(ALPHA_NAMES.keys()):
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
            print(f"  {aname:12s} {'(样本不足)':10s} {'─':>8s}")
            continue
        
        ic_arr = np.array(daily_ics)
        mean_ic = np.mean(ic_arr)
        std_ic = np.std(ic_arr)
        ir = mean_ic / std_ic if std_ic > 0 else 0
        pos_pct = sum(1 for ic in daily_ics if ic > 0) / len(daily_ics) * 100
        
        cname = ALPHA_NAMES.get(aname, '')
        icon = '✅' if abs(mean_ic) > 0.025 else ('⚡' if abs(mean_ic) > 0.01 else '⚠️')
        print(f"  {aname:12s} {cname:10s} {mean_ic:+7.4f}{icon} {ir:5.2f} {pos_pct:5.0f}% {len(daily_ics):6d}")
        
        ic_results[aname] = {
            'name': cname, 'ic': round(mean_ic, 4), 'ir': round(ir, 2),
            'pos_pct': round(pos_pct, 1), 'n_days': len(daily_ics), 'n_pairs': total_pairs
        }
    
    # 排序
    sorted_ics = sorted(ic_results.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    print(f"\n{'─'*70}")
    print(f"  📊 达到门槛的 Alpha 因子 (|IC| >= 0.025)")
    print(f"{'─'*70}")
    print(f"  {'因子':12s} {'中文名':10s} {'IC':>8s} {'IR':>5s} {'正IC%':>6s} {'样本':>8s}")
    print(f"  {'─'*52}")
    
    qualifying = [x for x in sorted_ics if abs(x[1]['ic']) >= 0.025]
    for aname, r in qualifying:
        icon = '✅' if r['ic'] > 0 else '🔻'
        print(f"  {aname:12s} {r['name']:10s} {r['ic']:+7.4f}{icon} {r['ir']:5.2f} {r['pos_pct']:5.0f}% {r['n_pairs']:>8,}")
    
    print(f"\n{'─'*70}")
    print(f"  ⚡ IC 0.01~0.024 的弱相关因子")
    print(f"{'─'*70}")
    weak = [x for x in sorted_ics if 0.01 <= abs(x[1]['ic']) < 0.025]
    for aname, r in weak:
        print(f"  {aname:12s} {r['name']:10s} {r['ic']:+7.4f} {r['ir']:5.2f} {r['pos_pct']:5.0f}% {r['n_pairs']:>8,}")
    
    print(f"\n{'─'*70}")
    print(f"  ❌ 无效因子 (|IC| < 0.01)")
    print(f"{'─'*70}")
    bad = [x for x in sorted_ics if abs(x[1]['ic']) < 0.01]
    for aname, r in bad:
        print(f"  {aname:12s} {r['name']:10s} {r['ic']:+7.4f} {r['ir']:5.2f} {r['pos_pct']:5.0f}% {r['n_pairs']:>8,}")
    
    # 摘要
    print(f"\n{'='*70}")
    print(f"  📋 汇总: 有效{len(qualifying)}个 | 弱相关{len(weak)}个 | 无效{len(bad)}个 | 总计{len(sorted_ics)}个")
    print(f"{'='*70}")
    
    fp = f'/tmp/alpha191_ic_v3_{time.strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp, 'w') as f:
        json.dump(ic_results, f, indent=2, ensure_ascii=False)
    print(f"\n📁 结果保存: {fp}")


if __name__ == '__main__':
    main()
