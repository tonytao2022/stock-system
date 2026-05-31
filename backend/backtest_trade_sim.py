#!/usr/bin/env python3
"""
时序回测 v4.0 — ATR×2.0 + 中性续持
====================================
相对v3.0的两处改动:
  1. 止损从 ATR×1.5 放宽到 ATR×2.0 (续持后缩到 ATR×0.8)
  2. 10日复查时 CAUTIOUS_BUY 也续持, 但止损缩到 ATR×0.8
"""
import pymysql, sys
from db_config import get_connection
from collections import defaultdict

sys.path.insert(0, '.')
from engine.vmap import vmap_score
from engine.chanlun_scorer import score_chanlun_enhanced
from engine.cycle_scorer import score_cycle_enhanced
from engine.indicators import rsi, sma, atr
from engine.sentiment_scorer import score_sentiment
from engine.block_weights import get_block_weights, apply_block_weights

conn = get_connection()
cur = conn.cursor(pymysql.cursors.DictCursor)

target_inds = ['半导体','元器件','通信设备','IT设备','电气设备']
ind_str = ','.join([f"'{x}'" for x in target_inds])
cur.execute(f"SELECT bp.ts_code, sb.industry FROM backtest_pool bp JOIN stock_basic sb ON bp.ts_code=sb.ts_code WHERE bp.status='ACTIVE' AND sb.industry IN ({ind_str})")
stocks = cur.fetchall()
print(f"📋 聚焦池: {len(stocks)} 只\n")

mkt_sea = 'chaos_spring'; mkt_score = 2.0; regime = 'range'; breadth = 0.52
FIRST_HOLD = 10; MAX_HOLD = 20

all_trades = []

for idx, s in enumerate(stocks):
    code = s['ts_code']; ind = s['industry'] or '未知'
    cur.execute("SELECT trade_date, high, low, close, vol, change_pct FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",(code,))
    rows = cur.fetchall()
    if len(rows) < MAX_HOLD + 200: continue
    
    closes = [float(r['close']) for r in rows]; vols = [float(r.get('vol',0) or 0) for r in rows]
    chgs = [float(r.get('change_pct') or 0) for r in rows]; n = len(closes)
    
    position = 0; entry_price = 0; entry_idx = 0; extended = False
    
    for i in range(200, n-1):
        c = closes[:i+1]; h = [float(r['high']) for r in rows[:i+1]]
        l = [float(r['low']) for r in rows[:i+1]]; v = vols[:i+1]; cg = chgs[:i+1]
        
        all_win = [{'close':c[j],'high':h[j],'low':l[j],'vol':v[j]} for j in range(len(c))]
        chanlun = score_chanlun_enhanced(all_win, mkt_sea, ind)
        cycle = score_cycle_enhanced(mkt_sea, regime, mkt_score, ind, c)
        bw_r = get_block_weights(ind)
        l2_raw = apply_block_weights(chanlun.trend, chanlun.momentum, chanlun.volatility, chanlun.volume, bw_r)
        cl_total = round(max(0, min(100, l2_raw + chanlun.chanlun_signal * 0.15)), 1)
        r14 = rsi(c,14)
        v5m = sma(v[-10:],5) if len(v)>=10 else v[-1]; v20m = sma(v[-25:],20) if len(v)>=25 else v5m
        vol_reg = 'high' if v5m>v20m*1.3 else ('low' if v5m<v20m*0.7 else 'normal')
        sent = score_sentiment(breadth, vol_reg, r14, cg[-1] if cg else 0)
        raw = cycle.score*0.30 + cl_total*0.40 + sent.score*0.30
        v = vmap_score(raw, 25)
        
        signal = 'HOLD'
        if v >= 38 and chanlun.trend >= 80: signal = 'BUY'
        elif v >= 34 and chanlun.trend >= 75: signal = 'CAUTIOUS_BUY'
        elif v < 10: signal = 'SELL'
        elif chanlun.trend < 25: signal = 'SELL'
        
        if position == 0 and signal in ('BUY','CAUTIOUS_BUY'):
            entry_idx = i; entry_price = closes[i+1]; position = 1; extended = False; continue
        
        if position == 1:
            cur_price = closes[i+1]; hold_days = i - entry_idx
            ret_pct = (cur_price - entry_price) / entry_price * 100
            
            # 止损: ATR×2.0 (续持后缩到ATR×0.8)
            stop_val = atr(h,l,c,14)
            stop_mult = 0.8 if extended else 2.0
            stop_pct = min(0.15, max(0.03, stop_val/entry_price*stop_mult))
            if cur_price < entry_price * (1 - stop_pct):
                all_trades.append({'code':code,'industry':ind,'ret':round(ret_pct,2),'hold_days':hold_days,'reason':'止损','extended':extended}); position = 0; continue
            
            if signal == 'SELL':
                all_trades.append({'code':code,'industry':ind,'ret':round(ret_pct,2),'hold_days':hold_days,'reason':'信号反转','extended':extended}); position = 0; continue
            
            if hold_days >= FIRST_HOLD and not extended:
                if signal in ('BUY','CAUTIOUS_BUY'):
                    extended = True; continue
                else:
                    all_trades.append({'code':code,'industry':ind,'ret':round(ret_pct,2),'hold_days':hold_days,'reason':'到期平仓','extended':extended}); position = 0; continue
            
            if hold_days >= MAX_HOLD:
                all_trades.append({'code':code,'industry':ind,'ret':round(ret_pct,2),'hold_days':hold_days,'reason':'到期平仓(强制)','extended':extended}); position = 0; continue
    
    if position == 1:
        ret_pct = (closes[-1] - entry_price) / entry_price * 100
        all_trades.append({'code':code,'industry':ind,'ret':round(ret_pct,2),'hold_days':n-1-entry_idx,'reason':'期末强制平仓','extended':extended})
    
    if (idx+1) % 20 == 0: print(f"  进度: {idx+1}/{len(stocks)} ({len(all_trades)}笔)")

returns = [t['ret'] for t in all_trades]; wins = [r for r in returns if r>0]; losses = [r for r in returns if r<=0]
print(f"\n{'='*100}")
print("📊 回测结果 (ATR×2.0 + 中性续持)")
print(f"{'='*100}")
print(f"  总交易数: {len(all_trades)}")
print(f"  胜率: {len(wins)}/{len(returns)} ({len(wins)/len(returns)*100:.1f}%)")
print(f"  均收益: {sum(returns)/len(returns):+.2f}%")
if wins and losses: print(f"  盈亏比: {sum(wins)/len(wins)/abs(sum(losses)/len(losses)):.2f}")
print(f"  平均持有期: {sum(t['hold_days'] for t in all_trades)/len(all_trades):.1f}日")

by_reason = defaultdict(list)
for t in all_trades: by_reason[t['reason']].append(t['ret'])
print(f"\n  退出原因:")
for reason in ['止损','信号反转','到期平仓','到期平仓(强制)','期末强制平仓']:
    grp = by_reason[reason]
    if grp:
        w = sum(1 for r in grp if r>0)
        print(f"    {reason:<18s}: {len(grp):>4d}笔 均={sum(grp)/len(grp):>+6.2f}% 胜率={w}/{len(grp)}({w/len(grp)*100:.0f}%)")

extended_trades = [t for t in all_trades if t['extended']]
if extended_trades:
    er = [t['ret'] for t in extended_trades]; ew = [r for r in er if r>0]
    print(f"\n  续持效果: {len(extended_trades)}笔 均={sum(er)/len(er):+.2f}% 胜率={len(ew)}/{len(er)}({len(ew)/len(er)*100:.0f}%)")

# 和v3.0对比
print(f"\n  🔄 对比 v3.0: 胜率50.8% 均+6.94% 盈亏比2.55 止损36.1%")
conn.close()
print("✅ 完成")
