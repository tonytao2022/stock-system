#!/usr/bin/env python3
"""
P6 分季评分双轨引擎 v1.0
========================
2026-06-01 定案

设计者: Tony + Main + MAY

架构:
  season_engine.py → 季节+regime判定 (数出一源)
  ┣━ 轨道A: 动量评分 (夏季/春季/偏多混沌*)
  ┃   缠论趋势分×0.7 + 动量因子×0.3
  ┃   P3信号基於轨道排序
  ┗━ 轨道B: 均值回归评分 (秋季/冬季/混沌*)
       缠论结构×0.4 + 超跌深度×0.3 + ATR波动×0.2 + 秋老虎+15分
       P3信号基於轨道排序

  双轨排序: 动量轨道×1.3校准后合并排序 (Tony决策B)
  防跳变: 持有一天以上才能切换轨道 (Tony决策A)
  *混沌子态分配: 待MAY确认后补充
"""

import sys, os, math, json
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from season_engine import SeasonEngine
from score_engine import score_chanlun_enhanced

# ============================================================
# 核心数据模型
# ============================================================

class MarketContext:
    """市场上下文——季节判定器输出的一次封装"""
    def __init__(self, judge_result: dict):
        self.season = judge_result.get('market_season', 'summer')
        self.regime = judge_result.get('market_regime', 'range')
        self.confidence = judge_result.get('market_confidence', 0.5)
        self.scoring_strategy = judge_result.get('market_scoring_strategy', 'momentum')
        self.trade_date = judge_result.get('trade_date', str(date.today()))
        self.raw = judge_result

    def is_momentum_track(self) -> bool:
        """
        是否走动量评分轨道
        
        P6定版 (MAY方案, 2026-06-01):
        动量轨道: 偏多混沌(任意regime) | 中性混沌+非熊市 | 春/夏
        回归轨道: 真秋/冬 | 偏空混沌 | 中性混沌+熊市
        """
        scoring = self.raw.get('scoring_strategy', 'momentum')
        return scoring == 'momentum'

    def momentum_multiplier(self) -> float:
        """
        动量轨道x1.3校准
        注意: 夏普高的秋季/混沌虽然走B轨,但B轨本身权重分配已不同
        """
        return 1.3 if self.is_momentum_track() else 1.0


# ============================================================
# 轨道A: 动量评分
# ============================================================

def track_momentum(ts_code: str, ctx: MarketContext) -> Dict:
    """
    动量轨道评分
    权重: 缠论趋势分×0.7 + 动量因子×0.3
    
    Returns:
        {'track': 'momentum', 'score': float, 'details': {...}}
    """
    details = {'track': 'momentum'}
    
    # 1. 从DB获取基础数据
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # 获取最新K线
        cur.execute("""
            SELECT d.close, d.high, d.low, d.vol, d.amount, d.trade_date,
                   d.volume_ratio, d.turnover_rate,
                   t.ma_5, t.ma_10, t.ma_20, t.ma_60, t.ma_120, t.ma_250,
                   t.rsi_12 as rsi_14, t.macd_dif, t.macd_dea, t.atr_14,
                   t.boll_upper, t.boll_mid, t.boll_lower
            FROM daily_kline d
            LEFT JOIN technical_indicator t ON d.ts_code=t.ts_code AND d.trade_date=t.trade_date
            WHERE d.ts_code=%s AND d.trade_date <= %s
            ORDER BY d.trade_date DESC LIMIT 120
        """, (ts_code, ctx.trade_date))
        rows = cur.fetchall()
        
        if not rows or len(rows) < 20:
            cur.close()
            return {'track': 'momentum', 'score': 50, 'reason': 'insufficient_data'}
        
        latest = rows[0]
        
        # 2. 读取缠论结构评分
        cur.execute("""
            SELECT structure_score, buy_sell_point, beichi_type, beichi_strength,
                   zoushi_type, zoushi_stage, autumn_tiger, tiger_confidence
            FROM chanlun_structure 
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        cl_row = cur.fetchone()
        cur.close()
        
        # 3. 计算缠论趋势分 (核心因子, 70%)
        trend_score = 50  # 默认
        if cl_row and cl_row.get('structure_score') is not None:
            ss = float(cl_row['structure_score'])
            if ss >= 75: trend_score = 85
            elif ss >= 60: trend_score = 70
            elif ss >= 40: trend_score = 55
            else: trend_score = 35
            
            # 买卖点加成
            bs = cl_row.get('buy_sell_point', 'none')
            bs_boost = {'buy3': 15, 'buy2': 8, 'buy1': 3, 'sell3': -15, 'sell2': -8, 'sell1': -3}.get(bs, 0)
            trend_score = max(0, min(100, trend_score + bs_boost))
            
            # 背驰增强
            bt = cl_row.get('beichi_type', 'none')
            if bt == 'bottom' and float(cl_row.get('beichi_strength', 0) or 0) > 40:
                trend_score = min(100, trend_score + 10)
            elif bt == 'top' and float(cl_row.get('beichi_strength', 0) or 0) > 40:
                trend_score = max(0, trend_score - 10)
        else:
            # 无缠论数据: 用MA位置代理
            close = float(latest['close'])
            ma20 = float(latest.get('ma20', 0) or 0)
            ma60 = float(latest.get('ma60', 0) or 0)
            if ma20 > 0 and ma60 > 0:
                if close > ma20 and ma20 > ma60: trend_score = 65
                elif close > ma20: trend_score = 55
                elif close > ma60: trend_score = 45
                else: trend_score = 35
        
        # 4. 计算动量因子 (30%)
        closes = [float(r['close']) for r in reversed(rows)]
        n = len(closes)
        
        # momentum的成分: 短期收益率 + RSI动量 + 加速确认
        momentum = 50
        if n >= 20:
            # 5日/10日/20日涨幅
            r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
            r10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 else 0
            r20 = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0
            
            # 连续上涨天数
            cons_up = 0
            for i in range(-5, 0):
                if closes[i] > closes[i-1]:
                    cons_up += 1
                else:
                    cons_up = 0
            
            # RSI动量
            rsi_val = float(latest.get('rsi_14', 50) or 50)
            
            # 动量综合计算
            score = 50
            score += max(-15, min(15, r5 * 150))      # -15~+15
            score += max(-10, min(10, r10 * 80))      # -10~+10
            score += max(-8, min(8, r20 * 50))        # -8~+8
            score += min(8, cons_up * 2)               # 0~+8（连续上涨确认）
            score += (rsi_val - 50) * 0.5              # RSI修正 -25~+25
            
            # 量能确认: 放量上涨=真突破, 缩量上涨=警惕
            vr = float(latest.get('volume_ratio', 1) or 1)
            if r5 > 0.02 and vr > 1.5:
                score += 5  # 放量上涨确认
            elif r5 > 0.02 and vr < 0.7:
                score -= 5  # 缩量上涨存疑
            
            momentum = max(0, min(100, score))
        
        # 5. 综合: 缠论趋势分×0.7 + 动量×0.3
        final_score = trend_score * 0.70 + momentum * 0.30
        final_score = max(0, min(100, round(final_score, 1)))
        
        details['chanlun_trend'] = trend_score
        details['momentum_raw'] = momentum
        details['chanlun_row'] = bool(cl_row)
        
        return {'track': 'momentum', 'score': final_score, 'details': details}
        
    except Exception as e:
        return {'track': 'momentum', 'score': 50, 'reason': str(e)}


# ============================================================
# 轨道B: 均值回归评分
# ============================================================

def track_reversion(ts_code: str, ctx: MarketContext) -> Dict:
    """
    均值回归轨道评分
    权重: 缠论结构×0.4 + 超跌深度×0.3 + ATR波动×0.2 + 秋老虎+15
    
    Returns:
        {'track': 'reversion', 'score': float, 'details': {...}}
    """
    details = {'track': 'reversion'}
    
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # 获取K线数据
        # 获取K线数据
        cur.execute("""
            SELECT d.close, d.high, d.low, d.vol, d.amount, d.trade_date,
                   d.volume_ratio, d.turnover_rate,
                   t.ma_5, t.ma_10, t.ma_20, t.ma_60, t.ma_120, t.ma_250,
                   t.rsi_12 as rsi_14, t.atr_14,
                   t.boll_upper, t.boll_mid, t.boll_lower
            FROM daily_kline d
            LEFT JOIN technical_indicator t ON d.ts_code=t.ts_code AND d.trade_date=t.trade_date
            WHERE d.ts_code=%s AND d.trade_date <= %s
            ORDER BY d.trade_date DESC LIMIT 250
        """, (ts_code, ctx.trade_date))
        rows = cur.fetchall()
        
        if not rows or len(rows) < 60:
            cur.close()
            return {'track': 'reversion', 'score': 50, 'reason': 'insufficient_data'}
        
        latest = rows[0]
        close = float(latest['close'])
        
        # 读取缠论结构
        cur.execute("""
            SELECT structure_score, buy_sell_point, beichi_type, beichi_strength,
                   zoushi_type, zoushi_stage, autumn_tiger, tiger_confidence
            FROM chanlun_structure 
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        cl_row = cur.fetchone()
        
        # 读取趋势评分中的波动率
        cur.execute("""
            SELECT volatility_score
            FROM trend_score
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        trend_row = cur.fetchone()
        cur.close()
        
        # ===== 因子1: 缠论结构 (40%) =====
        structure = 50
        chanlun_exists = False
        if cl_row and cl_row.get('structure_score') is not None:
            chanlun_exists = True
            ss = float(cl_row['structure_score'])
            bs = cl_row.get('buy_sell_point', 'none')
            bt = cl_row.get('beichi_type', 'none')
            bstr = float(cl_row.get('beichi_strength', 0) or 0)
            at = bool(cl_row.get('autumn_tiger', 0))
            
            if ss >= 75: structure = 80
            elif ss >= 60: structure = 65
            elif ss >= 40: structure = 50
            else: structure = 35
            
            # 底背离=均值回归买点
            if bt == 'bottom' and bstr > 40:
                structure = min(100, structure + 15)
            elif bs in ('buy2', 'buy3'):
                structure = min(100, structure + 10)
            
            details['chanlun_structure'] = structure
            details['autumn_tiger'] = at
            
            # 秋老虎加分
            if at:
                structure = min(100, structure + 15)
        
        # ===== 因子2: 超跌深度 (30%) =====
        oversold = 50
        closes = [float(r['close']) for r in reversed(rows)]
        n = len(closes)
        
        if n >= 120:
            ma120 = float(latest.get('ma120', 0) or 0)
            ma250 = float(latest.get('ma250', 0) or 0)
            high_52w = float(latest.get('high_52w', 0) or 0)
            low_52w = float(latest.get('low_52w', 0) or 0)
            rsi_val = float(latest.get('rsi_14', 50) or 50)
            
            # 价格相对均线的偏离度
            if ma120 > 0:
                dev_ma120 = (close - ma120) / ma120
            else:
                dev_ma120 = 0
            
            if ma250 > 0:
                dev_ma250 = (close - ma250) / ma250
            else:
                dev_ma250 = 0
            
            # 位置区间: 52周高低
            if high_52w > low_52w:
                pos_52w = (close - low_52w) / (high_52w - low_52w)
            else:
                pos_52w = 0.5
            
            # 超跌评分:
            # 价格低于MA120=超跌特征, 越低越好
            # 价格在52周低位=超跌
            score = 50
            if dev_ma120 < -0.05: score += 5
            if dev_ma120 < -0.10: score += 8
            if dev_ma120 < -0.15: score += 5
            if dev_ma120 > 0.10: score -= 8  # 远离均线买入成本高
            
            if dev_ma250 < -0.05: score += 5
            if dev_ma250 < -0.15: score += 8
            
            # RSI极端: 超卖区加分
            if rsi_val < 25: score += 15
            elif rsi_val < 30: score += 10
            elif rsi_val < 40: score += 5
            elif rsi_val > 70: score -= 10
            elif rsi_val > 60: score -= 5
            
            # 52周低位
            if pos_52w < 0.20: score += 10
            elif pos_52w < 0.35: score += 5
            elif pos_52w > 0.80: score -= 8
            
            oversold = max(0, min(100, score))
            
            details['dev_ma120'] = round(dev_ma120, 3)
            details['dev_ma250'] = round(dev_ma250, 3)
            details['pos_52w'] = round(pos_52w, 3)
            details['rsi'] = rsi_val
        
        # ===== 因子3: ATR波动预警 (20%) =====
        volatility = 50
        if n >= 20:
            highs = [float(r['high']) for r in reversed(rows)]
            lows = [float(r['low']) for r in reversed(rows)]
            
            # 计算ATR
            tr_list = []
            for i in range(1, min(15, n)):
                tr = max(
                    highs[-i] - lows[-i],
                    abs(highs[-i] - closes[-i-1]),
                    abs(lows[-i] - closes[-i-1])
                )
                tr_list.append(tr)
            atr_val = sum(tr_list) / len(tr_list) if tr_list else 0
            atr_pct = atr_val / close if close > 0 else 0
            
            # 低波动=布局窗口, 高波动=警惕
            if atr_pct < 0.015:   # 极低波动
                volatility = 70
            elif atr_pct < 0.025: # 低波动
                volatility = 60
            elif atr_pct < 0.040: # 正常
                volatility = 50
            elif atr_pct < 0.060: # 高波动
                volatility = 35
            else:                 # 极高波动
                volatility = 20
            
            details['atr_pct'] = round(atr_pct, 4)
        
        # ===== 综合 =====
        final_score = structure * 0.40 + oversold * 0.30 + volatility * 0.20
        
        # 秋老虎: 已经在structure中加了15分
        # 加上情绪辅助(微调, L3区分度不足所以权重低)
        # 预留: 后续可加情绪因子×0.10
        
        final_score = max(0, min(100, round(final_score, 1)))
        
        details['structure_factor'] = structure
        details['oversold_factor'] = oversold
        details['volatility_factor'] = volatility
        
        return {'track': 'reversion', 'score': final_score, 'details': details}
        
    except Exception as e:
        return {'track': 'reversion', 'score': 50, 'reason': str(e)}


# ============================================================
# 双轨评分主入口
# ============================================================

def score_stock(ts_code: str, ctx: MarketContext) -> Dict:
    """
    双轨评分入口
    根据市场上下文决定走哪条轨道
    
    Returns:
        {'ts_code': ..., 'track': 'momentum'|'reversion', 
         'score': float, 'details': {...}}
    """
    if ctx.is_momentum_track():
        result = track_momentum(ts_code, ctx)
    else:
        result = track_reversion(ts_code, ctx)
    
    result['ts_code'] = ts_code
    return result


def _build_calib_map(original_scores: List[float]) -> Dict[int, float]:
    """
    建立百分位映射校准表
    将P6原始分的排序位置映射到合理的校准分区间
    
    校准分目标分布（从v4历史分布验证）:
      P5=10, P10=15, P25=22, P50=30, P75=40, P90=50, P95=60, P100=80
    避免顶到100（丧失区分度）
    """
    n = len(original_scores)
    if n == 0: return {}
    sorted_scores = sorted(original_scores)
    targets = {
        5: 10, 10: 15, 15: 18, 20: 20, 25: 22, 30: 24,
        35: 26, 40: 28, 45: 29, 50: 30, 55: 32,
        60: 34, 65: 36, 70: 38, 75: 40, 80: 44,
        85: 48, 90: 50, 93: 55, 95: 60, 97: 68, 99: 75, 100: 80
    }
    calib_map = {}
    for pct, target in targets.items():
        idx = min(int(n * pct / 100), n - 1)
        raw = sorted_scores[idx]
        calib_map[raw] = target
    
    # 补全首尾
    if sorted_scores:
        calib_map[sorted_scores[0]] = max(0, targets.get(5, 10) - 5)
        calib_map[sorted_scores[-1]] = 80
    
    return calib_map


def _apply_calibration(raw_score: float, calib_map: Dict[int, float]) -> float:
    """
    对单个原始分应用百分位映射校准
    对映射表中每个断点做分段线性插值
    """
    if not calib_map:
        return max(0, min(100, raw_score))
    
    sorted_raws = sorted(calib_map.keys())
    
    # 边界处理
    if raw_score <= sorted_raws[0]:
        return float(calib_map[sorted_raws[0]])
    if raw_score >= sorted_raws[-1]:
        return float(calib_map[sorted_raws[-1]])
    
    # 分段线性插值
    for i in range(len(sorted_raws) - 1):
        lo_raw = sorted_raws[i]
        hi_raw = sorted_raws[i + 1]
        if lo_raw <= raw_score <= hi_raw:
            lo_cal = calib_map[lo_raw]
            hi_cal = calib_map[hi_raw]
            if hi_raw == lo_raw:
                return float(lo_cal)
            ratio = (raw_score - lo_raw) / (hi_raw - lo_raw)
            return round(lo_cal + ratio * (hi_cal - lo_cal), 1)
    
    return round(raw_score, 1)


def calibrate_scores(results: List[Dict]) -> List[Dict]:
    """
    对批量评分结果执行百分位映射校准
    
    两步:
    1. 根据所有原始分建立百分位映射表
    2. 对每个结果应用校准
    """
    raw_scores = [r['score'] for r in results if r.get('score') is not None]
    calib_map = _build_calib_map(raw_scores)
    
    for r in results:
        r['calibrated_score'] = _apply_calibration(r['score'], calib_map)
    
    results.sort(key=lambda x: x['calibrated_score'], reverse=True)
    return results


def batch_score(ts_codes: List[str], ctx: MarketContext) -> List[Dict]:
    """
    批量评分——全市场或监控池
    
    策略:
    1. 全部评分（不分轨道）
    2. 百分位映射校准（替代固定乘数×1.3）
    
    Returns:
        排序后的评分列表
    """
    results = []
    
    for ts_code in ts_codes:
        r = score_stock(ts_code, ctx)
        results.append(r)
    
    # 百分位映射校准（统一校准，不分轨道）
    calibrate_scores(results)
    
    return results


# ============================================================
# 防跳变 —— 轨道切换延迟一天
# ============================================================

class TrackHistory:
    """
    轨道切换防跳变
    原则: 持有一天以上才能切换轨道 (Tony决策A)
    """
    
    def __init__(self, db_table: str = 'strategy_signal'):
        self.db_table = db_table
        self._cache: Dict[str, Dict] = {}
    
    def get_previous_track(self, ts_code: str, date_str: str = None) -> Optional[str]:
        """获取上一个交易日的轨道"""
        from db_config import get_connection
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute(f"""
            SELECT track, trade_date FROM {self.db_table}
            WHERE ts_code=%s AND trade_date < %s
            ORDER BY trade_date DESC LIMIT 1
        """, (ts_code, date_str or date.today().isoformat()))
        row = cur.fetchone()
        cur.close()
        
        if row:
            return row['track']
        return None
    
    def should_switch(self, ts_code, new_track, current_date_str):
        """
        判定是否允许切换轨道
        
        规则:
        - 如果之前没有轨道记录 → 直接切换
        - 如果和之前相同 → 无需切换
        - 如果不同 → 检查切换间隔 > 1日 → 允许切换
        """
        prev = self.get_previous_track(ts_code, current_date_str)
        if prev is None:
            return True  # 首次,允许
        if prev == new_track:
            return True  # 相同轨道, 继续
        # 不同轨道: 需要间隔超过1天
        # 由batch_pipeline调度决定(只会每日计算一次)
        return False


# ============================================================
# 主管道
# ============================================================

def daily_pipeline(mode: str = 'watch_pool'):
    """
    每日评分管道
    
    Args:
        mode: 'watch_pool' | 'full_market'
    
    流程:
    1. SeasonEngine判断市场季节
    2. 获取评分池名单
    3. 双轨评分
    4. 校准+排序
    5. 入库
    """
    
    # 1. 市场季节判定 (数出一源)
    engine = SeasonEngine()
    judge_result = engine.judge_market_season()
    ctx = MarketContext(judge_result)
    
    print(f"📊 市场状态: {ctx.season}/{ctx.regime} | "
          f"策略: {ctx.scoring_strategy} | "
          f"轨道: {'动量' if ctx.is_momentum_track() else '均值回归'}")
    
    # 2. 获取评分池
    conn = get_connection()
    cur = conn.cursor()
    
    if mode == 'watch_pool':
        cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    else:
        cur.execute("SELECT DISTINCT ts_code FROM daily_kline WHERE trade_date=%s", 
                    (ctx.trade_date,))
    
    ts_codes = [row['ts_code'] for row in cur.fetchall()]
    tot = len(ts_codes)
    print(f"📈 评分池: {tot} 只股票")
    
    # 3. 批量评分
    results = batch_score(ts_codes, ctx)
    
    # 4. 入库+打印top
    saved, skipped = 0, 0
    for i, r in enumerate(results):
        try:
            cur.execute("""
                INSERT INTO strategy_signal 
                    (ts_code, trade_date, track, composite_score, calibrated_score, 
                     scoring_strategy, direction)
                VALUES (%s, %s, %s, %s, %s, %s, 'dual_track_v1')
                ON DUPLICATE KEY UPDATE
                    track=VALUES(track), composite_score=VALUES(composite_score),
                    calibrated_score=VALUES(calibrated_score),
                    scoring_strategy=VALUES(scoring_strategy)
            """, (r['ts_code'], ctx.trade_date, r['track'],
                  r['score'], r['calibrated_score'],
                  'momentum' if r['track'] == 'momentum' else 'reversion'))
            saved += 1
            if (i+1) % 50 == 0:
                print(f"  💾 已入库 {i+1}/{tot}")
        except Exception:
            skipped += 1
    
    conn.commit()
    cur.close()
    conn.close()
    
    # 5. Top排序结果
    print(f"\n{'='*60}")
    print(f"🏆 P6 双轨评分 TOP 20 ({ctx.trade_date})")
    print(f"   市场: {ctx.season}/{ctx.regime} | 轨道: {'动量(A)' if ctx.is_momentum_track() else '回归(B)'}")
    print(f"{'='*60}")
    for i, r in enumerate(results[:20]):
        track_icon = '🚀' if r['track'] == 'momentum' else '🔄'
        print(f"{i+1:2d}. {track_icon} {r['ts_code']} | "
              f"分:{r['score']:5.1f} | 校准分:{r['calibrated_score']:5.1f} | "
              f"轨道:{r['track']}")
    
    print(f"\n📦 已入库: {saved} | 跳过: {skipped} | 评分池: {tot}")
    return results


# ============================================================
# 入口
# ============================================================

if __name__ == '__main__':
    results = daily_pipeline(mode='watch_pool')
