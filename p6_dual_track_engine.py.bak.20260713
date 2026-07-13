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
       缠论结构×0.40 + 超跌深度×0.25 + ATR波动×0.10 + 资金因子×0.15 + 秋老虎+10分
       P3信号基於轨道排序

  V4: 动量权重从70/30改为50/25/25(缠论/动量/资金), 回归加入资金因子
  双轨排序: 动量轨道×1.3校准后合并排序 (Tony决策B)
  防跳变: 持有一天以上才能切换轨道 (Tony决策A)
  *混沌子态分配: 待MAY确认后补充
"""

import sys, os, math, json
import numpy as np
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from season_engine import SeasonEngine
# score_chanlun_enhanced 已内联到本文件末尾（见底部），不再依赖 score_engine

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

    def get_hs300_trend(self) -> float:
        """获取沪深300近5日涨幅，判断大盘强度"""
        try:
            from db_config import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT close FROM daily_kline_qfq 
                WHERE ts_code='000300.SH' AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 5
            """, (self.trade_date,))
            rows = [float(r['close']) for r in cur.fetchall()]
            cur.close(); conn.close()
            if len(rows) >= 5:
                return (rows[0] - rows[-1]) / rows[-1]
            return 0.0
        except:
            return 0.0


# ============================================================
# 轨道A: 动量评分
# ============================================================

def _calc_vol_ratio(ts_code: str, trade_date: str) -> float:
    """计算量比：当日vol / 前20日均vol"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT k.vol / NULLIF(ma.avg_vol, 0) as vol_ratio
            FROM daily_kline_qfq k
            JOIN (
                SELECT AVG(vol) as avg_vol FROM daily_kline_qfq 
                WHERE ts_code=%s AND trade_date < %s AND trade_date >= DATE_SUB(%s, INTERVAL 20 DAY)
            ) ma ON 1=1
            WHERE k.ts_code=%s AND k.trade_date=%s
        """, (ts_code, trade_date, trade_date, ts_code, trade_date))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row['vol_ratio'] is not None:
            return float(row['vol_ratio'])
    except:
        pass
    return 1.0


def _calc_moneyflow_score(ts_code: str, trade_date: str) -> tuple:
    """计算资金因子 (适配 stock_db_v2 简化版 money_flow 表)
    Returns:
        (moneyflow_score, net_mf_amount, lg_ratio)
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT SUM(main_net) as mf_5d,
                   SUM(net_value) as net_val
            FROM money_flow
            WHERE ts_code=%s AND trade_date <= %s AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
        """, (ts_code, trade_date, trade_date))
        row = cur.fetchone()
        cur.close(); conn.close()
        
        if row and row['mf_5d'] is not None:
            mf_5d = float(row['mf_5d'])
            
            if mf_5d > 50000000:   # 5千万以上
                mf_score = 80
            elif mf_5d > 0:
                mf_score = 60
            elif mf_5d > -50000000:
                mf_score = 40
            else:
                mf_score = 20
            
            return mf_score, mf_5d, 0
    except:
        pass
    return 50, 0, 0


def track_momentum(ts_code: str, ctx: MarketContext) -> Dict:
    """
    动量轨道评分
    V4权重: 缠论趋势分×0.50 + 动量因子×0.25 + 资金因子×0.25
    
    Returns:
        {'track': 'momentum', 'score': float, 'details': {...}}
    """
    details = {'track': 'momentum'}
    
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
        
        # 读取缠论结构评分
        cur.execute("""
            SELECT structure_score, buy_sell_point, beichi_type, beichi_strength,
                   zoushi_type, zoushi_stage, autumn_tiger, tiger_confidence
            FROM chanlun_structure 
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        cl_row = cur.fetchone()
        cur.close(); conn.close()
        
        # ──────── 计算四个因子 ────────
        
        # 1. 缠论趋势分 (40%)
        trend_score = 50
        raw_structure_score = 50  # 独立结构分（用于10%权重）
        if cl_row and cl_row.get('structure_score') is not None:
            ss = float(cl_row['structure_score'])
            raw_structure_score = ss  # 保存原始结构分
            if ss >= 75: trend_score = 85
            elif ss >= 60: trend_score = 70
            elif ss >= 40: trend_score = 55
            else: trend_score = 35
            
            bs = cl_row.get('buy_sell_point', 'none')
            bs_boost = {'buy3': 15, 'buy2': 8, 'buy1': 3, 'sell3': -15, 'sell2': -8, 'sell1': -3}.get(bs, 0)
            trend_score = max(0, min(100, trend_score + bs_boost))
            
            bt = cl_row.get('beichi_type', 'none')
            if bt == 'bottom' and float(cl_row.get('beichi_strength', 0) or 0) > 40:
                trend_score = min(100, trend_score + 10)
            elif bt == 'top' and float(cl_row.get('beichi_strength', 0) or 0) > 40:
                trend_score = max(0, trend_score - 10)
        else:
            close = float(latest['close'])
            ma20 = float(latest.get('ma20', 0) or 0)
            ma60 = float(latest.get('ma60', 0) or 0)
            if ma20 > 0 and ma60 > 0:
                if close > ma20 and ma20 > ma60: trend_score = 65
                elif close > ma20: trend_score = 55
                elif close > ma60: trend_score = 45
                else: trend_score = 35
        
        # 2. 动量因子 (25%)
        closes = [float(r['close']) for r in reversed(rows)]
        n = len(closes)
        momentum = 50
        if n >= 20:
            r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
            r10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 else 0
            r20 = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0
            cons_up = 0
            for i in range(-5, 0):
                if closes[i] > closes[i-1]: cons_up += 1
                else: cons_up = 0
            rsi_val = float(latest.get('rsi_14', 50) or 50)
            
            score = 50
            score += max(-15, min(15, r5 * 150))
            score += max(-10, min(10, r10 * 80))
            score += max(-8, min(8, r20 * 50))
            score += min(8, cons_up * 2)
            score += (rsi_val - 50) * 0.5
            momentum = max(0, min(100, score))
        
        # 3. 资金因子 (25%)
        mf_score, mf_5d, lg_r = _calc_moneyflow_score(ts_code, ctx.trade_date)
        
        # 4. Alpha因子 (新增 2026-07-11)
        # alpha062: 高量负相关 Corr(high, volume, 5)，IC=+0.0333, IR=0.32
        # alpha046: 多均线位置 (ma3+ma6+ma12+ma24)/(4*close)，IC=+0.0301, IR=0.16
        alpha062_score = 50
        alpha046_score = 50
        try:
            # alpha062: (-1 * Corr(high, volume, 5))
            if n >= 5:
                high_5 = [float(r['high']) for r in reversed(rows[:5])]
                vol_5 = [float(r['vol']) for r in reversed(rows[:5])]
                if np.std(high_5) > 0 and np.std(vol_5) > 0:
                    corr_hv = np.corrcoef(high_5, vol_5)[0, 1]
                    # corr在[-1,1]，取负后映射到[0,100]
                    # -corr=1时（强负相关）→80分，-corr=0时→50分，-corr=-1时（强正相关）→20分
                    alpha062_score = max(0, min(100, 50 + (-corr_hv) * 30))
                    details['alpha062_corr'] = round(float(corr_hv), 3)
                
            # alpha046: (ma3+ma6+ma12+ma24)/(4*close)
            # 值>1说明均线在价格之上（超涨），<1说明在价格之下（超跌）
            # ratio在[0.85,1.15]范围，映射到[0,100]
            # ratio=1→50分，ratio>1.1→低分（均线上方太远），ratio<0.9→高分（均线下方）
            if n >= 24:
                ma3 = np.mean(closes[-3:])
                ma6 = np.mean(closes[-6:])
                ma12 = np.mean(closes[-12:])
                ma24 = np.mean(closes[-24:])
                ratio = (ma3 + ma6 + ma12 + ma24) / (4 * closes[-1])
                # ratio=1→50, ratio=0.92→80, ratio=1.08→20
                alpha046_score = max(0, min(100, 50 + (1 - ratio) * 100 * 3))
                details['alpha046_ratio'] = round(float(ratio), 4)
        except Exception as e:
            details['alpha_error'] = str(e)[:50]
        
        details['alpha062_score'] = alpha062_score
        details['alpha046_score'] = alpha046_score
        details['chanlun_trend'] = trend_score
        details['momentum_raw'] = momentum
        details['mf_score'] = mf_score
        details['mf_5d'] = round(mf_5d, 0)
        details['lg_ratio'] = round(lg_r, 4)
        details['chanlun_row'] = bool(cl_row)
        details['structure_score'] = raw_structure_score
        
        # 5. 综合权重调整（2026-07-12: Alpha062×15%回测最优，移除alpha046）
        #    旧权重: trend×0.40 + struct×0.10 + moment×0.20 + mf×0.20 + α062×0.05 + α046×0.05 = 1.0
        #    新权重: trend×0.40 + struct×0.10 + moment×0.20 + mf×0.20 + α062×0.15 = 1.05
        #    归一化(/1.05): trendy×0.381 + struct×0.095 + moment×0.190 + mf×0.190 + α062×0.143
        final_score = (trend_score * 0.381 + raw_structure_score * 0.095 +
                       momentum * 0.190 + mf_score * 0.190 +
                       alpha062_score * 0.143)
        final_score = max(0, min(100, round(final_score, 1)))
        
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
        
        # ===== 因子4: 资金因子 (15%) =====
        mf_score, mf_5d, lg_r = _calc_moneyflow_score(ts_code, ctx.trade_date)
        details['mf_score'] = mf_score
        details['mf_5d'] = round(mf_5d, 0)
        details['lg_ratio'] = round(lg_r, 4)
        
        # ===== 综合 =====
        # V4权重: 缠论×0.40 + 超跌×0.25 + ATR×0.10 + 资金×0.15 + 秋老虎+10
        final_score = structure * 0.40 + oversold * 0.25 + volatility * 0.10 + mf_score * 0.15
        
        # 秋老虎: 已经从structure中移除，单独加10分
        autumn_tiger = details.get('autumn_tiger', False)
        if autumn_tiger:
            final_score += 10
        
        final_score = max(0, min(100, round(final_score, 1)))
        
        details['structure_factor'] = structure
        details['oversold_factor'] = oversold
        details['volatility_factor'] = volatility
        details['moneyflow_factor'] = mf_score
        
        return {'track': 'reversion', 'score': final_score, 'details': details}
        
    except Exception as e:
        return {'track': 'reversion', 'score': 50, 'reason': str(e)}


# ============================================================
# V4过滤层：量比/资金/大盘强度
# ============================================================

def _apply_filters(results: List[Dict], trade_date: str, hs300_trend: float) -> Dict[str, str]:
    """
    对批量评分结果应用买入过滤层
    
    过滤规则:
    1. 爆量>2倍（拉高出货信号）→ 过滤
    2. 大盘近5日跌>3% → 过滤（系统性风险）
    3. 资金近5日净流出+爆量 → 过滤（主力出逃）
    4. 缩量<0.5倍+资金流入 → 加分标记（地量见底）
    
    Returns:
        {ts_code: reason} 被过滤的原因
    """
    filter_reasons = {}
    
    for r in results:
        ts_code = r['ts_code']
        reasons = []
        
        # 计算量比（当日vol/前20日均量）
        vol_ratio = _calc_vol_ratio(ts_code, trade_date)
        
        # 规则1: 爆量>2倍 → 过滤
        if vol_ratio > 2.0:
            reasons.append(f'爆量{vol_ratio:.1f}倍>2')
        
        # 资金验证
        _, mf_5d, lg_r = _calc_moneyflow_score(ts_code, trade_date)
        
        # 规则3: 爆量+资金流出
        if vol_ratio > 2.0 and mf_5d < -50000:
            reasons.append(f'爆量+资金流出{mf_5d/10000:.0f}万')
        
        # 规则4: 缩量<0.5倍+资金流入 → 加分（不过滤，只是标记）
        if vol_ratio < 0.5 and mf_5d > 0:
            r['_volume_bonus'] = True
        
        # 规则2: 大盘趋势判断（全局过滤）
        if hs300_trend < -0.03:
            r['_market_danger'] = True
            reasons.append(f'大盘跌{hs300_trend*100:.1f}%>3%')
        
        # 记录过滤状态
        if reasons:
            r['_filtered'] = True
            r['_filter_reasons'] = ';'.join(reasons)
            filter_reasons[ts_code] = ';'.join(reasons)
        else:
            r['_filtered'] = False
            r['_filter_reasons'] = ''
    
    return filter_reasons


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


def _build_calib_map(original_scores: List[float], confidence: float = 1.0) -> Dict[int, float]:
    """
    建立百分位映射校准表
    将P6原始分的排序位置映射到校准分区间
    
    [2026-07-09 调整] 取消置信度压缩映射。
    取消按confidence打折的逻辑。
    校准分与综合分（原始分）保持一致，不再做压缩：
      校准分 = 综合分（不做折扣）
      买入判定改用综合分≥80
    """
    n = len(original_scores)
    if n == 0: return {}
    sorted_scores = sorted(original_scores)
    
    # 校准目标：与综合分一致，不做折扣
    # P100=100（保留顶部空间），各分位点映射到接近原值
    targets = {
        5: 10, 10: 18, 15: 22,
        20: 25, 25: 30, 30: 33,
        35: 36, 40: 40, 45: 42,
        50: 50, 55: 52,
        60: 55, 65: 58, 70: 62,
        75: 68, 80: 72,
        85: 78, 90: 82, 93: 86,
        95: 90, 97: 93, 99: 96,
        100: 100
    }
    calib_map = {}
    for pct, target in targets.items():
        idx = min(int(n * pct / 100), n - 1)
        raw = sorted_scores[idx]
        calib_map[raw] = target
    
    # 补全首尾
    if sorted_scores:
        calib_map[sorted_scores[0]] = max(0, targets.get(5, 10) - 5)
        calib_map[sorted_scores[-1]] = 100
    
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


def calibrate_scores(results: List[Dict], confidence: float = 1.0) -> List[Dict]:
    """
    对批量评分结果执行百分位映射校准
    [2026-07-09 调整] 取消置信度压缩，校准分接近综合分原值
    """
    raw_scores = [r['score'] for r in results if r.get('score') is not None]
    calib_map = _build_calib_map(raw_scores, confidence)
    
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
    calibrate_scores(results, ctx.confidence)
    
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
    
    # 1.5 季节判定结果入库
    from season_engine import save_result_to_db
    try:
        save_result_to_db(judge_result)
        print(f"💾 季节判定结果已写入 season_state ({judge_result.get('market_season','?')}/{judge_result.get('hengjiyuan_level','?')})")
    except Exception as e:
        print(f"⚠️ 季节入库失败: {e}")
    
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
    
    # 3.5 过滤层（V4: 基于量比/资金/大盘的买入过滤）
    hs300_trend = ctx.get_hs300_trend()
    print(f"📊 大盘强度(沪深300近5日): {hs300_trend*100:+.2f}%")
    filter_reasons = _apply_filters(results, ctx.trade_date, hs300_trend)
    filtered_out = [r['ts_code'] for r in results if r.get('_filtered', False)]
    print(f"🔒 过滤层: 排除{len(filtered_out)}只 | {len(results)-len(filtered_out)}只可通过")
    
    # 4. 入库+打印top
    # 重新获取连接（避免评分计算中连接被关闭）
    conn = get_connection()
    cur = conn.cursor()
    saved, skipped = 0, 0
    for i, r in enumerate(results):
        try:
            code = r['ts_code']
            
            # 从 chanlun_structure 读取缠论买卖点（当日最新）
            has_details = r.get('details') is not None and r['details'].get('chanlun_trend') is not None
            cur.execute("""
                SELECT buy_sell_point, zoushi_type, beichi_type, structure_score,
                       autumn_tiger, tiger_confidence
                FROM chanlun_structure
                WHERE ts_code=%s AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 1
            """, (code, ctx.trade_date))
            cl = cur.fetchone()
            
            bs = (cl['buy_sell_point'] or 'none') if cl else 'none'
            zt = (cl['zoushi_type'] or '未知') if cl else '未知'
            ss = float(cl['structure_score'] or 0) if cl else 0
            autumn = 1 if (cl and cl['autumn_tiger']) else 0
            tiger_conf = float(cl['tiger_confidence'] or 0) if cl else 0
            
            # 计算 operation_mode
            calib = float(r['calibrated_score'] or 0)
            if calib >= 75:
                op_mode = 'attack'
            elif calib >= 60:
                op_mode = 'normal'
            elif calib >= 40:
                op_mode = 'defense'
            else:
                op_mode = 'dormant'
            
            # 计算 signal_confidence
            if calib >= 80:
                sig_conf = 'high'
            elif calib >= 60:
                sig_conf = 'medium'
            else:
                sig_conf = 'low'
            
            # 构建 reason_chain
            track_label = '动量' if r['track'] == 'momentum' else '回归'
            reason_parts = [
                f"{ctx.season}+{ctx.regime}",
                f"{track_label}轨道",
            ]
            if bs and bs != 'none':
                reason_parts.append(f"{bs}确认")
            if zt and zt not in ('unknown', '未知'):
                reason_parts.append(zt)
            if ss >= 80:
                reason_parts.append('结构强势')
            elif ss >= 60:
                reason_parts.append('结构稳定')
            if autumn:
                reason_parts.append('秋老虎')
            reason = '+'.join(reason_parts)
            
            # 构建子因子值：有details用details，无details用缠论数据推算或保留旧值
            if has_details:
                v_trend = r['details'].get('chanlun_trend', 55)
                v_momentum = r['details'].get('momentum_raw', 50)
                v_structure = r['details'].get('structure_score', ss)
                v_emotion = r['details'].get('emotion_score', 0)
                v_alpha062 = r['details'].get('alpha062_score', 50)
                v_alpha062_corr = r['details'].get('alpha062_corr', None)
                v_alpha046 = r['details'].get('alpha046_score', 50)
                v_alpha046_ratio = r['details'].get('alpha046_ratio', None)
            else:
                # details为空时：用 chanlun_structure 的 structure_score 推算合理值
                if ss >= 75: v_trend = 85
                elif ss >= 60: v_trend = 70
                elif ss >= 40: v_trend = 55
                else: v_trend = 35
                v_momentum = 50
                v_structure = ss
                v_emotion = 0
                v_alpha062 = 50
                v_alpha062_corr = None
                v_alpha046 = 50
                v_alpha046_ratio = None
                
                # 有买卖点信号时增强趋势
                bs_boost = {'buy3': 15, 'buy2': 8, 'buy1': 3, 'sell3': -15, 'sell2': -8, 'sell1': -3}.get(bs, 0)
                v_trend = max(0, min(100, v_trend + bs_boost))
            
            cur.execute("""
                INSERT INTO strategy_signal 
                    (ts_code, trade_date, track, composite_score, calibrated_score,
                     scoring_strategy, direction, operation_mode, buy_sell_point,
                     reason_chain, signal_confidence, autumn_tiger, tiger_confidence,
                     hengjiyuan_level,
                     trend_score, momentum_score, structure_score, emotion_score,
                     alpha062_score, alpha062_corr, alpha046_score, alpha046_ratio)
                VALUES (%s, %s, %s, %s, %s, %s, 'dual_track_v1', %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    track=VALUES(track), composite_score=VALUES(composite_score),
                    calibrated_score=VALUES(calibrated_score),
                    scoring_strategy=VALUES(scoring_strategy),
                    operation_mode=VALUES(operation_mode),
                    buy_sell_point=VALUES(buy_sell_point),
                    reason_chain=VALUES(reason_chain),
                    signal_confidence=VALUES(signal_confidence),
                    autumn_tiger=VALUES(autumn_tiger),
                    tiger_confidence=VALUES(tiger_confidence),
                    hengjiyuan_level=VALUES(hengjiyuan_level),
                    trend_score=VALUES(trend_score),
                    momentum_score=VALUES(momentum_score),
                    structure_score=VALUES(structure_score),
                    emotion_score=VALUES(emotion_score),
                    alpha062_score=VALUES(alpha062_score),
                    alpha062_corr=VALUES(alpha062_corr),
                    alpha046_score=VALUES(alpha046_score),
                    alpha046_ratio=VALUES(alpha046_ratio)
            """, (code, ctx.trade_date, r['track'],
                  r['score'], r['calibrated_score'],
                  'momentum' if r['track'] == 'momentum' else 'reversion',
                  op_mode, bs, reason, sig_conf,
                  autumn, tiger_conf,
                  ctx.raw.get('hengjiyuan_level', 'weak_heng'),
                  v_trend, v_momentum, v_structure, v_emotion,
                  v_alpha062, v_alpha062_corr, v_alpha046, v_alpha046_ratio))
            if (i+1) % 50 == 0:
                print(f"  💾 已入库 {i+1}/{tot}")
        except Exception as e:
            skipped += 1
            if skipped <= 3:
                print(f"  ⚠️ 跳过 {r['ts_code']}: {e}")
    
    conn.commit()
    cur.close()
    conn.close()
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

# ═══ 以下从score_engine.py迁移（供score_chanlun_enhanced使用） ═══
def sma(d,p):
    if len(d)<p: return sum(d)/len(d) if d else 0
    return sum(d[-p:])/p


def rsi(c,p=14):
    if len(c)<p+1: return 50
    g=sum(max(0,c[i]-c[i-1]) for i in range(-p,0))
    l=sum(max(0,c[i-1]-c[i]) for i in range(-p,0))+0.0001
    return 100-100/(1+g/l)


def roc(c,p):
    if len(c)<=p: return 0
    return (c[-1]-c[-p-1])/c[-p-1]


def score_chanlun_enhanced(rows, season, industry, ts_code=None):
    """
    v4.0 缠论增强:
    - 优先从数据库读取chanlun_structure(buy_sell_point/structure_score/beichi)
    - 数据为空时用多周期背离代理: MACD背离 + RSI背离 + MA乖离
    """
    # ── 从chanlun_structure表读取真实缠论数据 ──
    if ts_code:
        try:
            cur=get_connection().cursor()
            cur.execute(
                "SELECT buy_sell_point, structure_score, beichi_type, beichi_strength, "
                "zoushi_type, zoushi_stage, autumn_tiger, tiger_confidence, "
                "bi_direction, bi_strength, zhongshu_count, zhongshu_stability "
                "FROM chanlun_structure WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",
                (ts_code,)
            )
            cl_row=cur.fetchone()
            cur.close()
            if cl_row and cl_row.get('structure_score') is not None:
                ss=float(cl_row['structure_score'])
                bs=cl_row['buy_sell_point'] or 'none'
                bt=cl_row['beichi_type'] or 'none'
                bstr=float(cl_row.get('beichi_strength',0) or 0)
                zt=cl_row['zoushi_type'] or 'unknown'
                at=cl_row['autumn_tiger'] or 0
                
                # 用结构评分映射到趋势/动量/波动/量能子维度
                base_cl=50.0
                if ss>=75: base_cl=80
                elif ss>=60: base_cl=65
                elif ss>=40: base_cl=50
                else: base_cl=30
                
                # 买卖点修正
                bs_boost=0
                if bs=='buy3': bs_boost=20
                elif bs=='buy2': bs_boost=10
                elif bs=='buy1': bs_boost=5
                elif bs=='sell3': bs_boost=-20
                elif bs=='sell2': bs_boost=-10
                elif bs=='sell1': bs_boost=-5
                
                # 背驰修正
                beichi_boost=0
                if bt=='bottom' and bstr>40: beichi_boost=15
                elif bt=='top' and bstr>40: beichi_boost=-15
                
                # 走势类型修正
                zoushi_boost=0
                if zt=='盘整' and bs in ('buy2','buy3'): zoushi_boost=10
                elif zt=='unknown': zoushi_boost=-5
                
                # 秋老虎加分
                tiger_boost=15 if at else 0
                
                chanlun_signal=bs_boost+beichi_boost+zoushi_boost+tiger_boost
                chanlun_signal=max(-100, min(100, chanlun_signal))
                
                return {
                    'total':round(max(0,min(100,base_cl+chanlun_signal*0.3)),1),
                    'trend':round(max(0,min(100,base_cl-10+chanlun_signal*0.2)),1),
                    'momentum':round(max(0,min(100,base_cl+bs_boost*0.5+tiger_boost*0.3)),1),
                    'volatility':round(max(0,min(100,base_cl-20+abs(chanlun_signal)*0.2)),1),
                    'volume':round(max(0,min(100,50+tiger_boost*0.3)),1),
                    'chanlun_signal':chanlun_signal,
                }
        except Exception:
            pass  # DB查询失败则fallback到代理算法
    
    closes=[float(r['close']) for r in rows]
    highs=[float(r['high']) for r in rows]
    lows=[float(r['low']) for r in rows]
    vols=[float(r.get('vol',0) or 0) for r in rows]
    n=len(closes)

    if n<120:
        return {'total':50,'trend':50,'momentum':50,'volatility':50,'volume':50,'chanlun_signal':0}

    close=closes[-1]

    # ── 趋势(40%) ──
    ma5=sma(closes,5); ma10=sma(closes,10); ma20=sma(closes,20)
    ma60=sma(closes,60); ma120=sma(closes,120)

    tr=0
    if ma5>ma10: tr+=8
    if ma5>ma20: tr+=7
    if ma10>ma20: tr+=10
    if ma20>ma60: tr+=10
    if ma20>ma120: tr+=5
    if close>ma5: tr+=5
    if close>ma20: tr+=5
    old_ma20=sma(closes[:-20],20) if n>80 else ma20
    slope20=(ma20-old_ma20)/old_ma20 if old_ma20>0 else 0
    tr+=max(0,min(25,(slope20+0.05)*250))
    yh=max(closes[-250:]) if n>=250 else max(closes)
    yl=min(closes[-250:]) if n>=250 else min(closes)
    if yh>yl: tr+=(close-yl)/(yh-yl)*25
    trend_score=round(max(0,min(100,tr)),1)

    # ── 动量(35%) ──
    r5=roc(closes,5); r10=roc(closes,10); r20=roc(closes,20); r14=rsi(closes,14)
    mo=0
    mo+=max(0,min(25,12.5+r5*50))
    mo+=max(0,min(20,10+r10*30))
    mo+=max(0,min(15,7.5+r20*20))
    mo+=max(0,min(20,r14*0.2))
    acc=r5-r20
    if acc>0.02: mo+=10
    elif acc>0: mo+=5
    if n>=6:
        up_vol=sum(1 for i in range(-5,0) if closes[i]>closes[i-1] and vols[i]>vols[i-1])
        mo+=up_vol*2
    momentum_score=round(max(0,min(100,mo)),1)

    # ── 波动(15%, 反转版) — 连续函数+滚动标准化 ──
    vol20=stddev(closes,20); vol60=stddev(closes,60)
    daily_vol=vol20/close if close>0 else 0.02
    # 波动率Z-score（相对自身60日历史的位置）
    vol_zscore = 0
    if vol60>0 and n>=60:
        vol_mean = sum(stddev(closes[i-20:i],20)/close for i in range(-60,0) if len(closes[i-20:i])==20) / 60
        vol_std = (sum((stddev(closes[i-20:i],20)/close - vol_mean)**2 for i in range(-60,0) if len(closes[i-20:i])==20) / 60)**0.5
        if vol_std > 0:
            vol_zscore = (daily_vol - vol_mean) / vol_std
    # 连续映射: 低波动=高分(低波异象), 高波动=低分
    vl = 50 - vol_zscore * 8  # 每1个标准差±8分
    vl = max(10, min(90, vl))
    # 滚动相对位置修正
    if vol60>0:
        vr=vol20/vol60
        if vr<0.7: vl+=8
        elif vr<0.85: vl+=4
        elif vr>1.5: vl-=8
        elif vr>1.2: vl-=4
    if n>=20:
        h20=max(closes[-20:]); mdd=(h20-close)/h20
        if mdd>0.15: vl+=6
        elif mdd>0.10: vl+=3
    volatility_score=round(max(10,min(90,vl)),1)

    # ── 量能(10%) ──
    v20m=sma(vols,20); v60m=sma(vols,60)
    vr_day=vols[-1]/v20m if v20m>0 else 1
    vo=50
    if v60m>0:
        vt=v20m/v60m
        if vt>1.3: vo-=8
        elif vt>1.1: vo-=3
        elif vt<0.7: vo+=5
        elif vt<0.9: vo+=3
    if vr_day>2.0: vo-=10
    elif vr_day>1.5: vo-=5
    elif 0.7<=vr_day<=1.3: vo+=3
    elif vr_day<0.5: vo+=5
    if n>=6:
        dn_vol=sum(1 for i in range(-5,0) if closes[i]<closes[i-1] and vols[i]>vols[i-1])
        vo-=dn_vol*3
    volume_score=round(max(0,min(100,vo)),1)

    # ── 缠论代理: 多周期背离检测 ──
    chanlun_signal=0  # -100~+100: 负=超跌反弹窗口, 正=趋势延续

    # MACD金叉/死叉 (12/26/9)
    ema12=sma(closes,12); ema26=sma(closes,26)
    # 简化: 判断MACD柱状图趋势
    if n>=35:
        old_ema12=sma(closes[-9:-1],12) if n>=38 else ema12
        if ema12>ema26 and old_ema12<=sma(closes[-9:-1],26) if n>=38 else False:
            chanlun_signal+=15  # 金叉

    # RSI背离: 价格创新高但RSI未创新高=顶背离
    if n>=40:
        h20_p=max(closes[-30:-10]); r20_p=rsi(closes[-30:-10],14)
        h20_n=max(closes[-10:]); r20_n=rsi(closes[-10:],14)
        if h20_n>h20_p and r20_n<r20_p-5: chanlun_signal-=20  # 顶背离
        l20_p=min(closes[-30:-10]); r20_p2=rsi(closes[-30:-10],14)
        l20_n=min(closes[-10:]); r20_n2=rsi(closes[-10:],14)
        if l20_n<l20_p and r20_n2>r20_p2+5: chanlun_signal+=20  # 底背离

    # MA乖离: 价格远离MA20=超跌/超涨
    if close>0 and n>=20:
        ma20_dev=(close-ma20)/ma20
        if ma20_dev<-0.1: chanlun_signal+=15  # 深度超跌
        elif ma20_dev<-0.05: chanlun_signal+=8
        elif ma20_dev>0.1: chanlun_signal-=10  # 追高危险

    # 连续K线方向
    if n>=5:
        cons_up=sum(1 for i in range(-4,0) if closes[i]>closes[i-1])
        cons_dn=sum(1 for i in range(-4,0) if closes[i]<closes[i-1])
        if cons_up>=4: chanlun_signal+=10
        elif cons_dn>=4: chanlun_signal-=5

    chanlun_signal=max(-100,min(100,chanlun_signal))

    # 合成
    total = trend_score*0.40 + momentum_score*0.35 + volatility_score*0.15 + volume_score*0.10
    # 缠论信号修正: ±15分
    total += chanlun_signal * 0.15

    return {
        'total':round(max(0,min(100,total)),1),
        'trend':trend_score,'momentum':momentum_score,
        'volatility':volatility_score,'volume':volume_score,
        'chanlun_signal':chanlun_signal
    }

# ═══ 优化3: 板块特化权重 ═══
BLOCK_WEIGHTS = {
    # 科技/AI类: 趋势+缠论权重大
    '半导体': {'trend':0.45,'momentum':0.30,'volatility':0.15,'volume':0.10},
    '元器件': {'trend':0.40,'momentum':0.30,'volatility':0.15,'volume':0.15},
    '通信设备': {'trend':0.40,'momentum':0.35,'volatility':0.10,'volume':0.15},
    'IT设备': {'trend':0.45,'momentum':0.30,'volatility':0.10,'volume':0.15},
    '软件服务': {'trend':0.35,'momentum':0.40,'volatility':0.10,'volume':0.15},
    # 消费类: 动量大
    '家用电器': {'trend':0.30,'momentum':0.40,'volatility':0.15,'volume':0.15},
    '中成药': {'trend':0.30,'momentum':0.35,'volatility':0.15,'volume':0.20},
    '化学制药': {'trend':0.30,'momentum':0.35,'volatility':0.15,'volume':0.20},
    '乳制品': {'trend':0.30,'momentum':0.40,'volatility':0.15,'volume':0.15},
    '批发业': {'trend':0.30,'momentum':0.35,'volatility':0.15,'volume':0.20},
    # 周期类: 均值回归权重大(波动因子上调)
    '化工原料': {'trend':0.30,'momentum':0.30,'volatility':0.20,'volume':0.20},
    '电气设备': {'trend':0.35,'momentum':0.30,'volatility':0.20,'volume':0.15},
    '专用机械': {'trend':0.30,'momentum':0.30,'volatility':0.20,'volume':0.20},
    '玻璃': {'trend':0.25,'momentum':0.30,'volatility':0.25,'volume':0.20},
    '普钢': {'trend':0.25,'momentum':0.25,'volatility':0.25,'volume':0.25},
    '化工机械': {'trend':0.30,'momentum':0.30,'volatility':0.20,'volume':0.20},
    '水力发电': {'trend':0.30,'momentum':0.25,'volatility':0.20,'volume':0.25},
    '火力发电': {'trend':0.30,'momentum':0.30,'volatility':0.20,'volume':0.20},
    '小金属': {'trend':0.25,'momentum':0.30,'volatility':0.25,'volume':0.20},
}

