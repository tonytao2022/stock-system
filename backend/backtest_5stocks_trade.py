#!/usr/bin/env python3
"""
5只股票 趋势回测 + 信号交易模拟
基于评分引擎 v2.0 前复权数据, 模拟买入/卖出操作
"""
import pymysql, sys, math, os
from db_config import get_connection
from collections import defaultdict


conn = get_connection()
cur = conn.cursor(pymysql.cursors.DictCursor)

sys.path.insert(0, '.')
from score_engine import score_trend, score_momentum, score_volatility, score_volume, vmap

stocks = {
    '002050.SZ': '三花智控',
    '002185.SZ': '华天科技',
    '300476.SZ': '胜宏科技',
    '688036.SH': '传音控股',
    '301377.SZ': '鼎泰高科',
}

print("=" * 120)
print("🧪 5只股票 趋势回测 + 买入卖出信号模拟 — 2024-01 至 2026-05-25")
print("   策略: V型评分>45且趋势分>=90 → 买入, V型评分<25且趋势下降 → 卖出")
print("=" * 120)

for code, name in stocks.items():
    cur.execute("""
        SELECT trade_date, open, close, change_pct, vol 
        FROM daily_kline_qfq WHERE ts_code=%s 
        ORDER BY trade_date ASC
    """, (code,))
    rows = cur.fetchall()
    
    if len(rows) < 200: continue
    
    closes = [float(r['close']) for r in rows]
    opens = [float(r['open']) for r in rows]
    vols = [float(r.get('vol',0) or 0) for r in rows]
    dates = [str(r['trade_date']) for r in rows]
    chgs = [float(r.get('change_pct') or 0) for r in rows]
    
    # 逐日评分
    daily_signals = []
    for i in range(200, len(rows)):
        c = closes[:i+1]
        v = vols[:i+1]
        tr = score_trend(c)
        mo = score_momentum(c, v)
        vl = score_volatility(c)
        vo = score_volume(v, c)
        raw = tr*0.30 + mo*0.30 + vl*0.20 + vo*0.20
        vs = vmap(raw, 25)
        
        signal = 'HOLD'
        if vs >= 45 and tr >= 90:
            signal = 'BUY'
        elif vs >= 50 and tr >= 95:
            signal = 'STRONG_BUY'
        elif vs < 20 and tr < 40:
            signal = 'SELL'
        elif tr < 25:
            signal = 'STRONG_SELL'
        
        daily_signals.append({
            'idx': i, 'date': dates[i],
            'open': opens[i], 'close': closes[i],
            'raw': raw, 'v': vs, 'tr': tr, 'mo': mo, 'vl': vl, 'vo': vo,
            'signal': signal, 'chg': chgs[i],
        })
    
    # ─── 交易模拟 ───
    # 规则: 买入信号出现 → 以次日开盘价买入
    #       卖出信号出现 → 以次日开盘价卖出
    #       无信号 → 持有(若有仓) 或空仓
    position = 0       # 0=空仓, 1=持有
    buy_price = 0
    buy_date = ''
    trades = []
    total_return = 0
    hold_bars = 0
    
    for j in range(len(daily_signals)):
        s = daily_signals[j]
        
        if position == 0 and s['signal'] in ('BUY', 'STRONG_BUY'):
            # 买入: 次日开盘价
            if j + 1 < len(daily_signals):
                buy_price = daily_signals[j+1]['open']
                buy_date = daily_signals[j+1]['date']
                position = 1
                hold_bars = 0
            else:
                buy_price = s['close']
                buy_date = s['date']
                position = 1
                hold_bars = 0
        
        elif position == 1:
            hold_bars += 1
            if s['signal'] in ('SELL', 'STRONG_SELL'):
                # 卖出: 次日开盘价
                sell_price = daily_signals[j+1]['open'] if j+1 < len(daily_signals) else s['close']
                sell_date = daily_signals[j+1]['date'] if j+1 < len(daily_signals) else s['date']
                ret = (sell_price - buy_price) / buy_price * 100
                trades.append({
                    'buy_date': buy_date, 'buy_price': buy_price,
                    'sell_date': sell_date, 'sell_price': sell_price,
                    'return': ret, 'hold_bars': hold_bars,
                })
                total_return += ret
                position = 0
                buy_price = 0
    
    # 如果仍持有, 以最新价浮动盈亏
    if position == 1 and buy_price > 0:
        last_close = closes[-1]
        unrealized = (last_close - buy_price) / buy_price * 100
    else:
        unrealized = 0
    
    # ─── 趋势分析 ───
    # 分阶段统计
    total_bars = len(daily_signals)
    buy_count = sum(1 for s in daily_signals if s['signal'] in ('BUY','STRONG_BUY'))
    sell_count = sum(1 for s in daily_signals if s['signal'] in ('SELL','STRONG_SELL'))
    hold_count = total_bars - buy_count - sell_count
    avg_v = sum(s['v'] for s in daily_signals) / total_bars
    avg_tr = sum(s['tr'] for s in daily_signals) / total_bars
    
    # 收益统计
    all_returns = [t['return'] for t in trades]
    win_trades = [r for r in all_returns if r > 0]
    loss_trades = [r for r in all_returns if r <= 0]
    
    print(f"\n{'─'*120}")
    print(f"  📊 {code} {name}")
    print(f"     前复权: {min(closes):.2f} ~ {max(closes):.2f} | 期间涨跌: {(closes[-1]-closes[0])/closes[0]*100:+.1f}%")
    print(f"  {'─'*120}")
    
    print(f"\n  ▸▸ 信号统计 ({total_bars}个交易日)")
    print(f"     买入信号: {buy_count}次 ({buy_count/total_bars*100:.1f}%)")
    print(f"     卖出信号: {sell_count}次 ({sell_count/total_bars*100:.1f}%)")
    print(f"     持有/观望: {hold_count}次 ({hold_count/total_bars*100:.1f}%)")
    print(f"     平均V分: {avg_v:.1f} | 平均趋势: {avg_tr:.1f}")
    
    print(f"\n  ▸▸ 交易记录 ({len(trades)}笔)")
    if trades:
        print(f"     {'买入日':>12s} {'买入价':>10s} {'卖出日':>12s} {'卖出价':>10s} {'收益':>8s} {'持有时':>6s}")
        print(f"     {'─'*65}")
        for t in trades:
            em = '🟢' if t['return'] > 0 else '🔴'
            print(f"     {t['buy_date']:>12s} {t['buy_price']:>10.2f} {t['sell_date']:>12s} {t['sell_price']:>10.2f} {em}{t['return']:>+6.2f}% {t['hold_bars']:>4d}日")
        
        print(f"\n  ▸▸ 交易绩效")
        print(f"     累计收益: {total_return:+.2f}%")
        print(f"     胜率: {len(win_trades)}/{len(trades)} ({len(win_trades)/len(trades)*100:.1f}%)")
        avg_win = sum(win_trades)/len(win_trades) if win_trades else 0
        avg_loss = sum(loss_trades)/len(loss_trades) if loss_trades else 0
        print(f"     平均盈利: +{avg_win:.2f}% | 平均亏损: {avg_loss:.2f}%")
        print(f"     盈亏比: {abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "     盈亏比: ∞")
        
        # 最大连续盈利/亏损
        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for r in all_returns:
            if r > 0:
                cur_win += 1; cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            else:
                cur_loss += 1; cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)
        print(f"     最大连赢: {max_win_streak}次 | 最大连亏: {max_loss_streak}次")
    else:
        print(f"     无交易信号触发")
    
    if position == 1:
        print(f"\n  ▸▸ 当前持仓")
        print(f"     买入日: {buy_date}  买入价: {buy_price:.2f}")
        print(f"     最新价: {closes[-1]:.2f}  浮动盈亏: {unrealized:+.2f}%")
    
    # ─── 近期信号(5/10-5/25) ───
    print(f"\n  ▸▸ 近期信号 (5/10-5/25)")
    recent = [s for s in daily_signals if s['date'] >= '2026-05-10'][-11:]
    if recent:
        print(f"     {'日期':>12s} {'收盘':>9s} {'日涨跌':>7s} {'信号':>12s} {'V分':>6s} {'趋势':>6s}")
        print(f"     {'─'*55}")
        for s in recent:
            print(f"     {s['date']:>12s} {s['close']:>9.2f} {s['chg']:>6.2f}% {s['signal']:>12s} {s['v']:>6.1f} {s['tr']:>6.1f}")

# ─── 综合对比 ───
print(f"\n{'='*120}")
print("📊 5只股票 综合交易绩效对比")
print(f"{'='*120}")

cur.close(); conn.close()
print(f"\n✅ 数据: Tushare Pro daily_kline_qfq | 策略: V型评分>45 & 趋势≥90→买入")
print(f"   评分引擎: v2.0 (趋势35%+动量35%+波动15%+量能15%, V型c=25)")
