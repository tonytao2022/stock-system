#!/usr/bin/env python3
"""
恒纪元判定模型回测 v2
修复: 纯 pymysql 读取数据 + 修复字段长度问题

判定逻辑:
- 春(spring): MA20>MA60>MA120 且 close>MA20 → 多头排列
- 夏(summer): MA20>MA60 且 close>MA60 → 趋势确认
- 秋(autumn): MA60>MA120 但 MA20<MA60 → 多头衰减
- 冬(winter): MA20<MA60<MA120 且 close<MA60 → 空头排列
- 混沌(chaos): 均线缠绕
"""
import os, sys
from db_config import get_connection
import pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict

# ─── DB ───


def get_conn():
    return get_connection()

# ─── 恒纪元四季判定 ───
def judge_season(ma20, ma60, ma120, close):
    if ma20 is None or ma60 is None or ma120 is None or close is None:
        return 'chaos', '数据不足'
    if ma120 <= 0:
        return 'chaos', '数据异常'
    
    m20_60 = ma20 > ma60
    m20_120 = ma20 > ma120
    m60_120 = ma60 > ma120
    p_ma20 = close > ma20
    p_ma60 = close > ma60
    
    spread = abs(ma20 - ma120) / ma120
    
    if m20_60 and m60_120 and p_ma20:
        return 'spring', '大多头排列|MA20>MA60>MA120|进攻期'
    elif m20_60 and p_ma60 and spread > 0.03:
        return 'summer', '多头初成|MA20>MA60|趋势确认'
    elif m20_60 and not p_ma20 and p_ma60:
        return 'summer', '多头回踩|价格MA20下MA60上|短期调整'
    elif m60_120 and not m20_60 and p_ma20:
        return 'autumn', '多头衰减|MA20<MA60但MA60>MA120|防守'
    elif m60_120 and not m20_60 and not p_ma20:
        return 'autumn', '趋势减弱|价格在MA20下|防守减仓'
    elif not m60_120 and not m20_60 and not p_ma60:
        return 'winter', '空头排列|MA20<MA60<MA120|休眠'
    elif not m20_60 and p_ma60:
        return 'autumn', '拐点观察|价格在MA60上MA20下|谨慎'
    else:
        return 'chaos', '均线缠绕|无明确趋势|观望'

# ─── 获取K线并计算MA ───
def get_kline_with_ma(conn, ts_code, min_days=130):
    cur = conn.cursor()
    cur.execute(
        "SELECT trade_date, close FROM daily_kline WHERE ts_code=%s ORDER BY trade_date ASC",
        (ts_code,)
    )
    rows = cur.fetchall()
    cur.close()
    
    if len(rows) < min_days:
        return None
    
    closes = [float(r['close']) for r in rows]
    dates = [r['trade_date'] for r in rows]
    
    # 计算MA
    def rolling_mean(data, window):
        result = [None] * len(data)
        for i in range(window - 1, len(data)):
            result[i] = sum(data[i - window + 1:i + 1]) / window
        return result
    
    ma20 = rolling_mean(closes, 20)
    ma60 = rolling_mean(closes, 60)
    ma120 = rolling_mean(closes, 120)
    
    # 只取最近365天且MA120有效的数据
    cutoff = datetime.now().date() - timedelta(days=365)
    data = []
    for i in range(len(rows)):
        if isinstance(dates[i], datetime):
            d = dates[i].date()
        else:
            d = dates[i]
        if d < cutoff or ma120[i] is None:
            continue
        data.append({
            'trade_date': d,
            'close': closes[i],
            'ma20': ma20[i],
            'ma60': ma60[i],
            'ma120': ma120[i]
        })
    
    return data

# ─── 单只回测 ───
def backtest_one_stock(conn, ts_code):
    data = get_kline_with_ma(conn, ts_code)
    if not data:
        return None, f"数据不足(需≥130个有效交易日, 实得{len(data) if data else 0})"
    
    results = []
    for i, row in enumerate(data):
        season, trend_desc = judge_season(
            row['ma20'], row['ma60'], row['ma120'], row['close']
        )
        
        # 未来N日收益
        f5 = f10 = f20 = None
        if i + 5 < len(data):
            f5 = float((data[i+5]['close'] - row['close']) / row['close'])
        if i + 10 < len(data):
            f10 = float((data[i+10]['close'] - row['close']) / row['close'])
        if i + 20 < len(data):
            f20 = float((data[i+20]['close'] - row['close']) / row['close'])
        
        # 正确性
        is_correct = None
        if season in ('spring', 'summer') and f5 is not None:
            is_correct = 1 if f5 > 0 else 0
        elif season in ('autumn', 'winter') and f5 is not None:
            is_correct = 1 if f5 < 0 else 0
        
        results.append({
            'ts_code': ts_code,
            'trade_date': row['trade_date'],
            'season': season,
            'ma20': round(row['ma20'], 3),
            'ma60': round(row['ma60'], 3),
            'ma120': round(row['ma120'], 3),
            'close_price': row['close'],
            'ma_trend': trend_desc[:100],
            'forward_5d': f5,
            'forward_10d': f10,
            'forward_20d': f20,
            'is_correct': is_correct
        })
    
    return results, None

# ─── 汇总统计 ───
def calc_summary(results):
    if not results: return None
    total = len(results)
    
    def stats(season):
        s = [r for r in results if r['season'] == season]
        cnt = len(s)
        if cnt == 0: return cnt, 0, 0
        valid = [r for r in s if r['is_correct'] is not None]
        acc = sum(r['is_correct'] for r in valid) / len(valid) if valid else 0
        f20_list = [r['forward_20d'] for r in s if r['forward_20d'] is not None]
        avg20 = sum(f20_list) / len(f20_list) if f20_list else 0
        return cnt, round(acc, 4), round(avg20, 4)
    
    sp_cnt, sp_acc, sp_20d = stats('spring')
    su_cnt, su_acc, su_20d = stats('summer')
    au_cnt, au_acc, au_20d = stats('autumn')
    wi_cnt, wi_acc, wi_20d = stats('winter')
    ch_cnt, ch_acc, ch_20d = stats('chaos')
    
    valid_all = [r for r in results if r['is_correct'] is not None]
    overall = round(sum(r['is_correct'] for r in valid_all) / len(valid_all), 4) if valid_all else 0
    
    return {
        'total_days': total,
        'spring_cnt': sp_cnt, 'spring_win': sp_acc, 'spring_avg20d': sp_20d,
        'summer_cnt': su_cnt, 'summer_win': su_acc,
        'autumn_cnt': au_cnt, 'autumn_win': au_acc,
        'winter_cnt': wi_cnt, 'winter_win': wi_acc, 'winter_avg20d': wi_20d,
        'chaos_cnt': ch_cnt, 'chaos_win': ch_acc,
        'overall_acc': overall
    }

# ─── 主流程 ───
def main():
    conn = get_conn()
    
    # 获取股票列表
    cur = conn.cursor()
    cur.execute("SELECT ts_code, name FROM backtest_pool WHERE status='ACTIVE' ORDER BY ts_code")
    stocks = cur.fetchall()
    cur.close()
    
    print(f"📋 回测池: {len(stocks)}只股票\n")
    
    all_results = []
    summaries = []
    skipped = 0
    
    for idx, s in enumerate(stocks):
        ts_code = s['ts_code']
        name = s['name']
        print(f"[{idx+1}/{len(stocks)}] {ts_code} {name} ", end='', flush=True)
        
        results, err = backtest_one_stock(conn, ts_code)
        if err:
            print(f"❌ {err}")
            skipped += 1
            continue
        
        print(f"✅ {len(results)}日判定")
        all_results.extend(results)
        
        summary = calc_summary(results)
        summary['ts_code'] = ts_code
        summary['run_date'] = date.today()
        summaries.append(summary)
    
    # ─── 写入数据库 ───
    print(f"\n💾 写入数据库 ({len(all_results)}条记录)...")
    cur = conn.cursor()
    
    # 清旧数据
    cur.execute("TRUNCATE TABLE backtest_hengjiyuan_daily")
    cur.execute("TRUNCATE TABLE backtest_hengjiyuan_summary")
    
    insert_sql = """INSERT INTO backtest_hengjiyuan_daily 
        (ts_code, trade_date, season, ma20, ma60, ma120, close_price, ma_trend,
         forward_5d, forward_10d, forward_20d, is_correct)
        VALUES (%(ts_code)s, %(trade_date)s, %(season)s, %(ma20)s, %(ma60)s, %(ma120)s,
                %(close_price)s, %(ma_trend)s, %(forward_5d)s, %(forward_10d)s, %(forward_20d)s,
                %(is_correct)s)"""
    
    for i in range(0, len(all_results), 500):
        batch = all_results[i:i+500]
        cur.executemany(insert_sql, batch)
        conn.commit()
    
    sum_sql = """INSERT INTO backtest_hengjiyuan_summary 
        (run_date, ts_code, total_days, spring_cnt, spring_win, summer_cnt, summer_win,
         autumn_cnt, autumn_win, winter_cnt, winter_win, chaos_cnt, chaos_win,
         overall_acc, spring_avg20d, winter_avg20d)
        VALUES (%(run_date)s, %(ts_code)s, %(total_days)s, %(spring_cnt)s, %(spring_win)s,
                %(summer_cnt)s, %(summer_win)s, %(autumn_cnt)s, %(autumn_win)s,
                %(winter_cnt)s, %(winter_win)s, %(chaos_cnt)s, %(chaos_win)s,
                %(overall_acc)s, %(spring_avg20d)s, %(winter_avg20d)s)"""
    
    cur.executemany(sum_sql, summaries)
    conn.commit()
    cur.close()
    
    # ─── 报告 ───
    total_stocks = len(stocks) - skipped
    asr = all_results
    
    # 全市场四季分布
    season_counts = defaultdict(int)
    season_correct = defaultdict(int)
    season_total_correct = defaultdict(int)
    for r in asr:
        season_counts[r['season']] += 1
        if r['is_correct'] is not None:
            season_correct[r['season']] += r['is_correct']
            season_total_correct[r['season']] += 1
    
    print(f"\n{'='*70}")
    print(f"📊 恒纪元判定回测报告")
    print(f"{'='*70}")
    print(f"回测日期: {date.today()}")
    print(f"覆盖股票: {total_stocks}/{len(stocks)}只 (跳过{skipped}只数据不足)")
    print(f"总判定记录: {len(asr)}条")
    
    season_names = [('spring','🌸 春(进攻)'), ('summer','☀️ 夏(持有)'), 
                    ('autumn','🍂 秋(防守)'), ('winter','❄️ 冬(休眠)'), ('chaos','🌪️ 混沌')]
    
    print(f"\n{'─'*70}")
    print(f"📈 全市场四季统计:")
    print(f"{'─'*70}")
    print(f"  {'季节':12s} {'判定次数':>8s} {'准确率':>10s} {'5日平均收益':>12s} {'10日平均收益':>12s} {'20日平均收益':>12s}")
    print(f"  {'─'*70}")
    
    for sn, scn in season_names:
        cnt = season_counts[sn]
        total_c = season_total_correct[sn]
        acc = season_correct[sn] / total_c if total_c > 0 else 0
        f5l = [r['forward_5d'] for r in asr if r['season']==sn and r['forward_5d'] is not None]
        f10l = [r['forward_10d'] for r in asr if r['season']==sn and r['forward_10d'] is not None]
        f20l = [r['forward_20d'] for r in asr if r['season']==sn and r['forward_20d'] is not None]
        avg5 = sum(f5l)/len(f5l) if f5l else 0
        avg10 = sum(f10l)/len(f10l) if f10l else 0
        avg20 = sum(f20l)/len(f20l) if f20l else 0
        print(f"  {scn:12s} {cnt:>8d} {acc:>9.1%} {avg5:>11.2%} {avg10:>11.2%} {avg20:>11.2%}")
    
    # 总体准确率
    total_valid = sum(season_total_correct.values())
    total_corr = sum(season_correct.values())
    print(f"\n  ⭐ 全市场总体准确率: {total_corr/total_valid:.1%} ({total_corr}/{total_valid})")
    
    # 春夏vs秋冬20日利差
    bull_f20 = [r['forward_20d'] for r in asr if r['season'] in ('spring','summer') and r['forward_20d'] is not None]
    bear_f20 = [r['forward_20d'] for r in asr if r['season'] in ('autumn','winter') and r['forward_20d'] is not None]
    bull_avg = sum(bull_f20)/len(bull_f20) if bull_f20 else 0
    bear_avg = sum(bear_f20)/len(bear_f20) if bear_f20 else 0
    print(f"\n  📈 春+夏后20日均收益: {bull_avg:+.2%}")
    print(f"  📉 秋+冬后20日均收益: {bear_avg:+.2%}")
    print(f"  💰 多空利差: {bull_avg - bear_avg:+.2%}")
    
    # Top 10准确率最高的股票
    print(f"\n{'─'*70}")
    print(f"🏆 准确率 Top 10 股票:")
    print(f"{'─'*70}")
    sorted_s = sorted(summaries, key=lambda x: x['overall_acc'], reverse=True)[:10]
    for s in sorted_s:
        stock_info = next((st for st in stocks if st['ts_code']==s['ts_code']), {'name':'?'})
        print(f"  {s['ts_code']} {stock_info['name']:8s}  准确率:{s['overall_acc']:.1%} "
              f"春:{s['spring_cnt']} 夏:{s['summer_cnt']} 秋:{s['autumn_cnt']} 冬:{s['winter_cnt']}")
    
    print(f"\n✅ 回测完成! 数据已存入 backtest_hengjiyuan_daily / backtest_hengjiyuan_summary")
    
    conn.close()

if __name__ == '__main__':
    main()
