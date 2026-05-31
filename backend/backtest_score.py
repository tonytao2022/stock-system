#!/usr/bin/env python3
"""
个股综合评分回测 v1
基于 OHLCV 日K数据，构建多层因子评分模型，回测评分对N日后收益的预测力

评分因子体系（权重留空等优化）：
1. 趋势因子(40%): MA排列方向 + 价格位置 + 趋势强度
2. 动量因子(30%): 短期动量 + 中期动量 + 量价配合
3. 波动因子(20%): 波动率健康度 + 回撤控制
4. 量能因子(10%): 放量程度 + 换手活跃度

输出: 每日每只股票0-100综合评分 → 对比N日后实际收益 → 统计IC/分层收益
"""
import os, sys, pymysql, math, time
from db_config import get_connection
from datetime import datetime, date, timedelta
from collections import defaultdict, OrderedDict

# ─── DB ───


# ═══════════════════════════════════════════════
# 因子计算模块
# ═══════════════════════════════════════════════

def rolling_mean(data, window):
    r = [None]*len(data)
    for i in range(window-1, len(data)):
        r[i] = sum(data[i-window+1:i+1])/window
    return r

def rolling_std(data, window):
    r = [None]*len(data)
    for i in range(window-1, len(data)):
        avg = sum(data[i-window+1:i+1])/window
        var = sum((x-avg)**2 for x in data[i-window+1:i+1])/window
        r[i] = math.sqrt(var)
    return r

def rolling_max(data, window):
    r = [None]*len(data)
    for i in range(len(data)):
        start = max(0, i-window+1)
        r[i] = max(data[start:i+1])
    return r

def rolling_min(data, window):
    r = [None]*len(data)
    for i in range(len(data)):
        start = max(0, i-window+1)
        r[i] = min(data[start:i+1])
    return r

# ═══════════════════════════════════════════════
# 单只股票评分计算
# ═══════════════════════════════════════════════

def compute_scores_for_stock(closes, highs, lows, vols, dates):
    """
    输入: 按时间升序排列的OHLCV数组
    输出: 每日评分字典 {idx: {total_score, trend_score, momentum_score, vol_score, wave_score, detail}}
    """
    n = len(closes)
    if n < 121:
        return None
    
    # 计算需要的指标
    ma5   = rolling_mean(closes, 5)
    ma10  = rolling_mean(closes, 10)
    ma20  = rolling_mean(closes, 20)
    ma60  = rolling_mean(closes, 60)
    ma120 = rolling_mean(closes, 120)
    
    std20 = rolling_std(closes, 20)
    hh20  = rolling_max(highs, 20)
    ll20  = rolling_min(lows, 20)
    vol_ma20 = rolling_mean(vols, 20)
    vol_ma60 = rolling_mean(vols, 60)
    
    daily_scores = {}
    
    for i in range(120, n):
        close = closes[i]
        if close <= 0:
            continue
        
        score_detail = {}
        w_trend = 0.40
        w_momentum = 0.30
        w_wave = 0.20
        w_volume = 0.10
        
        # ─── 1. 趋势因子 (40%) ───
        trend_score = 0.0
        if all(x is not None for x in [ma5[i],ma10[i],ma20[i],ma60[i],ma120[i]]):
            # MA排列打分 (0-50)
            ma_alignment = 0
            if ma5[i] > ma10[i]: ma_alignment += 12.5
            if ma5[i] > ma20[i]: ma_alignment += 12.5
            if ma10[i] > ma20[i]: ma_alignment += 12.5
            if ma20[i] > ma60[i]: ma_alignment += 12.5
            # (去掉ma20>ma120,减少大盘环境干扰)
            
            # 价格在MA系统中的位置 (0-25)
            price_position = 0
            if close > ma5[i]:  price_position += 8
            if close > ma10[i]: price_position += 8
            if close > ma20[i]: price_position += 9
            
            # 趋势强度: MA5/MA20的斜率 (0-25)
            trend_strength = 0
            if ma20[i] > 0 and ma5[i-20] is not None:
                ma20_slope = (ma20[i] - ma20[i-20]) / ma20[i-20]
                ma20_slope = max(-0.2, min(0.3, ma20_slope))
                trend_strength = 25 * (ma20_slope + 0.2) / 0.5
                trend_strength = max(0, min(25, trend_strength))
            
            trend_raw = ma_alignment + price_position + trend_strength
            trend_score = min(100, max(0, trend_raw))
        
        score_detail['trend_raw'] = round(trend_score, 1)
        
        # ─── 2. 动量因子 (30%) ───
        momentum_score = 0.0
        # 短期动量 (0-40): 5日/10日/20日收益率
        m5 = (close - closes[i-5]) / closes[i-5] if i >= 5 and closes[i-5] > 0 else 0
        m10 = (close - closes[i-10]) / closes[i-10] if i >= 10 and closes[i-10] > 0 else 0
        m20 = (close - closes[i-20]) / closes[i-20] if i >= 20 and closes[i-20] > 0 else 0
        
        m5_s = min(20, max(0, 10 + m5*40))   # -25%~+25%映射到0~20
        m10_s = min(10, max(0, 5 + m10*20))   # -25%~+25%映射到0~10
        m20_s = min(10, max(0, 5 + m20*15))   # -33%~+33%映射到0~10
        
        # 量价配合: 涨幅放量加分 (0-30)
        volume_quality = 0
        if (all(x is not None for x in [vols[i], vol_ma20[i]]) and 
            vol_ma20[i] > 0 and i >= 1 and vols[i-1] > 0):
            vol_ratio = vols[i] / vol_ma20[i]
            price_ratio = close / closes[i-1] if closes[i-1] > 0 else 1
            
            # 价升量增 = 正常, 价升量缩 = 分歧, 价跌量增 = 恐慌
            if price_ratio > 1 and vol_ratio > 1:
                volume_quality = min(30, 15 + vol_ratio * 5)  # 放量上涨
            elif price_ratio > 1 and vol_ratio <= 1:
                volume_quality = max(5, 10 + (price_ratio-1)*200)  # 缩量上涨
            elif price_ratio < 1 and vol_ratio < 0.8:
                volume_quality = max(0, 8)  # 缩量下跌
            else:
                volume_quality = max(0, 5 - vol_ratio*2)  # 放量下跌
        
        # RSI-like 动量 (0-30)
        momentum_rsi = 0
        if i >= 14 and all(x > 0 for x in closes[i-14:i+1]):
            gains = sum(max(0, closes[j]-closes[j-1]) for j in range(i-13, i+1))
            losses = sum(max(0, closes[j-1]-closes[j]) for j in range(i-13, i+1))
            if gains + losses > 0:
                rsi = 100 * gains / (gains + losses)
                momentum_rsi = rsi * 0.30
        
        momentum_raw = m5_s + m10_s + m20_s + volume_quality + momentum_rsi
        momentum_score = min(100, max(0, momentum_raw))
        score_detail['momentum_raw'] = round(momentum_score, 1)
        
        # ─── 3. 波动/风险因子 (20%) ───
        wave_score = 50  # 默认中性
        
        if all(x is not None for x in [std20[i], close, hh20[i], ll20[i]]):
            # 波动率评估 (0-50)
            daily_vol = std20[i] / close
            # 日波动率0.5%~3%为正常，<0.5%太死，>3%太妖
            if daily_vol < 0.005:
                vol_health = 15  # 太死, 没有市场关注
            elif daily_vol < 0.015:
                vol_health = 40 + (daily_vol-0.005)/0.01 * 10  # 最佳区间 40-50
            elif daily_vol < 0.03:
                vol_health = 25 + (0.03-daily_vol)/0.015 * 25  # 中性偏高
            else:
                vol_health = max(10, 25 - (daily_vol-0.03)*500)  # 太妖
            
            # 回撤控制 (0-50)
            if hh20[i] - ll20[i] > 0:
                position_in_range = (close - ll20[i]) / (hh20[i] - ll20[i])
                # 在区间上部 = 趋势好, 在下部 = 风险
                drawdown_score = 50 * position_in_range
            else:
                drawdown_score = 25
            
            wave_raw = vol_health * 0.6 + drawdown_score * 0.4
            wave_score = min(100, max(0, wave_raw))
        
        score_detail['wave_raw'] = round(wave_score, 1)
        
        # ─── 4. 量能因子 (10%) ───
        volume_score = 50
        if all(x is not None for x in [vol_ma20[i], vol_ma60[i]]):
            # 比较20日均量与60日均量 (0-40)
            if vol_ma60[i] > 0:
                vol_trend = vol_ma20[i] / vol_ma60[i]
                vol_trend_score = min(40, max(0, 20 + (vol_trend-1)*20))
            else:
                vol_trend_score = 20
            
            # 当日量比 (0-30)
            if vol_ma20[i] > 0 and vols[i] > 0:
                vol_ratio = vols[i] / vol_ma20[i]
                # 0.5~2.0为正常区间
                if 0.7 <= vol_ratio <= 1.5:
                    vol_ratio_score = 25
                elif vol_ratio < 0.7:
                    vol_ratio_score = max(10, 25 - (0.7-vol_ratio)*30)
                else:
                    vol_ratio_score = max(10, 30 - (vol_ratio-1.5)*10)
            else:
                vol_ratio_score = 15
            
            # 近日最大单日量 (0-30) — 用来识别爆发力
            recent_vols = vols[max(0,i-10):i+1]
            if vol_ma20[i] > 0:
                max_vol_ratio = max(v/vol_ma20[i] for v in recent_vols if v and vol_ma20[i])
                max_vol_score = min(30, max(10, max_vol_ratio * 10))
            else:
                max_vol_score = 15
            
            volume_raw = vol_trend_score * 0.3 + vol_ratio_score * 0.3 + max_vol_score * 0.4
            volume_score = min(100, max(0, volume_raw))
        
        score_detail['volume_raw'] = round(volume_score, 1)
        
        # ─── 综合评分 ───
        total = (trend_score * w_trend + 
                 momentum_score * w_momentum + 
                 wave_score * w_wave + 
                 volume_score * w_volume)
        
        daily_scores[i] = {
            'trade_date': dates[i],
            'close': close,
            'total_score': round(total, 1),
            'trend_score': round(trend_score, 1),
            'momentum_score': round(momentum_score, 1),
            'wave_score': round(wave_score, 1),
            'volume_score': round(volume_score, 1),
            'detail': score_detail
        }
    
    return daily_scores

# ═══════════════════════════════════════════════
# 回测统计
# ═══════════════════════════════════════════════

def evaluate_predictive_power(all_results, forward_days=5):
    """
    评分回测: 检验高分组合 vs 低分组合的N日收益差异
    """
    records = []
    for ts_code, data in all_results.items():
        for idx, s in data.items():
            fwd_close = None
            if idx + forward_days in data:
                fwd_close = data[idx + forward_days]['close']
            if fwd_close and s['close'] > 0:
                fwd_ret = (fwd_close - s['close']) / s['close']
                records.append({
                    'ts_code': ts_code,
                    'trade_date': s['trade_date'],
                    'score': s['total_score'],
                    'trend': s['trend_score'],
                    'momentum': s['momentum_score'],
                    'wave': s['wave_score'],
                    'volume': s['volume_score'],
                    f'fwd_{forward_days}d': round(fwd_ret, 6)
                })
    
    if not records:
        return None
    
    records.sort(key=lambda x: x['score'])
    n = len(records)
    
    # 分10组
    results = []
    for g in range(10):
        start = int(n * g / 10)
        end = int(n * (g+1) / 10)
        group = records[start:end]
        avg_score = sum(r['score'] for r in group) / len(group)
        avg_fwd = sum(r[f'fwd_{forward_days}d'] for r in group) / len(group)
        results.append({
            'group': g+1,
            'n': len(group),
            'avg_score': round(avg_score, 1),
            f'avg_fwd_{forward_days}d': round(avg_fwd, 6)
        })
    
    # IC: 评分与未来收益的相关系数
    scores = [r['score'] for r in records]
    fwds = [r[f'fwd_{forward_days}d'] for r in records]
    avg_s = sum(scores)/len(scores)
    avg_f = sum(fwds)/len(fwds)
    cov = sum((s-avg_s)*(f-avg_f) for s,f in zip(scores, fwds))/len(scores)
    std_s = math.sqrt(sum((s-avg_s)**2 for s in scores)/len(scores))
    std_f = math.sqrt(sum((f-avg_f)**2 for f in fwds)/len(fwds))
    ic = cov/(std_s*std_f) if std_s>0 and std_f>0 else 0
    
    return {
        'total_records': n,
        'groups': results,
        'ic': round(ic, 4),
        'top_bottom_spread': round(results[-1][f'avg_fwd_{forward_days}d'] - results[0][f'avg_fwd_{forward_days}d'], 6),
        'avg_fwd_all': round(avg_f, 6)
    }

# ═══════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════

def main():
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 获取回测池股票
    cur.execute("SELECT ts_code, name FROM backtest_pool WHERE status='ACTIVE' AND market != '指数' ORDER BY ts_code")
    stocks = cur.fetchall()
    print(f"📋 回测池个股: {len(stocks)}只\n")
    
    all_scores = {}
    skipped = 0
    
    for idx, s in enumerate(stocks):
        ts_code = s['ts_code']
        
        cur2 = conn.cursor(pymysql.cursors.DictCursor)
        cur2.execute(
            "SELECT trade_date, open, high, low, close, vol FROM daily_kline WHERE ts_code=%s ORDER BY trade_date ASC",
            (ts_code,)
        )
        rows = cur2.fetchall()
        cur2.close()
        
        if len(rows) < 121:
            skipped += 1
            continue
        
        closes = [float(r['close']) for r in rows]
        highs = [float(r['high']) for r in rows]
        lows = [float(r['low']) for r in rows]
        vols = [float(r['vol'] or 0) for r in rows]
        dates = [r['trade_date'] for r in rows]
        
        scores = compute_scores_for_stock(closes, highs, lows, vols, dates)
        if scores:
            all_scores[ts_code] = scores
        
        if (idx+1) % 30 == 0:
            print(f"  已完成 {idx+1}/{len(stocks)}")
    
    print(f"\n✅ 评分完成: {len(all_scores)}只股票, 跳过{skipped}只数据不足")
    
    total_score_days = sum(len(v) for v in all_scores.values())
    print(f"   总评分数: {total_score_days}条")
    
    # ─── 回测: 分别检验5/10/20日预测力 ───
    print(f"\n{'='*80}")
    print(f"📊 评分因子回测报告")
    print(f"{'='*80}")
    
    for fwd in [5, 10, 20]:
        result = evaluate_predictive_power(all_scores, fwd)
        if not result:
            continue
        
        print(f"\n{'─'*80}")
        print(f"  预测窗口: {fwd}日")
        print(f"{'─'*80}")
        print(f"  总记录数: {result['total_records']}")
        print(f"  IC (信息系数): {result['ic']:.4f}")
        print(f"  头部-底部利差: {result['top_bottom_spread']*100:+.2f}%")
        print(f"  全样本平均收益: {result['avg_fwd_all']*100:+.2f}%")
        print(f"\n  {'分组':8s} {'数量':>6s} {'平均评分':>8s} {f'{fwd}日收益':>10s}")
        print(f"  {'─'*40}")
        for g in result['groups']:
            print(f"  {'第'+str(g['group'])+'组':8s} {g['n']:>6d} {g['avg_score']:>8.1f} {g[f'avg_fwd_{fwd}d']*100:>9.2f}%")
    
    # ─── 分数打分分布 ───
    print(f"\n{'─'*80}")
    print(f"📈 评分分布统计")
    print(f"{'─'*80}")
    
    all_sc = []
    for ts_code, data in all_scores.items():
        all_sc.extend(s['total_score'] for s in data.values())
    
    from collections import Counter
    bins = [(0,10),(10,20),(20,30),(30,40),(40,50),(50,60),(60,70),(70,80),(80,90),(90,100)]
    hist = defaultdict(int)
    for s in all_sc:
        for lo, hi in bins:
            if lo <= s < hi:
                hist[(lo,hi)] += 1
                break
    
    print(f"  评分区间         数量        占比")
    for lo, hi in bins:
        cnt = hist[(lo,hi)]
        print(f"  [{lo:2d}-{hi:2d})      {cnt:>6d}      {cnt/len(all_sc)*100:>5.1f}%")
    
    # ─── 写入数据库 ───
    print(f"\n💾 写入回测数据到MySQL...")
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_score_daily (
            id        BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts_code   VARCHAR(16) NOT NULL,
            trade_date DATE NOT NULL,
            total_score   DECIMAL(5,1),
            trend_score   DECIMAL(5,1),
            momentum_score DECIMAL(5,1),
            wave_score    DECIMAL(5,1),
            volume_score  DECIMAL(5,1),
            close_price   DECIMAL(12,3),
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_code_date (ts_code, trade_date),
            INDEX idx_score (total_score),
            INDEX idx_date (trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股综合评分每日回测'
    """)
    
    cur.execute("TRUNCATE TABLE backtest_score_daily")
    
    insert_sql = """INSERT INTO backtest_score_daily 
        (ts_code, trade_date, total_score, trend_score, momentum_score, wave_score, volume_score, close_price)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
    
    batch = []
    for ts_code, data in all_scores.items():
        for idx, s in data.items():
            batch.append((ts_code, s['trade_date'], s['total_score'], 
                         s['trend_score'], s['momentum_score'], s['wave_score'], s['volume_score'], s['close']))
            if len(batch) >= 1000:
                cur.executemany(insert_sql, batch)
                conn.commit()
                batch = []
    
    if batch:
        cur.executemany(insert_sql, batch)
        conn.commit()
    
    cur.close()
    conn.close()
    print(f"✅ 已写入 {total_score_days} 条评分记录到 backtest_score_daily")

if __name__ == '__main__':
    main()
