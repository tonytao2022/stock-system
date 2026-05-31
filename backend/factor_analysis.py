#!/usr/bin/env python3
"""
因子有效性分析 v1.0
==================
检验缠论信号(买卖点/背驰) 与 资金流向(大单净流入) 的因子有效性

核心问题:
  1. 缠论看多信号(buy3/buy2/底背驰) 出现后10日胜率是否 >55%?
  2. 资金净流入 + 缠论看多 的共振信号胜率是否显著更高?
  3. 单独使用资金流向因子的预测能力如何?
  4. 最有效的因子组合是什么?
"""
import os, sys, time, pymysql, json
from db_config import db_cursor, get_connection
from datetime import datetime
from collections import defaultdict

def get_pass():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for l in f:
                if 'password' in l: return l.strip().split('=')[-1].strip().strip('"').strip("'")
    except: pass
    return ''

DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':get_pass(),
      'database':'stock_db','charset':'utf8mb4'}

def main():
    conn = pymysql.connect(**DB)
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 从 chanlun_structure 读取所有历史缠论信号
    cur.execute("""
        SELECT cs.ts_code, cs.trade_date, cs.buy_sell_point, cs.beichi_type, cs.beichi_strength,
               cs.structure_score, cs.zoushi_type, cs.autumn_tiger
        FROM chanlun_structure cs
        ORDER BY cs.ts_code, cs.trade_date
    """)
    chanlun_rows = cur.fetchall()
    print(f"📋 读取缠论数据: {len(chanlun_rows)}条")

    # 按股票分组
    stock_data = defaultdict(list)
    for r in chanlun_rows:
        stock_data[r['ts_code']].append(r)

    print(f"   涉及 {len(stock_data)} 只股票\n")

    # 获取K线数据（用于计算未来N日收益）
    stock_klines = {}
    for code in stock_data:
        cur.execute(
            "SELECT trade_date, close FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",
            (code,)
        )
        klines = cur.fetchall()
        if len(klines) > 60:
            stock_klines[code] = [(str(r['trade_date']), float(r['close'])) for r in klines]

    # 获取资金流向数据（最近可用的）
    money_flow_data = {}
    cur.execute("SELECT ts_code, trade_date, net_mf_amount, buy_lg_amount-sell_lg_amount AS lg_net "
                "FROM money_flow ORDER BY ts_code, trade_date")
    for r in cur.fetchall():
        code = r['ts_code']
        if code not in money_flow_data:
            money_flow_data[code] = {}
        money_flow_data[code][str(r['trade_date'])] = {
            'net': float(r['net_mf_amount'] or 0),
            'lg_net': float(r['lg_net'] or 0),
        }

    cur.close(); conn.close()

    # ═══════════════════════════════════════════
    # 因子有效性检验
    # ═══════════════════════════════════════════

    # 定义信号组
    signal_groups = {
        '缠论三买': lambda r: r['buy_sell_point'] == 'buy3',
        '缠论任意买': lambda r: r['buy_sell_point'] in ('buy1','buy2','buy3'),
        '缠论底背驰': lambda r: r['beichi_type'] == 'bottom' and float(r['beichi_strength'] or 0) > 30,
        '缠论顶背驰': lambda r: r['beichi_type'] == 'top' and float(r['beichi_strength'] or 0) > 30,
        '结构评分高(≥75)': lambda r: float(r['structure_score'] or 0) >= 75,
        '结构评分低(<35)': lambda r: float(r['structure_score'] or 0) < 35,
        '秋老虎': lambda r: int(r['autumn_tiger'] or 0) == 1,
    }

    forward_periods = [5, 10, 20]
    results = {}

    for group_name, test_fn in signal_groups.items():
        for fwd in forward_periods:
            wins = 0; total = 0; returns = []

            for code, signals in stock_data.items():
                if code not in stock_klines:
                    continue
                klines = stock_klines[code]
                closes = [k[1] for k in klines]
                date_map = {k[0]: i for i, k in enumerate(klines)}

                for sig in signals:
                    if not test_fn(sig):
                        continue
                    sig_date = str(sig['trade_date'])
                    if sig_date not in date_map:
                        continue
                    idx = date_map[sig_date]
                    if idx + fwd >= len(closes):
                        continue
                    fwd_ret = (closes[idx + fwd] - closes[idx]) / closes[idx]
                    returns.append(fwd_ret)
                    if fwd_ret > 0:
                        wins += 1
                    total += 1

            key = f'{group_name}|{fwd}日'
            if total >= 5:  # 最少5个样本
                avg_ret = sum(returns) / total * 100
                win_rate = wins / total * 100
                results[key] = {
                    'group': group_name,
                    'days': fwd,
                    'samples': total,
                    'win_rate': round(win_rate, 1),
                    'avg_return': round(avg_ret, 2),
                }

    # 输出结果
    print("=" * 90)
    print("📊 因子有效性分析报告")
    print(f"   生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   缠论样本: {len(chanlun_rows)}条 | 资金流向: {sum(len(v) for v in money_flow_data.values())}条")
    print("=" * 90)

    # 按因子分组
    current_group = ''
    print(f"\n{'─'*90}")
    for key in sorted(results.keys(), key=lambda k: (k.split('|')[0], int(k.split('|')[1].replace('日','')))):
        r = results[key]
        group = r['group']
        if group != current_group:
            print(f"\n  📈 {group}")
            print(f"  {'周期':>6s} {'样本':>6s} {'胜率':>8s} {'均收益':>10s} {'评级':>10s}")
            current_group = group
        rating = '🟢有效' if r['win_rate'] >= 55 else ('🟡参考' if r['win_rate'] >= 50 else '🔴无效')
        print(f"  {r['days']:>4d}日 {r['samples']:>6d} {r['win_rate']:>6.1f}% {r['avg_return']:>+8.2f}% {rating:>10s}")

    # ═══ 资金流向 + 缠论共振检验 ═══
    print(f"\n{'═'*90}")
    print("📊 资金流向 vs 缠论 共振分析")
    print(f"{'═'*90}")

    # 只对有资金流向数据的最近日期做检验
    resonance_results = {}
    for fwd in forward_periods:
        long_only_win = 0; long_only_total = 0
        mf_only_win = 0; mf_only_total = 0
        both_win = 0; both_total = 0
        neither_win = 0; neither_total = 0

        for code, signals in stock_data.items():
            if code not in stock_klines or code not in money_flow_data:
                continue
            klines = stock_klines[code]
            closes = [k[1] for k in klines]
            date_map = {k[0]: i for i, k in enumerate(klines)}
            mf = money_flow_data[code]

            for sig in signals:
                sig_date = str(sig['trade_date'])
                if sig_date not in date_map:
                    continue
                idx = date_map[sig_date]
                if idx + fwd >= len(closes):
                    continue

                has_chanlun_buy = sig['buy_sell_point'] in ('buy1','buy2','buy3')
                has_money_flow = mf.get(sig_date, {}).get('net', 0) > 1000  # 净流入>1000万

                fwd_ret = (closes[idx + fwd] - closes[idx]) / closes[idx]
                is_win = fwd_ret > 0

                if has_chanlun_buy and has_money_flow:
                    both_total += 1
                    if is_win: both_win += 1
                elif has_chanlun_buy and not has_money_flow:
                    long_only_total += 1
                    if is_win: long_only_win += 1
                elif not has_chanlun_buy and has_money_flow:
                    mf_only_total += 1
                    if is_win: mf_only_win += 1
                else:
                    neither_total += 1
                    if is_win: neither_win += 1

        for label, w, t in [('仅缠论看多', long_only_win, long_only_total),
                              ('仅资金净流入', mf_only_win, mf_only_total),
                              ('两者共振', both_win, both_total),
                              ('均无', neither_win, neither_total)]:
            if t >= 5:
                key = f'{label}|{fwd}日'
                resonance_results[key] = {
                    'group': label, 'days': fwd,
                    'samples': t, 'win_rate': round(w/t*100, 1) if t > 0 else 0,
                }

    if resonance_results:
        print(f"  {'信号组合':>16s} {'周期':>6s} {'样本':>6s} {'胜率':>8s} {'评级':>12s}")
        prev_group = ''
        for key in sorted(resonance_results.keys(), key=lambda k: (k.split('|')[0], int(k.split('|')[1].replace('日','')))):
            r = resonance_results[key]
            if r['group'] != prev_group:
                prev_group = r['group']
            rating = '🟢强有效' if r['win_rate'] >= 60 else ('🟡可用' if r['win_rate'] >= 50 else '🔴无效')
            print(f"  {r['group']:>16s} {r['days']:>4d}日 {r['samples']:>6d} {r['win_rate']:>6.1f}% {rating:>12s}")

    # ═══ 结论 ═══
    print(f"\n{'═'*90}")
    print("📋 结论与建议")
    print(f"{'═'*90}")

    best_signals = sorted(results.values(), key=lambda r: r['win_rate'], reverse=True)[:5]
    print(f"\n🏆 最有效因子 Top 5 (按胜率排序):")
    for i, r in enumerate(best_signals[:5]):
        print(f"  {i+1}. {r['group']} ({r['days']}日) — 胜率{r['win_rate']}% 均收益{r['avg_return']:+.2f}% 样本{r['samples']}")

    print(f"\n💡 开发建议:")
    print(f"  - 如果因子胜率>55%: 可直接用于信号增强(评分+5~10分)")
    print(f"  - 如果因子胜率<50%: 该因子在当前市场无效, 不纳入评分")
    print(f"  - 共振>60%: 说明缠论+资金双因子共振是有效的超额信号")

if __name__ == '__main__':
    t0 = time.time()
    main()
    print(f"\n⏱ 耗时: {time.time()-t0:.1f}s")
