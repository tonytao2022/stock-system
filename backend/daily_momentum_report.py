from db_config import get_connection
#!/usr/bin/env python3
"""
每日动量分层报告 v2.0
======================
从 trend_score + strategy_signal 读取数据
写入 momentum_daily_index 表
"""
import pymysql, statistics
from datetime import date

def main():
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS momentum_daily_index (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            trade_date DATE NOT NULL UNIQUE,
            season VARCHAR(20), strategy VARCHAR(20),
            total_count INT DEFAULT 0,
            top_quarter_avg_score DECIMAL(5,2) DEFAULT 0,
            bottom_quarter_avg_score DECIMAL(5,2) DEFAULT 0,
            top_bottom_spread DECIMAL(5,2) DEFAULT 0,
            buy_count INT DEFAULT 0, sell_count INT DEFAULT 0, neutral_count INT DEFAULT 0,
            avg_position_pct DECIMAL(5,2) DEFAULT 0,
            momentum_env VARCHAR(10),
            avg_roc20 DECIMAL(5,2) DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    cur.execute("SELECT MAX(trade_date) as d FROM trend_score")
    r = cur.fetchone()
    trade_date = r['d'] or date.today()

    # 评分数据
    cur.execute("SELECT ts_code, composite_score FROM trend_score WHERE trade_date=%s", (trade_date,))
    scores = cur.fetchall()
    total = len(scores)
    if total < 10:
        print(f"⚠️ 数据不足: {total}条 ({trade_date})")
        conn.close()
        return

    # 信号数据
    cur.execute("SELECT direction, position_pct FROM strategy_signal WHERE trade_date=%s", (trade_date,))
    signals = cur.fetchall()

    # 季节
    cur.execute("SELECT season, scoring_strategy FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
    ss = cur.fetchone()
    season = ss['season'] if ss else 'unknown'
    strategy = ss['scoring_strategy'] if ss else 'momentum'

    # 动量分层
    all_scores = sorted([float(s['composite_score'] or 0) for s in scores], reverse=True)
    q1 = all_scores[:max(1, len(all_scores)//4)]
    q4 = all_scores[-max(1, len(all_scores)//4):]
    top_avg = sum(q1)/len(q1)
    bottom_avg = sum(q4)/len(q4)
    spread = round(top_avg - bottom_avg, 2)

    # 信号分布
    buy = sum(1 for s in signals if s['direction'] == 'LONG')
    sell = sum(1 for s in signals if s['direction'] == 'SHORT')
    neutral = sum(1 for s in signals if s['direction'] == 'NEUTRAL')
    avg_pos = sum(float(s['position_pct'] or 0) for s in signals) / max(len(signals), 1)

    # 平均ROC20: 从daily_kline逐只算
    codes = [s['ts_code'] for s in scores]
    roc20_list = []
    for c in codes[:50]:  # 前50只足够代表
        cur.execute("""
            SELECT trade_date, close FROM daily_kline_qfq 
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 21
        """, (c,))
        rs = cur.fetchall()
        if len(rs) >= 21:
            roc = (float(rs[0]['close']) - float(rs[-1]['close'])) / float(rs[-1]['close']) * 100
            roc20_list.append(roc)
    avg_roc20 = round(statistics.mean(roc20_list), 2) if roc20_list else 0

    # 动量环境
    if spread > 15 and avg_roc20 > 2:
        momentum_env = 'positive'
    elif spread < 5 and avg_roc20 < -2:
        momentum_env = 'negative'
    else:
        momentum_env = 'neutral'

    # 写入
    cur.execute("""
        INSERT INTO momentum_daily_index
            (trade_date, season, strategy, total_count,
             top_quarter_avg_score, bottom_quarter_avg_score, top_bottom_spread,
             buy_count, sell_count, neutral_count, avg_position_pct,
             momentum_env, avg_roc20)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            season=VALUES(season), strategy=VALUES(strategy),
            total_count=VALUES(total_count),
            top_quarter_avg_score=VALUES(top_quarter_avg_score),
            bottom_quarter_avg_score=VALUES(bottom_quarter_avg_score),
            top_bottom_spread=VALUES(top_bottom_spread),
            buy_count=VALUES(buy_count), sell_count=VALUES(sell_count),
            neutral_count=VALUES(neutral_count),
            avg_position_pct=VALUES(avg_position_pct),
            momentum_env=VALUES(momentum_env), avg_roc20=VALUES(avg_roc20)
    """, (trade_date, season, strategy, total,
          round(top_avg,2), round(bottom_avg,2), spread,
          buy, sell, neutral, round(avg_pos,2),
          momentum_env, avg_roc20))
    conn.commit()

    env_cn = {'positive':'🟢正向','negative':'🔴负向','neutral':'➡️中性'}
    print(f"✅ 每日动量报告 ({trade_date})")
    print(f"   季节: {season}  策略: {strategy}  评分: {total}条")
    print(f"   Top25%: {top_avg:.1f}  Bottom25%: {bottom_avg:.1f}  利差: {spread:.1f}")
    print(f"   信号: 🟢{buy} / ⏸️{neutral} / 🔴{sell}  均仓位: {avg_pos:.0f}%")
    print(f"   平均ROC20: {avg_roc20:+.2f}%  动量环境: {env_cn[momentum_env]}")
    conn.close()

if __name__ == '__main__':
    main()
