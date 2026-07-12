#!/usr/bin/env python3
"""
V13.2 因子归因分析 — 逐因子IC验证
==================================
数据源: strategy_signal 表已有字段
核心因子:
  - trend_score    - 缠论趋势分
  - momentum_score - 动量因子
  - structure_score- 结构分
  - mf_score       - 资金因子
  - pos_score      - 位置/阶段评分
  - emotion_score  - 情绪因子（数据极少，跳过）
  - composite_score- 综合评分（对照）
"""

import os, sys, json, math, time, pymysql
from datetime import datetime
import pandas as pd
import numpy as np
from scipy.stats import spearmanr
sys.path.insert(0, '/opt/stock-analyzer')
import db_config

START = '2024-01-02'
END = '2026-07-10'
HOLD_DAYS = [1, 3, 5, 10, 20]
FACTORS = ['trend_score', 'momentum_score', 'structure_score',
           'mf_score', 'pos_score', 'composite_score']


def calc_ic(factor_name, factor_dict, closes, codes, alld, hd):
    """计算单个因子在指定持有期的IC"""
    ic_list = []
    above_ret = []; below_ret = []
    above_hit = []; below_hit = []
    n_above = 0; n_below = 0

    for di, dt in enumerate(alld):
        if di + hd >= len(alld): continue
        target_dt = alld[di + hd]

        vals = {}; rets = {}
        for code in codes:
            ft = (code, dt)
            if ft not in factor_dict: continue
            ff = (code, target_dt)
            if ff not in closes: continue
            today_c = closes.get((code, dt))
            if today_c is None or today_c == 0: continue
            val = factor_dict[ft]
            if val is None or math.isnan(val): continue
            ret = (closes[ff] / today_c - 1) * 100
            vals[code] = val
            rets[code] = ret

        if len(vals) < 20: continue

        # RankIC
        vlist = [vals[c] for c in vals]; rlist = [rets[c] for c in vals]
        try:
            rho, _ = spearmanr(vlist, rlist)
        except:
            continue
        if not math.isnan(rho):
            ic_list.append(rho)

        # 中位数分组
        median_v = np.median(vlist)
        above = [rets[c] for c in vals if vals[c] >= median_v and c in rets]
        below = [rets[c] for c in vals if vals[c] < median_v and c in rets]
        if above:
            above_ret.extend(above)
            above_hit.append(sum(1 for r in above if r > 0) / len(above) * 100)
            n_above += len(above)
        if below:
            below_ret.extend(below)
            below_hit.append(sum(1 for r in below if r > 0) / len(below) * 100)
            n_below += len(below)

    if not ic_list: return None

    ic_arr = np.array(ic_list)
    mean_ic = np.mean(ic_arr)
    std_ic = np.std(ic_arr)
    cum_ic = ic_arr[-1] if len(ic_arr) == 1 else ic_arr.sum()

    return {
        'n_days': len(ic_list),
        'ic': round(mean_ic, 4),
        'ic_std': round(std_ic, 4),
        'ir': round(mean_ic / std_ic, 2) if std_ic > 0 else 0,
        'pos_pct': round(sum(1 for ic in ic_list if ic > 0) / len(ic_list) * 100, 1),
        'cum_ic': round(cum_ic, 2),
        'spread': round(np.mean(above_ret) - np.mean(below_ret), 2) if above_ret and below_ret else 0,
        'hit_above': round(np.mean(above_hit), 1) if above_hit else 0,
        'hit_below': round(np.mean(below_hit), 1) if below_hit else 0,
        'n_above': n_above,
        'n_below': n_below
    }


def print_ic_table(factor_name, factor_dict, closes, codes, alld):
    """打印单个因子的完整IC表"""
    print(f"\n{'─'*65}")
    print(f"  🔬 {factor_name} ({len(factor_dict):,}条)")
    print(f"{'─'*65}")

    rows = []
    for hd in HOLD_DAYS:
        res = calc_ic(factor_name, factor_dict, closes, codes, alld, hd)
        if res is None: continue
        rows.append((hd, res))
        icon = '✅' if res['ic'] > 0.03 else ('⚡' if res['ic'] > 0.01 else '⚠️')
        print(f"  📈 H{hd:2d} | IC: {res['ic']:+.4f} {icon} | IR: {res['ir']:.2f} | 正IC: {res['pos_pct']:.0f}%")
        print(f"         高分: +{res['hit_above']:.1f}%/均值{np.mean(above_ret) if ... else 0:+.2f}% | 低分: +{res['hit_below']:.1f}%/均值{np.mean(below_ret) if ... else 0:+.2f}%")
        print(f"         利差: {res['spread']:+.2f}% | 累计IC: {res['cum_ic']:+.2f}")

    # 简化版打印
    print(f"  {'持有':>4s} {'IC':>8s} {'IR':>5s} {'正IC':>5s} {'命中_高':>7s} {'命中_低':>7s} {'利差':>8s} {'累计IC':>7s}")
    print(f"  {'─'*54}")
    for hd, res in rows:
        print(f"  H{hd:3d} {res['ic']:+7.4f} {res['ir']:5.2f} {res['pos_pct']:4.0f}% {res['hit_above']:6.1f}% {res['hit_below']:6.1f}% {res['spread']:+7.2f}% {res['cum_ic']:+7.2f}")

    return {r['hd']: r for r in rows}


def main():
    pwd = db_config._get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2',
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    cur.execute("SET SESSION group_concat_max_len = 100000")

    print(f"⏳ 取因子数据 from strategy_signal...", end='', flush=True)
    fnames = ','.join(FACTORS)
    cur.execute(f"""
        SELECT ts_code, trade_date, {fnames}
        FROM strategy_signal
        WHERE trade_date>=%s AND trade_date<=%s
    """, (START, END))
    raw = cur.fetchall()
    print(f" {len(raw)}条")

    # 构建因子字典 {code+date: value}
    factor_maps = {f: {} for f in FACTORS}
    dates = set()
    for r in raw:
        td = r['trade_date'].strftime('%Y-%m-%d')
        dates.add(td)
        for f in FACTORS:
            v = r.get(f)
            if v is not None:
                factor_maps[f][(r['ts_code'], td)] = float(v)

    # 取backtest_pool的股票列表
    cur.execute("SELECT ts_code FROM backtest_pool")
    codes = [r['ts_code'] for r in cur.fetchall()]

    # 取K线close
    ph = ','.join(['%s']*len(codes))
    cur.execute(f"""
        SELECT ts_code, trade_date, `close`
        FROM daily_kline
        WHERE ts_code IN ({ph}) AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date, ts_code
    """, (*codes, START, END))
    closes = {}; kdset = set()
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d')
        kdset.add(td)
        closes[(r['ts_code'], td)] = float(r['close'])
    conn.close()

    alld = sorted(dates & kdset)
    n_stocks = len(codes)
    print(f"  {n_stocks}只股票, {len(alld)}个交易日")

    # ──────── 逐个因子打印 ────────
    all_results = {}

    for fn in FACTORS:
        fd = factor_maps[fn]
        if len(fd) < 1000:
            print(f"\n  ⏭️  {fn}: 数据不足({len(fd)}条)")
            continue

        print(f"\n{'='*65}")
        print(f"  🔬 因子: {fn}")
        vals = list(fd.values())
        print(f"  均值: {np.mean(vals):.1f} | 中位: {np.median(vals):.1f} | 标准差: {np.std(vals):.1f}")
        print(f"  P25: {np.percentile(vals,25):.1f} | P75: {np.percentile(vals,75):.1f}")

        print(f"  {'持有':>4s} {'IC':>8s} {'IR':>5s} {'正IC':>5s} {'命中_高':>7s} {'命中_低':>7s} {'利差':>8s} {'累计IC':>7s}")
        print(f"  {'─'*54}")

        fn_res = {}
        for hd in HOLD_DAYS:
            res = calc_ic(fn, fd, closes, codes, alld, hd)
            if res is None: continue
            fn_res[f'H{hd}'] = res
            icon = '✅' if abs(res['ic']) > 0.03 else ('⚡' if abs(res['ic']) > 0.01 else '⚠️')
            print(f"  H{hd:3d} {res['ic']:+7.4f}{icon} {res['ir']:5.2f} {res['pos_pct']:4.0f}% {res['hit_above']:6.1f}% {res['hit_below']:6.1f}% {res['spread']:+7.2f}% {res['cum_ic']:+7.2f}")

        all_results[fn] = fn_res

    # ──────── 汇总表格 ────────
    print(f"\n\n{'='*70}")
    print(f"  📊 因子归因汇总")
    print(f"{'='*70}")
    print(f"  {'因子':20s} | {'H1 IC':>7s} {'H1利差':>7s} | {'H5 IC':>7s} {'H5利差':>7s} | {'H10IC':>7s} {'H10利差':>7s} | {'H20IC':>7s} {'H20利差':>8s}")
    print(f"{'─'*83}")
    for fn in FACTORS:
        r = all_results.get(fn, {})
        h1 = r.get('H1', {}); h5 = r.get('H5', {}); h10 = r.get('H10', {}); h20 = r.get('H20', {})
        print(f"  {fn:20s} | "
              f"{h1.get('ic',0):+6.4f}{'✅' if abs(h1.get('ic',0))>0.03 else '⚡' if abs(h1.get('ic',0))>0.01 else '⚠️'} {h1.get('spread',0):+6.2f}% | "
              f"{h5.get('ic',0):+6.4f}{'✅' if abs(h5.get('ic',0))>0.03 else '⚡' if abs(h5.get('ic',0))>0.01 else '⚠️'} {h5.get('spread',0):+6.2f}% | "
              f"{h10.get('ic',0):+6.4f}{'✅' if abs(h10.get('ic',0))>0.03 else '⚡' if abs(h10.get('ic',0))>0.01 else '⚠️'} {h10.get('spread',0):+6.2f}% | "
              f"{h20.get('ic',0):+6.4f}{'✅' if abs(h20.get('ic',0))>0.03 else '⚡' if abs(h20.get('ic',0))>0.01 else '⚠️'} {h20.get('spread',0):+7.2f}%")

    fp = f'/tmp/factor_ic_analysis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp,'w') as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\n📁 {fp}")


if __name__ == '__main__':
    main()
