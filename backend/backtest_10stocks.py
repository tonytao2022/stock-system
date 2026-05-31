#!/usr/bin/env python3
"""
10只股票精细回测 — 逐因子、逐日验证评分对未来收益的预测力
"""
import os, sys, pymysql, math, json
from db_config import get_connection
from datetime import datetime, date, timedelta
from collections import defaultdict

# ─── 工具函数 ───
def roll_mean(d, w):
    r=[None]*len(d)
    for i in range(w-1,len(d)): r[i]=sum(d[i-w+1:i+1])/w
    return r

def roll_std(d, w):
    r=[None]*len(d)
    for i in range(w-1,len(d)):
        avg=sum(d[i-w+1:i+1])/w
        r[i]=math.sqrt(sum((x-avg)**2 for x in d[i-w+1:i+1])/w)
    return r

def roll_max(d,w):
    r=[None]*len(d)
    for i in range(len(d)):
        s=max(0,i-w+1); r[i]=max(d[s:i+1])
    return r

def roll_min(d,w):
    r=[None]*len(d)
    for i in range(len(d)):
        s=max(0,i-w+1); r[i]=min(d[s:i+1])
    return r

# ─── 评分 ───
def score_one(close, highs, lows, vols, i, ma5,ma10,ma20,ma60,ma120, std20, hh20, ll20, vol_ma20, vol_ma60):
    c = close[i]
    if c <= 0: return {'total':0,'trend':0,'momentum':0,'wave':0,'volume':0}
    
    # trend
    tr = 0
    if all(x is not None for x in [ma5[i],ma10[i],ma20[i],ma60[i],ma120[i]]):
        align = 0
        if ma5[i]>ma10[i]: align+=12.5
        if ma5[i]>ma20[i]: align+=12.5
        if ma10[i]>ma20[i]: align+=12.5
        if ma20[i]>ma60[i]: align+=12.5
        pos = 0
        if c>ma5[i]: pos+=8
        if c>ma10[i]: pos+=8
        if c>ma20[i]: pos+=9
        slope=0
        if ma20[i-20] and ma20[i-20]>0 and i>=20:
            raw=(ma20[i]-ma20[i-20])/ma20[i-20]
            raw=max(-0.2,min(0.3,raw))
            slope=25*(raw+0.2)/0.5
        tr = min(100,max(0,align+pos+slope))
    
    # momentum
    mo = 0
    m5=(c-close[i-5])/close[i-5] if i>=5 and close[i-5]>0 else 0
    m10=(c-close[i-10])/close[i-10] if i>=10 and close[i-10]>0 else 0
    m20=(c-close[i-20])/close[i-20] if i>=20 and close[i-20]>0 else 0
    m5s=min(20,max(0,10+m5*40))
    m10s=min(10,max(0,5+m10*20))
    m20s=min(10,max(0,5+m20*15))
    
    vq=0
    if all(x is not None for x in [vols[i],vol_ma20[i]]) and vol_ma20[i]>0 and i>=1:
        vr=vols[i]/vol_ma20[i]; pr=c/close[i-1] if close[i-1]>0 else 1
        if pr>1 and vr>1: vq=min(30,15+vr*5)
        elif pr>1 and vr<=1: vq=max(5,10+(pr-1)*200)
        elif pr<1 and vr<0.8: vq=max(0,8)
        else: vq=max(0,5-vr*2)
    
    mr=0
    if i>=14 and all(x>0 for x in close[i-14:i+1]):
        gains=sum(max(0,close[j]-close[j-1]) for j in range(i-13,i+1))
        losses=sum(max(0,close[j-1]-close[j]) for j in range(i-13,i+1))
        if gains+losses>0: mr=100*gains/(gains+losses)*0.3
    mo=min(100,max(0,m5s+m10s+m20s+vq+mr))
    
    # wave
    wv=50
    if all(x is not None for x in [std20[i],c,hh20[i],ll20[i]]):
        dv=std20[i]/c
        vh=0
        if dv<0.005: vh=15
        elif dv<0.015: vh=40+(dv-0.005)/0.01*10
        elif dv<0.03: vh=25+(0.03-dv)/0.015*25
        else: vh=max(10,25-(dv-0.03)*500)
        rng=hh20[i]-ll20[i]
        ds=50*(c-ll20[i])/rng if rng>0 else 25
        wv=min(100,max(0,vh*0.6+ds*0.4))
    
    # volume
    vl=50
    if all(x is not None for x in [vol_ma20[i],vol_ma60[i]]):
        vts=min(40,max(0,20+(vol_ma20[i]/vol_ma60[i]-1)*20)) if vol_ma60[i]>0 else 20
        vrs=25
        if vol_ma20[i]>0 and vols[i]>0:
            vr=vols[i]/vol_ma20[i]
            vrs=max(10,25-(0.7-vr)*30) if vr<0.7 else (25 if vr<=1.5 else max(10,30-(vr-1.5)*10))
        mvr=min(30,max(10,max(vols[max(0,i-10):i+1])/vol_ma20[i]*10)) if vol_ma20[i]>0 else 15
        vl=min(100,max(0,vts*0.3+vrs*0.3+mvr*0.4))
    
    total = tr*0.4 + mo*0.3 + wv*0.2 + vl*0.1
    return {'total':round(total,1),'trend':round(tr,1),'momentum':round(mo,1),'wave':round(wv,1),'volume':round(vl,1)}

# ─── 主流程 ───
def main():
    conn = get_connection()
    
    stocks = [
        ('001211.SZ','双枪科技'),('001696.SZ','宗申动力'),('000048.SZ','京基智农'),
        ('000607.SZ','华媒控股'),('000906.SZ','浙商中拓'),('002344.SZ','海宁皮城'),
        ('000016.SZ','*ST康佳A'),('000066.SZ','中国长城'),('603019.SH','中科曙光'),
        ('000026.SZ','飞亚达')
    ]
    
    print(f"{'='*100}")
    print(f"📊 10只股票精细化回测")
    print(f"{'='*100}\n")
    
    all_records = []
    
    for ts_code, name in stocks:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT trade_date, open, high, low, close, vol FROM daily_kline WHERE ts_code=%s ORDER BY trade_date ASC", (ts_code,))
        rows = cur.fetchall()
        cur.close()
        
        if len(rows) < 121: continue
        
        close = [float(r['close']) for r in rows]
        high = [float(r['high']) for r in rows]
        low = [float(r['low']) for r in rows]
        vol = [float(r['vol'] or 0) for r in rows]
        dates = [r['trade_date'] for r in rows]
        
        ma5=roll_mean(close,5); ma10=roll_mean(close,10); ma20=roll_mean(close,20)
        ma60=roll_mean(close,60); ma120=roll_mean(close,120)
        std20=roll_std(close,20); hh20=roll_max(high,20); ll20=roll_min(low,20)
        vol_ma20=roll_mean(vol,20); vol_ma60=roll_mean(vol,60)
        
        stock_records = []
        for i in range(120, len(rows)):
            s = score_one(close, high, low, vol, i, ma5,ma10,ma20,ma60,ma120, std20, hh20, ll20, vol_ma20, vol_ma60)
            f5 = (close[i+5]-close[i])/close[i] if i+5<len(rows) else None
            f10 = (close[i+10]-close[i])/close[i] if i+10<len(rows) else None
            f20 = (close[i+20]-close[i])/close[i] if i+20<len(rows) else None
            stock_records.append({**s, 'ts_code':ts_code, 'name':name, 'date':dates[i], 'close':close[i], 'f5':f5, 'f10':f10, 'f20':f20})
            all_records.append(stock_records[-1])
        
        # ─── 单只分析 ───
        print(f"{'─'*100}")
        print(f"🐂 {ts_code} {name}")
        print(f"{'─'*100}")
        print(f"  交易日: {len(stock_records)}")
        
        # 按5日收益排序,分三组
        for fwd, fwd_name in [(5,'5日'),(10,'10日'),(20,'20日')]:
            f_recs = [r for r in stock_records if r[f'f{fwd}'] is not None]
            f_recs.sort(key=lambda x: x['total'])
            n = len(f_recs)
            
            top_s = [r for r in f_recs if r['total'] >= f_recs[int(n*0.7)]['total']]
            mid_s = [r for r in f_recs if f_recs[int(n*0.3)]['total'] <= r['total'] < f_recs[int(n*0.7)]['total']]
            bot_s = [r for r in f_recs if r['total'] < f_recs[int(n*0.3)]['total']]
            
            top_ret = sum(r[f'f{fwd}'] for r in top_s)/len(top_s) if top_s else 0
            mid_ret = sum(r[f'f{fwd}'] for r in mid_s)/len(mid_s) if mid_s else 0
            bot_ret = sum(r[f'f{fwd}'] for r in bot_s)/len(bot_s) if bot_s else 0
            
            top_sc = sum(r['total'] for r in top_s)/len(top_s) if top_s else 0
            bot_sc = sum(r['total'] for r in bot_s)/len(bot_s) if bot_s else 0
            
            spread = top_ret - bot_ret
            sig = '✅' if spread > 0 else '❌'
            
            print(f"  {fwd_name}预测: 高分组({top_sc:.0f}分)={top_ret*100:+.1f}%  "
                  f"中分组={mid_ret*100:+.1f}%  低分组({bot_sc:.0f}分)={bot_ret*100:+.1f}%  "
                  f"高-低利差={spread*100:+.1f}% {sig}")
    
    # ─── 10只合并分析 ───
    print(f"\n{'='*100}")
    print(f"📈 10只合并统计")
    print(f"{'='*100}")
    
    for fwd in [5,10,20]:
        f_recs = [r for r in all_records if r[f'f{fwd}'] is not None]
        f_recs.sort(key=lambda x: x['total'])
        n = len(f_recs)
        
        print(f"\n{'─'*80}")
        print(f"  预测窗口: {fwd}日 ({n}条记录)")
        print(f"{'─'*80}")
        print(f"  {'分组':8s} {'数量':>6s} {'均分':>6s} {'收益均':>8s} {'胜率':>7s} {'最大':>8s} {'最小':>8s}")
        print(f"  {'─'*50}")
        
        for g in range(5):
            start = int(n*g/5)
            end = int(n*(g+1)/5)
            group = f_recs[start:end]
            avg_sc = sum(r['total'] for r in group)/len(group)
            avg_ret = sum(r[f'f{fwd}'] for r in group)/len(group)
            win_rate = sum(1 for r in group if r[f'f{fwd}']>0)/len(group)
            mx = max(r[f'f{fwd}'] for r in group)
            mn = min(r[f'f{fwd}'] for r in group)
            label = ['最低分','低分','中等','高分','最高分'][g]
            print(f"  {label:8s} {len(group):>6d} {avg_sc:>5.0f} {avg_ret*100:>7.2f}% {win_rate:>6.1%} {mx*100:>7.2f}% {mn*100:>7.2f}%")
        
        top_g = f_recs[int(n*0.8):]
        bot_g = f_recs[:int(n*0.2)]
        top_ret = sum(r[f'f{fwd}'] for r in top_g)/len(top_g)
        bot_ret = sum(r[f'f{fwd}'] for r in bot_g)/len(bot_g)
        print(f"  {'─'*50}")
        print(f"  高分组({f_recs[int(n*0.8)]['total']:.0f}+分): {top_ret*100:+.2f}%")
        print(f"  低分组({f_recs[int(n*0.2)]['total']:.0f}分以下): {bot_ret*100:+.2f}%")
        print(f"  利差: {(top_ret-bot_ret)*100:+.2f}%")
    
    # ─── 因子贡献分析 ───
    print(f"\n{'='*100}")
    print(f"🔬 因子内部相关性 (评分子模块 vs {5}日收益)")
    print(f"{'='*100}")
    
    f5_recs = [r for r in all_records if r['f5'] is not None]
    for factor in ['trend','momentum','wave','volume']:
        vals = [r[factor] for r in f5_recs]
        rets = [r['f5'] for r in f5_recs]
        n = len(vals)
        avg_v = sum(vals)/n; avg_r = sum(rets)/n
        cov = sum((vals[i]-avg_v)*(rets[i]-avg_r) for i in range(n))/n
        std_v = math.sqrt(sum((v-avg_v)**2 for v in vals)/n)
        std_r = math.sqrt(sum((r-avg_r)**2 for r in rets)/n)
        ic = cov/(std_v*std_r) if std_v>0 and std_r>0 else 0
        print(f"  {factor:10s} IC={ic:+.4f}  均值={avg_v:.1f}  标准差={std_v:.1f}")
    
    conn.close()
    print(f"\n✅ 10只回测完成")

if __name__ == '__main__':
    main()
