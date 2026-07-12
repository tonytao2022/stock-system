#!/usr/bin/env python3
"""
V13.2 评分因子IC验证 — 买入线73
==================================
信息系数(IC) = composite_score 与 未来收益的 RankIC
IC均值 > 0.03 认为有效
"""

import os, sys, json, math, time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from scipy.stats import spearmanr
sys.path.insert(0, '/opt/stock-analyzer')
import db_config

START = '2024-01-02'
END = '2026-07-10'
BUY_MIN = 73
HOLD_DAYS = [1, 3, 5, 10, 20]


def main():
    pwd = db_config._get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2',
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()

    print(f"⏳ 取评分数据...", end='', flush=True)
    cur.execute("""
        SELECT ts_code, trade_date, composite_score
        FROM strategy_signal
        WHERE trade_date>=%s AND trade_date<=%s AND composite_score >= 0
    """, (START, END))
    rows = cur.fetchall()
    print(f" {len(rows)}条")

    # 按日期+股票构建
    scores = {}
    dates = set()
    for r in rows:
        td = r['trade_date'].strftime('%Y-%m-%d')
        dates.add(td)
        scores[(r['ts_code'], td)] = float(r['composite_score'])

    # 取所有股票代码
    cur.execute("SELECT ts_code FROM backtest_pool")
    codes = [r['ts_code'] for r in cur.fetchall()]
    code_set = set(codes)

    print(f"⏳ 取K线计算未来收益...", end='', flush=True)
    # 取K线 close
    ph = ','.join(['%s']*len(codes))
    cur.execute(f"""
        SELECT ts_code, trade_date, `close`
        FROM daily_kline
        WHERE ts_code IN ({ph}) AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date, ts_code
    """, (*codes, START, END))
    closes = {}
    kline_dates = set()
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d')
        kline_dates.add(td)
        closes[(r['ts_code'], td)] = float(r['close'])
    conn.close()

    alld = sorted(dates & kline_dates)
    print(f" {len(closes)}条/{len(alld)}天")

    # 计算每个交易日+每只股票的次日回报
    # 用日期索引来映射
    date_idx = {d: i for i, d in enumerate(alld)}

    print(f"\n{'='*65}")
    print(f"  📊 评分因子 IC 验证（买入线≥{BUY_MIN}）")
    print(f"  {START} ~ {END} | {len(codes)}只 | {len(alld)}天")
    print(f"{'='*65}")

    for hd in HOLD_DAYS:
        ic_list = []
        ic_pos = []
        ic_neg = []
        bin_records = {k: [] for k in ['≥73_正收益','≥73_负收益','<73_正收益','<73_负收益']}
        hit_rates_above = []
        hit_rates_below = []

        for di, dt in enumerate(alld):
            if di + hd >= len(alld):
                continue
            target_dt = alld[di + hd]

            # 今天有评分且目标日有close的股票
            today_scores = {}
            future_returns = {}

            for code in codes:
                key_today = (code, dt)
                key_future = (code, target_dt)
                if key_today in scores and key_future in closes:
                    today_close = closes.get((code, dt), None)
                    if today_close is None or today_close == 0:
                        continue
                    sc = scores[key_today]
                    fc = closes[key_future]
                    ret = (fc / today_close - 1) * 100
                    today_scores[code] = sc
                    future_returns[code] = ret

            if len(today_scores) < 30:
                continue

            # RankIC
            score_list = [today_scores[c] for c in today_scores]
            ret_list = [future_returns[c] for c in today_scores]
            rho, pv = spearmanr(score_list, ret_list)
            ic_list.append(rho)
            if rho > 0: ic_pos.append(rho)
            else: ic_neg.append(rho)

            # 按买入线分组的命中率
            above = [future_returns[c] for c in today_scores if today_scores[c] >= BUY_MIN]
            below = [future_returns[c] for c in today_scores if today_scores[c] < BUY_MIN]
            if above:
                hit_rates_above.append(sum(1 for r in above if r > 0) / len(above) * 100)
                bin_records['≥73_正收益'].extend([r for r in above if r > 0])
                bin_records['≥73_负收益'].extend([r for r in above if r <= 0])
            if below:
                hit_rates_below.append(sum(1 for r in below if r > 0) / len(below) * 100)
                bin_records['<73_正收益'].extend([r for r in below if r > 0])
                bin_records['<73_负收益'].extend([r for r in below if r <= 0])

        # 统计
        ic_arr = np.array(ic_list)
        mean_ic = ic_arr.mean()
        std_ic = ic_arr.std()
        ir = mean_ic / std_ic if std_ic > 0 else 0  # 信息比率
        pos_ratio = len(ic_pos) / len(ic_list) * 100 if ic_list else 0
        cum_ic = ic_arr.cumsum()

        # 命中率
        avg_hit_above = np.mean(hit_rates_above) if hit_rates_above else 0
        avg_hit_below = np.mean(hit_rates_below) if hit_rates_below else 0
        above_pos = len(bin_records['≥73_正收益'])
        above_neg = len(bin_records['≥73_负收益'])
        below_pos = len(bin_records['<73_正收益'])
        below_neg = len(bin_records['<73_负收益'])
        above_hit = above_pos / (above_pos + above_neg) * 100 if (above_pos + above_neg) > 0 else 0
        below_hit = below_pos / (below_pos + below_neg) * 100 if (below_pos + below_neg) > 0 else 0

        # 分组收益
        above_avg_ret = np.mean(bin_records['≥73_正收益'] + bin_records['≥73_负收益']) if (above_pos+above_neg) > 0 else 0
        below_avg_ret = np.mean(bin_records['<73_正收益'] + bin_records['<73_负收益']) if (below_pos+below_neg) > 0 else 0

        print(f"\n{'─'*60}")
        print(f"  📈 持有{hd}日 RankIC")
        print(f"{'─'*60}")
        print(f"  均值IC: {mean_ic:+.4f} {'✅' if abs(mean_ic) > 0.03 else '❌'}")
        print(f"  标准差: {std_ic:.4f} | IR: {ir:.2f}")
        print(f"  正IC占比: {pos_ratio:.1f}% (共{len(ic_list)}个交易日)")
        print(f"  累计IC: {cum_ic[-1]:+.2f}")
        print(f"\n  🎯 买入线≥{BUY_MIN} 分组表现:")
        print(f"  ≥{BUY_MIN}分: {above_pos+above_neg:,}条 | 涨幅均值{above_avg_ret:+.2f}% | 命中率{above_hit:.1f}%")
        print(f"  <{BUY_MIN}分: {below_pos+below_neg:,}条 | 涨幅均值{below_avg_ret:+.2f}% | 命中率{below_hit:.1f}%")
        print(f"  利差(≥ - <): {above_avg_ret - below_avg_ret:+.2f}% {'✅' if (above_avg_ret - below_avg_ret) > 0 else '⚠️'}")

    # 额外: 看评分分档的表
    print(f"\n{'─'*60}")
    print(f"  📊 评分分布统计")
    print(f"{'─'*60}")
    all_scores = list(scores.values())
    print(f"  全量评分: {len(all_scores):,}条")
    print(f"  均值: {np.mean(all_scores):.1f} | 中位: {np.median(all_scores):.1f} | 标准差: {np.std(all_scores):.1f}")
    print(f"  P25: {np.percentile(all_scores,25):.1f} | P50: {np.percentile(all_scores,50):.1f} | P75: {np.percentile(all_scores,75):.1f}")
    print(f"  P90: {np.percentile(all_scores,90):.1f} | P95: {np.percentile(all_scores,95):.1f}")
    print(f"  ≥73占比: {sum(1 for s in all_scores if s>=73)/len(all_scores)*100:.1f}%")
    print(f"  ≥70占比: {sum(1 for s in all_scores if s>=70)/len(all_scores)*100:.1f}%")
    print(f"  ≥80占比: {sum(1 for s in all_scores if s>=80)/len(all_scores)*100:.1f}%")


if __name__ == '__main__':
    import pymysql
    main()
