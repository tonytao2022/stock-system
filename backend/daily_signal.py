#!/usr/bin/env python3
"""
每日可操作信号生成器 v1.0
========================
基于season_engine + score_engine，每日收盘后生成:

1. 市场全景: 季节 + 状态 + 置信度 + 仓位建议
2. Top N推荐: 按当前评分策略排序的推荐标的
3. 写入数据库: season_daily_signal 表

用法:
  python3 daily_signal.py           # 最新交易日
  python3 daily_signal.py --date 2026-05-25
  python3 daily_signal.py --top 10  # Top10推荐

设计者: May
"""

import sys, os, json, pymysql
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from season_engine import SeasonEngine, DB_CONFIG
from score_engine import ScoreEngine

CREATE_SIGNAL_TABLE = """
CREATE TABLE IF NOT EXISTS season_daily_signal (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date DATE NOT NULL COMMENT '交易日期',
    market_season VARCHAR(20) NOT NULL COMMENT '市场季节',
    market_regime VARCHAR(10) COMMENT '市场状态 bull/bear/range',
    scoring_strategy VARCHAR(20) COMMENT '评分策略 momentum/reversion',
    raw_score DECIMAL(6,2) COMMENT '综合得分',
    confidence DECIMAL(5,3) COMMENT '置信度',
    position_advice VARCHAR(200) COMMENT '仓位建议',
    top_picks_json JSON COMMENT 'Top推荐标的JSON',
    full_report TEXT COMMENT '完整分析报告',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日操作信号表';
"""


def init_db():
    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute(CREATE_SIGNAL_TABLE)
    conn.commit()
    cur.close()
    conn.close()
    print("✅ season_daily_signal 表已就绪")


def generate_signal(target_date: date = None, top_n: int = 5):
    """
    生成每日可操作信号

    Returns:
        {
            'trade_date': '2026-05-25',
            'market_season': 'chaos_spring',
            'market_regime': 'bull',
            'scoring_strategy': 'momentum',
            'raw_score': 2.0,
            'confidence': 0.74,
            'position_advice': '谨慎做多, 仓位≤50%',
            'top_picks': [{ts_code, name, score, strategy, close}],
            'full_report': '...'
        }
    """
    season_engine = SeasonEngine(use_market_breadth=False)
    score_engine = ScoreEngine()

    try:
        # 1. 市场季节
        if target_date:
            market = season_engine.judge_market_season(target_date)
        else:
            market = season_engine.get_realtime_season()

        # 2. 评分股票池
        loader = score_engine.loader
        stock_codes = loader.get_stock_pool_codes()
        scores = score_engine.score_stocks_batch(stock_codes, target_date)

        # 3. 加载股票名称
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ','.join(['%s'] * len(stock_codes))
        cur.execute(f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ({placeholders})", stock_codes)
        name_map = {r['ts_code']: r['name'] for r in cur.fetchall()}
        cur.close()
        conn.close()

        # 4. Top推荐
        top_picks = []
        for s in scores[:top_n]:
            top_picks.append({
                'ts_code': s['ts_code'],
                'name': name_map.get(s['ts_code'], '?'),
                'total_score': s['total_score'],
                'strategy': s['strategy'],
                'close': s.get('close', 0),
            })

        # 5. 仓位建议
        season = market['market_season']
        confidence = market['market_confidence']
        position = _get_position_advice(season, confidence)

        # 6. 完整报告
        report = _build_report(market, top_picks, position)

        result = {
            'trade_date': _extract_date(market) or str(date.today()),
            'market_season': season,
            'market_regime': market.get('market_regime', 'range'),
            'scoring_strategy': market.get('market_scoring_strategy', 'momentum'),
            'raw_score': market['raw_score'],
            'confidence': confidence,
            'position_advice': position,
            'top_picks': top_picks,
            'full_report': report,
        }

        return result

    finally:
        season_engine.close()
        score_engine.close()


def save_signal(signal: dict):
    """保存信号到数据库"""
    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO season_daily_signal 
            (trade_date, market_season, market_regime, scoring_strategy,
             raw_score, confidence, position_advice, top_picks_json, full_report)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE 
            market_season=VALUES(market_season),
            market_regime=VALUES(market_regime),
            scoring_strategy=VALUES(scoring_strategy),
            raw_score=VALUES(raw_score),
            confidence=VALUES(confidence),
            position_advice=VALUES(position_advice),
            top_picks_json=VALUES(top_picks_json),
            full_report=VALUES(full_report)
    """, (
        signal['trade_date'],
        signal['market_season'],
        signal['market_regime'],
        signal['scoring_strategy'],
        signal['raw_score'],
        signal['confidence'],
        signal['position_advice'],
        json.dumps(signal['top_picks'], ensure_ascii=False),
        signal['full_report'],
    ))
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ 信号已保存 ({signal['trade_date']})")


def _extract_date(market: dict) -> str:
    """从market结果中提取有效日期"""
    td = market.get('trade_date')
    if td and str(td) != 'None':
        return str(td)
    # fallback: 查数据库最新交易日
    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM daily_kline WHERE ts_code='000300.SH'")
    row = cur.fetchone()
    cur.close(); conn.close()
    return str(row[0]) if row else str(date.today())


def _get_position_advice(season: str, confidence: float) -> str:
    cmap = {
        'spring': f'进攻期 → 仓位80-100% (置信度{confidence:.0%})',
        'summer': f'持有期 → 仓位50-80% (置信度{confidence:.0%})',
        'chaos_spring': f'弱春偏多 → 仓位40-60%, 选强势股 (置信度{confidence:.0%})',
        'chaos': f'观望期 → 仓位≤30%, 等待方向 (置信度{confidence:.0%})',
        'chaos_autumn': f'弱秋偏空 → 仓位20-30%, 可考虑防守标的 (置信度{confidence:.0%})',
        'autumn': f'防守期 → 仓位10-20%或空仓 (置信度{confidence:.0%})',
        'winter': f'休眠期 → 空仓或≤10% (置信度{confidence:.0%})',
    }
    return cmap.get(season, f'未定义 → 建议观望 (置信度{confidence:.0%})')


def _build_report(market: dict, top_picks: list, position: str) -> str:
    season_names = {
        'spring': '🌸 春(进攻)', 'summer': '☀️ 夏(持有)',
        'autumn': '🍂 秋(防守)', 'winter': '❄️ 冬(休眠)',
        'chaos': '🌪️ 混沌(观望)', 'chaos_spring': '🌤️ 弱春(偏多)',
        'chaos_autumn': '🌥️ 弱秋(偏空)',
    }
    regime_names = {'bull': '🐂 牛市', 'bear': '🐻 熊市', 'range': '📊 震荡'}

    lines = [
        f"📅 {market['trade_date']} 恒纪元每日信号",
        "=" * 50,
        f"市场季节: {season_names.get(market['market_season'], market['market_season'])}",
        f"市场状态: {regime_names.get(market['market_regime'], market['market_regime'])}",
        f"评分策略: {market.get('market_scoring_strategy', '?')}",
        f"综合得分: {market['raw_score']:+.1f} | 置信度: {market['market_confidence']:.0%}",
        f"仓位建议: {position}",
        "",
        f"🏆 Top {len(top_picks)} 推荐标的:",
    ]

    for i, p in enumerate(top_picks, 1):
        lines.append(
            f"  {i}. {p['ts_code']} {p['name']:8s} "
            f"评分={p['total_score']:.1f} 策略={p['strategy']} 收盘={p['close']}"
        )

    lines.append("")
    lines.append("⚠️ 风险提示: 本信号由量化模型生成，仅供参考，不构成投资建议。")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='每日操作信号生成器')
    parser.add_argument('--date', type=str, help='指定日期 YYYY-MM-DD')
    parser.add_argument('--top', type=int, default=5, help='Top N 推荐')
    parser.add_argument('--init-db', action='store_true', help='初始化数据库表')
    parser.add_argument('--save', action='store_true', default=True, help='保存到数据库')
    args = parser.parse_args()

    if args.init_db:
        init_db()
        return

    target = None
    if args.date:
        target = datetime.strptime(args.date, '%Y-%m-%d').date()

    init_db()  # 确保表存在
    signal = generate_signal(target, args.top)

    print(signal['full_report'])

    if args.save:
        save_signal(signal)


if __name__ == '__main__':
    main()
