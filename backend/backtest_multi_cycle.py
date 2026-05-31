#!/usr/bin/env python3
"""
多周期评分回测 v2.0 — 5/10/20/30/60日持有期对比
包含: 原始评分分层 / V型映射 / 子因子贡献度分析
"""
import os, sys, pymysql, math, json
from db_config import get_connection
from datetime import datetime, date
from collections import defaultdict



def evaluate_vmap(all_records, forward_days, center=15):
    """
    V型映射回测: 将评分映射为距中心点的距离
    假设: 极端低分(超跌)和极端高分(趋势加速)都有正向收益
    """
    records = []
    for r in all_records:
        fwd_key = f'fwd_{forward_days}d'
        if fwd_key not in r or r[fwd_key] is None:
            continue
        # V型映射: |score - center|
        v_score = abs(r['score'] - center)
        records.append({
            'v_score': v_score,
            'raw_score': r['score'],
            fwd_key: r[fwd_key],
            'ts_code': r['ts_code'],
            'trade_date': r['trade_date']
        })
    
    if not records:
        return None
    
    # 按V评分排序分层
    records.sort(key=lambda x: x['v_score'])
    n = len(records)
    
    groups = []
    for g in range(5):  # 5组更精简
        start = int(n * g / 5)
        end = int(n * (g+1) / 5)
        group = records[start:end]
        avg_vs = sum(r['v_score'] for r in group) / len(group)
        avg_fwd = sum(r[fwd_key] for r in group) / len(group)
        avg_raw = sum(r['raw_score'] for r in group) / len(group)
        
        # 统计正收益比例
        pos_pct = sum(1 for r in group if r[fwd_key] > 0) / len(group)
        
        groups.append({
            'group': g+1, 'n': len(group),
            'avg_v_score': round(avg_vs, 1),
            'avg_raw_score': round(avg_raw, 1),
            'avg_ret': round(avg_fwd, 6),
            'win_rate': round(pos_pct, 4)
        })
    
    # IC
    vs = [r['v_score'] for r in records]
    fwds = [r[fwd_key] for r in records]
    avg_vs = sum(vs)/n
    avg_f = sum(fwds)/n
    cov = sum((vs[i]-avg_vs)*(fwds[i]-avg_f) for i in range(n))/n
    std_vs = math.sqrt(sum((v-avg_vs)**2 for v in vs)/n)
    std_f = math.sqrt(sum((f-avg_f)**2 for f in fwds)/n)
    ic = cov/(std_vs*std_f) if std_vs>0 and std_f>0 else 0
    
    # 最强信号 (V评分最高组) vs 最弱信号
    spread = groups[-1]['avg_ret'] - groups[0]['avg_ret']
    
    return {
        'center': center,
        'total_records': n,
        'groups': groups,
        'ic': round(ic, 4),
        'top_bottom_spread': spread,
        'avg_ret_all': round(avg_f, 6)
    }

def evaluate_raw(all_records, forward_days):
    """原始评分分层(10组)回测"""
    records = [r for r in all_records if f'fwd_{forward_days}d' in r and r[f'fwd_{forward_days}d'] is not None]
    if not records:
        return None
    
    records.sort(key=lambda x: x['score'])
    n = len(records)
    
    groups = []
    for g in range(10):
        start = int(n * g / 10)
        end = int(n * (g+1) / 10)
        group = records[start:end]
        avg_s = sum(r['score'] for r in group)/len(group)
        avg_f = sum(r[f'fwd_{forward_days}d'] for r in group)/len(group)
        pos_pct = sum(1 for r in group if r[f'fwd_{forward_days}d'] > 0)/len(group)
        groups.append({'group': g+1, 'n': len(group), 'avg_score': round(avg_s,1),
                       'avg_ret': round(avg_f,6), 'win_rate': round(pos_pct,4)})
    
    sc = [r['score'] for r in records]
    fw = [r[f'fwd_{forward_days}d'] for r in records]
    avg_sc = sum(sc)/n
    avg_fw = sum(fw)/n
    cov = sum((sc[i]-avg_sc)*(fw[i]-avg_fw) for i in range(n))/n
    std_sc = math.sqrt(sum((s-avg_sc)**2 for s in sc)/n)
    std_fw = math.sqrt(sum((f-avg_fw)**2 for f in fw)/n)
    ic = cov/(std_sc*std_fw) if std_sc>0 and std_fw>0 else 0
    
    return {
        'total_records': n,
        'groups': groups,
        'ic': round(ic, 4),
        'top_bottom_spread': groups[-1]['avg_ret'] - groups[0]['avg_ret'],
        'avg_ret_all': round(avg_fw, 6)
    }

def main():
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 读取已有的评分数据
    cur.execute("""
        SELECT bsd.ts_code, bsd.trade_date, 
               bsd.total_score, bsd.trend_score, bsd.momentum_score, 
               bsd.wave_score, bsd.volume_score, bsd.close_price
        FROM backtest_score_daily bsd
        JOIN backtest_pool bp ON bsd.ts_code = bp.ts_code
        WHERE bp.status='ACTIVE' AND bp.market != '指数'
        ORDER BY bsd.ts_code, bsd.trade_date
    """)
    rows = cur.fetchall()
    print(f"📋 读取评分数据: {len(rows)}条")
    
    # 按股票分组
    stock_data = defaultdict(list)
    for r in rows:
        stock_data[r['ts_code']].append(r)
    
    print(f"   涉及 {len(stock_data)} 只股票")
    
    # 计算未来N日收益
    forward_periods = [5, 10, 20, 30, 60]
    all_records = []
    
    for ts_code, data in stock_data.items():
        # 按日期排序
        data.sort(key=lambda x: x['trade_date'])
        
        for i, row in enumerate(data):
            record = {
                'ts_code': ts_code,
                'trade_date': row['trade_date'],
                'score': float(row['total_score']),
                'trend': float(row['trend_score']),
                'momentum': float(row['momentum_score']),
                'wave': float(row['wave_score']),
                'volume': float(row['volume_score']),
                'close': float(row['close_price'])
            }
            
            for fwd in forward_periods:
                if i + fwd < len(data):
                    fwd_close = float(data[i+fwd]['close_price'])
                    if record['close'] > 0:
                        record[f'fwd_{fwd}d'] = (fwd_close - record['close']) / record['close']
                else:
                    record[f'fwd_{fwd}d'] = None
            
            all_records.append(record)
    
    print(f"   有效评分记录: {len(all_records)}条\n")
    
    # ═══════════════════════════════════════════
    # 报告输出
    # ═══════════════════════════════════════════
    print("=" * 100)
    print("🧪 多周期评分回测报告 v2.0")
    print(f"   回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   样本范围: {rows[0]['trade_date']} ~ {rows[-1]['trade_date']}")
    print(f"   基准日数量: {len(all_records)}")
    print("=" * 100)
    
    # ─── Part 1: 原始评分分层 ───
    print("\n" + "─" * 100)
    print("📊 Part 1: 原始评分分层回测 (10组)")
    print("─" * 100)
    
    for fwd in forward_periods:
        r = evaluate_raw(all_records, fwd)
        if not r: continue
        
        print(f"\n  ▸ 持有 {fwd:>2d} 日 | n={r['total_records']} | IC={r['ic']:+.4f} | 多空利差={r['top_bottom_spread']*100:+.2f}% | 全样本={r['avg_ret_all']*100:+.2f}%")
        print(f"    {'组别':>6s} {'数量':>6s} {'均分':>7s} {'均收益':>9s} {'胜率':>7s}")
        for g in r['groups']:
            print(f"    {'G'+str(g['group']):>6s} {g['n']:>6d} {g['avg_score']:>7.1f} {g['avg_ret']*100:>8.2f}% {g['win_rate']*100:>6.1f}%")
    
    # ─── Part 2: V型映射回测 (多中心对比) ───
    print("\n" + "─" * 100)
    print("📊 Part 2: V型映射回测 (|score - center|, 5组)")
    print("─" * 100)
    
    # 测试不同中心点
    for center in [10, 15, 20, 25]:
        print(f"\n  ▸▸▸ V型中心 = {center} ▸▸▸")
        for fwd in forward_periods:
            r = evaluate_vmap(all_records, fwd, center)
            if not r: continue
            print(f"    ★ 持有 {fwd:>2d} 日 | n={r['total_records']} | IC={r['ic']:+.4f} | 极端-温和利差={r['top_bottom_spread']*100:+.2f}% | 真实均值={r['avg_ret_all']*100:+.2f}%")
            print(f"       {'组别':>6s} {'数量':>6s} {'V评分':>7s} {'原始均分':>9s} {'均收益':>9s} {'胜率':>7s}")
            for g in r['groups']:
                print(f"       {'V'+str(g['group']):>6s} {g['n']:>6d} {g['avg_v_score']:>7.1f} {g['avg_raw_score']:>9.1f} {g['avg_ret']*100:>8.2f}% {g['win_rate']*100:>6.1f}%")
    
    # ─── Part 3: 综合对比矩阵 ───
    print("\n" + "═" * 100)
    print("📊 Part 3: 多周期综合对比矩阵")
    print("═" * 100)
    
    header = f"  {'方法':<20s}"
    for fwd in forward_periods:
        header += f"{fwd}日利差{'':>7s}"
    header += f"{'最佳周期':>10s}"
    print(header)
    print("  " + "─" * 98)
    
    # 原始评分
    row = f"  {'原始评分(多空)':<20s}"
    best_fwd = None
    best_val = -999
    vals = {}
    for fwd in forward_periods:
        r = evaluate_raw(all_records, fwd)
        spread = r['top_bottom_spread']*100 if r else 0
        vals[fwd] = spread
        row += f"{spread:>+8.2f}%{'':>6s}"
        if spread > best_val:
            best_val = spread
            best_fwd = fwd
    row += f"{f'{best_fwd}日:{best_val:+.2f}%':>10s}"
    print(row)
    
    # V型各中心点
    for center in [10, 15, 20, 25]:
        row = f"  {'V型映射(c='+str(center)+')':<20s}"
        best_fwd = None
        best_val = -999
        for fwd in forward_periods:
            r = evaluate_vmap(all_records, fwd, center)
            spread = r['top_bottom_spread']*100 if r else 0
            row += f"{spread:>+8.2f}%{'':>6s}"
            if spread > best_val:
                best_val = spread
                best_fwd = fwd
        row += f"{f'{best_fwd}日:{best_val:+.2f}%':>10s}"
        print(row)
    
    # ─── Part 4: 子因子贡献 —— 趋势/动量/波动/量能分维度 ───
    print("\n" + "═" * 100)
    print("📊 Part 4: 子因子独立贡献度 (原始分层, 10组)")
    print("═" * 100)
    
    sub_factors = [
        ('trend', '趋势因子'),
        ('momentum', '动量因子'), 
        ('wave', '波动因子'),
        ('volume', '量能因子')
    ]
    
    for fkey, fname in sub_factors:
        print(f"\n  ▸ {fname}")
        # 按该子因子排序分层
        valid = [r for r in all_records if f'fwd_{10}d' in r and r[f'fwd_{10}d'] is not None]
        valid.sort(key=lambda x: x[fkey])
        n = len(valid)
        
        print(f"    n={n} | {'组别':>6s} {'均因子分':>9s} {'10日收益':>9s} {'胜率':>7s}")
        for g in range(5):
            start = int(n*g/5)
            end = int(n*(g+1)/5)
            gp = valid[start:end]
            avg_f = sum(r[fkey] for r in gp)/len(gp)
            avg_r = sum(r['fwd_10d'] for r in gp)/len(gp)
            wr = sum(1 for r in gp if r['fwd_10d']>0)/len(gp)
            print(f"    {'Q'+str(g+1):>6s} {avg_f:>9.1f} {avg_r*100:>8.2f}% {wr*100:>6.1f}%")
        
        # 多空利差
        hf = valid[int(n*0.8):]
        lf = valid[:int(n*0.2)]
        avg_h = sum(r['fwd_10d'] for r in hf)/len(hf)
        avg_l = sum(r['fwd_10d'] for r in lf)/len(lf)
        print(f"    多空利差(10日): {(avg_h-avg_l)*100:+.2f}%")
    
    cur.close()
    conn.close()
    
    print(f"\n{'='*100}")
    print("✅ 多周期回测完成")

if __name__ == '__main__':
    main()
