#!/usr/bin/env python3
"""
全量A股评分引擎 v1.0
=====================
从 stock_basic 筛选可评分A股（排除ST/北交所/指数）
批量跑评分 → 写入 trend_score + strategy_signal
"""
import os, sys, time, math, pymysql
from db_config import get_connection
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 从 stock_basic 筛选：沪深主板+创业板+科创板，排除ST/北交所/指数
    cur.execute("""
        SELECT ts_code, name, industry, market FROM stock_basic
        WHERE market IN ('主板','创业板','科创板')
          AND (ts_code LIKE '6%%' OR ts_code LIKE '0%%' OR ts_code LIKE '3%%'
               OR ts_code LIKE '4%%' OR ts_code LIKE '8%%' OR ts_code LIKE '2%%')
          AND name NOT LIKE '%%ST%%'
          AND name NOT LIKE '%%退%%'
        ORDER BY market, ts_code
    """)
    all_stocks = cur.fetchall()
    total = len(all_stocks)
    print(f"📋 全量A股待筛选: {total}只")
    
    # Step 1: 检查K线数据完整性（至少120日）
    valid = []
    for i, s in enumerate(all_stocks):
        if (i+1) % 500 == 0:
            print(f"\r  扫描K线: {i+1}/{total}", end='', flush=True)
        cur.execute(
            "SELECT COUNT(*) AS cnt, MAX(trade_date) AS latest FROM daily_kline_qfq WHERE ts_code=%s",
            (s['ts_code'],)
        )
        r = cur.fetchone()
        if r and r['cnt'] >= 120 and r['latest']:
            valid.append(s)
    
    print(f"\n  有足够K线(≥120日): {len(valid)}只")
    
    # Step 2: 拉取最新行情（昨天收盘价）
    from score_engine import ScoreEngineV4
    engine = ScoreEngineV4()
    
    mkt = engine.get_market_context()
    trade_date = datetime.now().strftime('%Y-%m-%d')
    
    scored = 0; errors = []
    cur2 = conn.cursor()
    
    from engine.cycle_scorer import score_cycle_enhanced
    from engine.indicators import rsi, sma
    from engine.sentiment_scorer import score_sentiment
    from engine.block_weights import get_block_weights, apply_block_weights
    from engine.vmap import vmap_score, classify_signal
    from score_engine import score_chanlun_enhanced
    
    # 每批50只，分批写入减少commit压力
    batch = []
    batch_size = 50
    
    t0 = time.time()
    
    for i, s in enumerate(valid):
        code = s['ts_code']
        if (i+1) % 100 == 0 or i == 0:
            elapsed = time.time() - t0
            rate = (i+1) / elapsed if elapsed > 0 else 0
            eta = (len(valid) - i - 1) / rate if rate > 0 else 0
            print(f"\r  [{i+1}/{len(valid)}] {code} | {rate:.1f}只/s | ETA {eta:.0f}s", end='', flush=True)
        
        try:
            result = engine.score_one(code, mkt)
            if 'error' in result:
                errors.append(code + ':' + str(result.get('error','?')))
                continue
            
            # 构建写入数据
            v = round(result['v_score'], 2)
            raw = round(result['raw_score'], 2)
            cycle_s = round(result['cycle_score'], 2)
            cl = round(result['chanlun_score'], 2)
            sent_s = round(result['sentiment_score'], 2)
            conf = round(result.get('confidence', 1.0), 2)
            
            # direction
            dir_map = {'STRONG_BUY': 'LONG', 'BUY': 'LONG', 'CAUTIOUS_BUY': 'LONG',
                       'SELL': 'SHORT', 'STRONG_SELL': 'SHORT',
                       'HOLD': 'NEUTRAL', 'REV_BUY': 'LONG', 'WAIT': 'NEUTRAL'}
            direction = dir_map.get(result['signal'], 'NEUTRAL')
            
            # op_mode
            strategy = result.get('strategy', 'momentum')
            op_mode = 'attack' if (strategy == 'momentum' and v >= 35) else \
                      ('defense' if (strategy != 'momentum' and v >= 35) else 'dormant')
            
            pos = round(result.get('position_pct', 50), 2)
            close_price = result.get('close', 0)
            stop_loss = round(close_price * (1 + result.get('stop_loss_pct', -0.05)), 3) if close_price else 0
            
            reason = f"{mkt['season']}+{result.get('signal_label','?')}"
            
            sig_conf = 'high' if v >= 45 else ('medium' if v >= 30 else 'low')
            
            # 安全闸门
            gate = 0
            if mkt['confidence'] < 0.3 or mkt.get('breadth_ratio', 0.5) < 0.20:
                gate = 1
                direction = 'NEUTRAL'
                pos = min(pos, 15)
                sig_conf = 'low'
            
            batch.append((
                code, trade_date, mkt.get('regime', 'range'), v, direction,
                pos, stop_loss, reason[:200], op_mode, sig_conf,
                round(close_price * 0.98, 3) if close_price else 0, close_price,
                0, 0.0, gate, None
            ))
            
            # 同时写 trend_score
            cur2.execute("""
                INSERT INTO trend_score
                    (ts_code, trade_date, cycle_score, structure_score, emotion_score,
                     composite_score, confidence_mult, raw_score, is_calculable)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)
                ON DUPLICATE KEY UPDATE
                    cycle_score=VALUES(cycle_score),
                    structure_score=VALUES(structure_score),
                    emotion_score=VALUES(emotion_score),
                    composite_score=VALUES(composite_score),
                    raw_score=VALUES(raw_score)
            """, (code, trade_date, cycle_s, cl, sent_s, v, conf, raw))
            
            scored += 1
            
        except Exception as e2:
            errors.append(f'{code}:{e2}')
        
        # 批量写 strategy_signal
        if len(batch) >= batch_size:
            cur2.executemany("""
                INSERT INTO strategy_signal
                    (ts_code, trade_date, cycle_stage, composite_score, direction,
                     position_pct, stop_loss, reason_chain, operation_mode,
                     signal_confidence, is_calculable, entry_low, entry_high,
                     autumn_tiger, tiger_confidence, gate_triggered, safety_gate)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    composite_score=VALUES(composite_score), direction=VALUES(direction),
                    position_pct=VALUES(position_pct), signal_confidence=VALUES(signal_confidence),
                    gate_triggered=VALUES(gate_triggered)
            """, batch)
            conn.commit()
            batch = []
    
    # 最后一批
    if batch:
        cur2.executemany("""
            INSERT INTO strategy_signal
                (ts_code, trade_date, cycle_stage, composite_score, direction,
                 position_pct, stop_loss, reason_chain, operation_mode,
                 signal_confidence, is_calculable, entry_low, entry_high,
                 autumn_tiger, tiger_confidence, gate_triggered, safety_gate)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                composite_score=VALUES(composite_score), direction=VALUES(direction),
                position_pct=VALUES(position_pct), signal_confidence=VALUES(signal_confidence),
                gate_triggered=VALUES(gate_triggered)
        """, batch)
        conn.commit()
    
    engine.close()
    cur.close(); cur2.close(); conn.close()
    
    elapsed = time.time() - t0
    print(f"\n{'='*50}")
    print(f"✅ 全量评分完成!")
    print(f"  总扫描: {total}只 有效K线: {len(valid)}只 评分写入: {scored}只")
    print(f"  耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    if errors:
        print(f"  ⚠️ 错误 ({len(errors)}): {errors[:10]}...")

if __name__ == '__main__':
    main()
