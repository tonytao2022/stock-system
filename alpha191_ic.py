#!/usr/bin/env python3
"""
Alpha191 因子 IC 验证 — 按日循环版
=====================================
直接从 MySQL 逐日读取K线数据，计算每个Alpha因子的RankIC

优点：
- 不需要pivot宽表（内存友好）
- 直接输出IC结果，效率高

用法:
  python3 alpha191_ic.py
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

# 待测试的alpha因子列表（手动筛选的，只含不依赖benchmark的）
ALPHA_NAMES = {
    'alpha001': '量价背离',
    'alpha002': '日内振幅变化',
    'alpha004': '收盘组合判断',
    'alpha005': '量价时序相关',
    'alpha011': '量价位置因子',
    'alpha014': '5日涨幅',
    'alpha018': '5日收盘比',
    'alpha019': '5日涨跌幅条件',
    'alpha020': '6日涨幅',
    'alpha031': '12日偏离度',
    'alpha032': '高中量相关',
    'alpha034': '12日均线比',
    'alpha036': '量价秩相关',
    'alpha040': '量比功率',
    'alpha043': '净量因子',
    'alpha046': '多均线位置',
    'alpha048': '方向变化+量',
    'alpha055': '随机指标',
    'alpha056': '开盘位置',
    'alpha062': '高量负相关',
    'alpha064': '量价强度',
    'alpha084': '累积上涨',
    'alpha087': '日内波动',
    'alpha089': '高低量相关',
    'alpha092': '衰减量价',
    'alpha094': '相对强度+量',
    'alpha102': '三维量价',
    'alpha108': '相对波动',
}


def compute_alpha_for_day(code, df_daily):
    """对单只股票的日K线DataFrame计算所有alpha因子"""
    if df_daily is None or len(df_daily) < 30:
        return {}
    
    d = df_daily.copy()
    for col in ['close','open','high','low','vol','amount']:
        d[col] = d[col].astype(float)
    d['returns'] = d['close'].pct_change()
    d['vwap'] = d['amount'] / (d['vol'] * 100)
    d['vol_ratio'] = d['vol'] / d['vol'].rolling(20).mean()
    
    results = {}
    
    # 基础算子辅助
    def _rank_series(s):
        return s.rank(method='min', pct=True)
    
    for i in range(len(d)):
        # 跳过最开始的不够窗口的行
        enough = True
        if i < 30:  
            continue
        
        row = d.iloc[i]
        td = row['trade_date']
        
        # 取足够的历史窗口
        hist = d.iloc[max(0, i-260):i+1].reset_index(drop=True)
        n = len(hist)
        if n < 30:
            continue
        
        # 计算值
        try:
            close = hist['close'].values
            open_ = hist['open'].values
            high = hist['high'].values
            low = hist['low'].values
            vol = hist['vol'].values
            vwap = hist['vwap'].values
            ret = hist['returns'].values
            vol_ratio = hist['vol_ratio'].values
            
            c = close[-1]; o = open_[-1]; h = high[-1]; l = low[-1]
            cv = vol[-1]; cw = vwap[-1]
            
            vals = {}
            
            # alpha001: (-1 * Corr(Rank(Delta(Log(volume),1)), Rank((close-open)/open), 6))
            if n >= 7:
                dvol = np.log(np.maximum(vol[-7:], 1))
                dvol_delta = np.diff(dvol)
                ret_today = (close[-7:] - open_[-7:]) / np.maximum(open_[-7:], 0.001)
                if len(dvol_delta) == 6:
                    r1 = pd.Series(dvol_delta).rank(pct=True).values
                    r2 = pd.Series(ret_today).rank(pct=True).values
                    corr = np.corrcoef(r1, r2)[0, 1] if len(r1) > 1 else 0
                    vals['alpha001'] = -corr
            
            # alpha002: -1 * delta((((close-low)-(high-close))/(high-low)), 1)
            if n >= 2:
                hl = high[-2:] - low[-2:]
                val2 = -1 * (((close[-1]-low[-1])-(high[-1]-close[-1]))/max(hl[-1], 0.001) - 
                            ((close[-2]-low[-2])-(high[-2]-close[-2]))/max(hl[-2], 0.001))
                vals['alpha002'] = val2
            
            # alpha011: Sum(((close-low)-(high-close))/(high-low)*volume, 6)
            if n >= 6:
                parts = []
                for j in range(-6, 0):
                    hl_j = max(high[j] - low[j], 0.001)
                    parts.append(((close[j]-low[j])-(high[j]-close[j]))/hl_j * vol[j])
                vals['alpha011'] = sum(parts)
            
            # alpha014: close - delay(close, 5)
            if n >= 6:
                vals['alpha014'] = c - close[-6]
            
            # alpha018: close / delay(close, 5)
            if n >= 6 and close[-6] > 0:
                vals['alpha018'] = c / close[-6]
            
            # alpha020: (close-delay(close,6))/delay(close,6)*100
            if n >= 7 and close[-7] > 0:
                vals['alpha020'] = (c - close[-7]) / close[-7] * 100
            
            # alpha031: (close - mean(close,12)) / mean(close,12) * 100
            if n >= 12:
                ma12 = np.mean(close[-12:])
                if ma12 > 0:
                    vals['alpha031'] = (c - ma12) / ma12 * 100
            
            # alpha034: mean(close,12) / close
            if n >= 12 and c > 0:
                vals['alpha034'] = np.mean(close[-12:]) / c
            
            # alpha040: Sum(up_vol,26)/Sum(dn_vol,26)*100
            if n >= 27:
                up_sum = 0; dn_sum = 0
                for j in range(-26, 0):
                    if close[j] > close[j-1]:
                        up_sum += vol[j]
                    else:
                        dn_sum += vol[j]
                vals['alpha040'] = up_sum / max(dn_sum, 1) * 100 if dn_sum > 0 else 100
            
            # alpha043: Sum((close>delay(close,1)?vol:(close<delay(close,1)?-vol:0)),6)
            if n >= 7:
                net_vol = 0
                for j in range(-6, 0):
                    if close[j] > close[j-1]:
                        net_vol += vol[j]
                    elif close[j] < close[j-1]:
                        net_vol -= vol[j]
                vals['alpha043'] = net_vol
            
            # alpha046: (ma3+ma6+ma12+ma24)/(4*close)
            if n >= 24 and c > 0:
                ma3 = np.mean(close[-3:])
                ma6 = np.mean(close[-6:])
                ma12 = np.mean(close[-12:])
                ma24 = np.mean(close[-24:])
                vals['alpha046'] = (ma3 + ma6 + ma12 + ma24) / (4 * c)
            
            # alpha055: (close-tsmin(low,12))/(tsmax(high,12)-tsmin(low,12))*100
            if n >= 12:
                hi12 = np.max(high[-12:])
                lo12 = np.min(low[-12:])
                if hi12 > lo12:
                    vals['alpha055'] = (c - lo12) / (hi12 - lo12) * 100
            
            # alpha062: (-1 * corr(high, volume, 5))
            if n >= 5:
                corr_hv = np.corrcoef(high[-5:], vol[-5:])[0, 1] if len(high[-5:])>1 and np.std(high[-5:])>0 and np.std(vol[-5:])>0 else 0
                vals['alpha062'] = -corr_hv
            
            # alpha064: (close-open)/open * volume
            if o > 0:
                vals['alpha064'] = (c - o) / o * cv
            
            # alpha084: Sum((close-delay(close,1)>0?close-delay(close,1):0),20)
            if n >= 21:
                up_sum = 0
                for j in range(-20, 0):
                    diff = close[j] - close[j-1]
                    if diff > 0:
                        up_sum += diff
                vals['alpha084'] = up_sum
            
            # alpha087: (-1 * rank(close-open) + rank(high-low))
            vals['alpha087'] = -_rank_series(pd.Series([c-o]))[0] if n>0 else 0 + \
                               _rank_series(pd.Series([h-l]))[0] if n>0 else 0
            
            # alpha094: (-1 * rank((close-open)/open) * rank(volume))
            if o > 0:
                v1 = _rank_series(pd.Series([(c-o)/o]))[0]
                v2 = _rank_series(pd.Series([cv]))[0]
                vals['alpha094'] = -v1 * v2
            
            # alpha108: (-1 * rank((high-low)/close) * rank(high/low))
            if c > 0 and l > 0:
                v1 = _rank_series(pd.Series([(h-l)/c]))[0]
                v2 = _rank_series(pd.Series([h/l]))[0]
                vals['alpha108'] = -v1 * v2
            
            if vals:
                results[td] = vals
        except:
            pass
    
    return results


def main():
    pwd = db_config._get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2',
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    
    print(f"⏳ 取K线数据 {START}~{END}...", end='', flush=True)
    cur.execute("SELECT ts_code FROM backtest_pool")
    codes = [r['ts_code'] for r in cur.fetchall()]
    
    # 取所有K线（用backtest_pool的ts_code，全部读不需要IN子句）
    cur.execute(f"""
        SELECT dk.ts_code, dk.trade_date, dk.`open`, dk.high, dk.low, dk.`close`, dk.vol, dk.amount
        FROM daily_kline_qfq dk
        INNER JOIN backtest_pool bp ON dk.ts_code = bp.ts_code
        WHERE dk.trade_date>=%s AND dk.trade_date<=%s
        ORDER BY dk.ts_code, dk.trade_date
    """, (START, END))
    rows = cur.fetchall()
    conn.close()
    print(f" {len(rows)}条/{len(codes)}只")
    
    # 按股票分组
    stock_kline = defaultdict(list)
    for r in rows:
        stock_kline[r['ts_code']].append(r)
    
    # 计算因子值
    print(f"\n⏳ 逐日计算Alpha因子 IC...")
    
    # 结构: {trade_date: {alpha_name: [(value, future_return)]}}
    alpha_records = defaultdict(lambda: defaultdict(list))
    date_close_index = defaultdict(dict)
    
    # 先构建日期索引的close
    for code, klines in stock_kline.items():
        for r in klines:
            td = r['trade_date'].strftime('%Y-%m-%d')
            date_close_index[td][code] = float(r['close'])
    
    alld = sorted(date_close_index.keys())
    total = len(stock_kline)
    
    for idx, (code, klines) in enumerate(stock_kline.items()):
        if idx % 100 == 0:
            print(f"  [{idx}/{total}] {code}")
        
        df = pd.DataFrame(klines)
        if len(df) < 30:
            continue
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df = df.sort_values('trade_date').reset_index(drop=True)
        
        alpha_vals = compute_alpha_for_day(code, df)
        
        for td, factors in alpha_vals.items():
            # 找未来5日收益
            td_idx = alld.index(td) if td in alld else -1
            if td_idx < 0 or td_idx + 5 >= len(alld):
                continue
            future_date = alld[td_idx + 5]
            future_close = date_close_index[future_date].get(code)
            today_close = date_close_index[td].get(code)
            if today_close is None or future_close is None or today_close == 0:
                continue
            future_ret = (future_close / today_close - 1) * 100
            
            for aname, aval in factors.items():
                if aval is not None and not math.isnan(aval) and not math.isinf(aval):
                    alpha_records[aname][td].append((aval, future_ret))
    
    # 计算IC
    print(f"\n\n{'='*70}")
    print(f"  📊 Alpha191 因子 IC 验证（5日持有期）")
    print(f"{'='*70}")
    print(f"  {'因子':15s} {'中文名':12s} {'平均IC':>8s} {'IR':>5s} {'正IC%':>6s} {'有效天数':>8s}")
    print(f"  {'─'*55}")
    
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
            continue
        
        ic_arr = np.array(daily_ics)
        mean_ic = np.mean(ic_arr)
        std_ic = np.std(ic_arr)
        ir = mean_ic / std_ic if std_ic > 0 else 0
        pos_pct = sum(1 for ic in daily_ics if ic > 0) / len(daily_ics) * 100
        
        cname = ALPHA_NAMES.get(aname, '')
        icon = '✅' if abs(mean_ic) > 0.03 else ('⚡' if abs(mean_ic) > 0.01 else '⚠️')
        print(f"  {aname:15s} {cname:12s} {mean_ic:+7.4f}{icon} {ir:5.2f} {pos_pct:5.0f}% {len(daily_ics):6d}")
        
        ic_results[aname] = {
            'name': cname,
            'ic': round(mean_ic, 4),
            'ir': round(ir, 2),
            'pos_pct': round(pos_pct, 1),
            'n_days': len(daily_ics),
            'n_pairs': total_pairs
        }
    
    # 按IC绝对值排序
    sorted_ics = sorted(ic_results.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    print(f"\n{'─'*70}")
    print(f"  📊 Top 10 有效Alpha因子")
    print(f"{'─'*70}")
    print(f"  {'因子':15s} {'中文名':12s} {'IC':>8s} {'IR':>5s} {'正IC%':>6s} {'样本':>8s}")
    print(f"  {'─'*55}")
    for aname, r in sorted_ics[:10]:
        icon = '✅' if abs(r['ic']) > 0.03 else ('⚡' if abs(r['ic']) > 0.01 else '⚠️')
        print(f"  {aname:15s} {r['name']:12s} {r['ic']:+7.4f}{icon} {r['ir']:5.2f} {r['pos_pct']:5.0f}% {r['n_pairs']:>8,}")
    
    fp = f'/tmp/alpha191_ic_{time.strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp, 'w') as f:
        json.dump(ic_results, f, indent=2, ensure_ascii=False)
    print(f"\n📁 {fp}")


if __name__ == '__main__':
    import pymysql
    main()
