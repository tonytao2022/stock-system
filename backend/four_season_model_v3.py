from db_config import get_connection
#!/usr/bin/env python3
"""
四季趋势投资模型 v3.0 — AI 投资顾问版
========================================
基于 2026-05-26 多周期回测结论 + season_engine v2.1

核心认知：
  1. 趋势是朋友 — 牛市 momentum 最有效
  2. 恐惧是机会 — 熊市/超跌/崩盘是反转窗口
  3. 时间是你的武器 — 不同持有期对应不同信号
  4. 仓位比选股重要 — 季节决定风险敞口

三时间尺度：
  L1 周线级别 — 大趋势方向(长期仓位基准)
  L2 日线级别 — 中期波段(波段操作)
  L3 次级别   — 短期动量(短线窗口)

新增特性(v3.0):
  - 恐慌(panic)/复苏(recovery) 极端状态识别
  - 三态 × 三时间尺度 × 自适应仓位
  - hold_period_days + 止损线内置到信号
  - 评分策略: momentum/reversion/mixed 三模式
"""

import os, sys, math, json, pymysql
from db_config import db_cursor, get_connection
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import warnings; warnings.filterwarnings("ignore")

# ═══ 配置 ═══
def _mysql_pass():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if 'password' in line:
                    return line.strip().split('=')[-1].strip().strip('"').strip("'")
    except: pass
    return os.environ.get('MYSQL_PASSWORD', '')

IDX = {
    '000300.SH':{'name':'沪深300','w':0.40},
    '000001.SH':{'name':'上证综指','w':0.25},
    '399006.SZ':{'name':'创业板指','w':0.20},
    '399001.SZ':{'name':'深证成指','w':0.10},
    '000688.SH':{'name':'科创50','w':0.05},
}

EMOJI = {'spring':'🌸','summer':'☀️','autumn':'🍂','winter':'❄️','chaos_mild':'🌤️','chaos':'🌪️','chaos_cold':'🌥️','panic':'💀','recovery':'🌱'}
REGIME_LABEL = {'bull':'🐂牛市','bear':'🐻熊市','range':'↔️震荡'}

# ═══ 数据结构 ═══
@dataclass
class SeasonResult:
    trade_date: date = None
    season: str = 'chaos'         # spring/summer/autumn/winter/chaos/panic/recovery
    season_detail: str = 'chaos'  # 细分类
    regime: str = 'range'         # bull/bear/range
    confidence: float = 0.3
    position_pct: float = 35.0    # 建议仓位%
    position_label: str = '中性'
    scoring_strategy: str = 'momentum'  # momentum/reversion/mixed
    hold_period_days: int = 10
    max_drawdown_stop: float = -0.05
    rule_chain: str = ''
    risk_flags: list = field(default_factory=list)
    raw_score: float = 0.0

# ═══ 技术指标 ═══
def sma(data, period):
    if len(data) < period: return sum(data)/len(data) if data else 0
    return sum(data[-period:])/period

def ema(data, period):
    if len(data) < period: return data[-1] if data else 0
    k = 2/(period+1)
    r = sum(data[:period])/period
    for x in data[period:]: r = x*k + r*(1-k)
    return r

def rsi(closes, period=14):
    if len(closes) < period+1: return 50
    gains = sum(max(0,closes[i]-closes[i-1]) for i in range(-period,0))
    losses = sum(max(0,closes[i-1]-closes[i]) for i in range(-period,0)) + 0.0001
    return 100 - 100/(1+gains/losses)

def roc(closes, period):
    if len(closes) <= period: return 0
    return (closes[-1]-closes[-period-1])/closes[-period-1]

def max_drawdown(closes, period):
    h = max(closes[-period:])
    l = min(closes[-period:])
    return (h-l)/h if h>0 else 0

# ═══ L1: 周线级别大趋势 ═══
def analyze_l1(closes):
    n = len(closes)
    if n < 200: return {'trend_dir':'flat','score':0,'confidence':0.3}
    
    ma20=sma(closes,20); ma60=sma(closes,60); ma120=sma(closes,120)
    ma200=sma(closes,200) if n>=200 else ma120
    close=closes[-1]
    
    # MA60 30日斜率
    old_ma60 = sma(closes[:-30],60) if n>90 else ma60
    ma60_slope = (ma60-old_ma60)/old_ma60 if old_ma60>0 else 0
    
    score = ma60_slope * 50  # 映射到±5
    score = max(-5,min(5,score))
    
    if ma20>ma60: score+=1.5
    if ma20>ma120: score+=1.0
    if ma60>ma120: score+=2.0
    if ma120>ma200: score+=1.5
    if close>ma20: score+=0.5
    if close>ma60: score+=0.5
    
    price_vs_ma200 = (close-ma200)/ma200 if ma200>0 else 0
    if price_vs_ma200>0.3: score-=2
    elif price_vs_ma200>0.1: score+=1
    elif price_vs_ma200<-0.2: score-=1
    
    score = max(-8,min(8,score))
    if score>2: td='up'
    elif score<-2: td='down'
    else: td='flat'
    
    return {'trend_dir':td, 'score':round(score,2),
            'confidence':round(min(1.0,abs(score)/6.0+0.3),3),
            'ma60_slope':round(ma60_slope*100,2),
            'price_vs_ma200':round(price_vs_ma200*100,1)}

# ═══ L2: 三态识别 + 季节判定 ═══
def detect_regime(closes):
    n = len(closes)
    if n < 120: return ('range', 0.3)
    
    ma120 = sma(closes, 120)
    roc_120 = roc(closes, 120)
    
    old = closes[:-30] if n>150 else closes[:n-120]
    old_ma120 = sma(old, 120) if len(old)>=60 else ma120
    ma120_slope = (ma120-old_ma120)/old_ma120 if old_ma120>0 else 0
    
    if ma120_slope>0.02 and roc_120>0.05:
        regime='bull'; conf=min(0.9,0.5+ma120_slope*5+roc_120)
    elif ma120_slope<-0.02 or roc_120<-0.10:
        regime='bear'; conf=min(0.9,0.5+abs(ma120_slope)*5+abs(roc_120))
    else:
        regime='range'; conf=0.4+(1-abs(ma120_slope)*10)*0.3
    
    return regime, max(0.2, min(0.9, conf))

def classify_chaos(short_mom, breadth_ratio):
    if short_mom>0.01 and breadth_ratio>0.55: return 'chaos_mild'
    elif short_mom<-0.01 or breadth_ratio<0.42: return 'chaos_cold'
    return 'chaos'

def detect_panic(l1, closes, vols, breadth_ratio):
    if l1['trend_dir']!='down' or len(closes)<10: return False
    ret5 = (closes[-1]-closes[-6])/closes[-6]
    v5ma = sma(vols[-10:-5],5) if len(vols)>10 else vols[-1]
    vr = vols[-1]/v5ma if v5ma>0 else 1
    return ret5<-0.07 and vr>1.5 and breadth_ratio<0.30

def detect_recovery(l1, closes):
    if len(closes)<20: return False
    ret5 = (closes[-1]-closes[-6])/closes[-6]
    week_low = min(closes[-10:-5])
    return ret5>0.03 and closes[-1]>week_low and l1['trend_dir']=='down'

# ═══ 增强版季节判定 ═══
def determine_season(l1, regime, short_mom, medium_mom, rsi_val, breadth_ratio, raw_score, closes, vols):
    """季节 × 三态 × 仓位 × 策略 × 持有期 × 止损 完整映射"""
    
    # 1. 极端状态优先
    if detect_panic(l1, closes, vols, breadth_ratio):
        return SeasonResult(season='panic',season_detail='panic',regime=regime,
            position_pct=20,position_label='🔻极低仓位(恐慌抄底窗口)',
            scoring_strategy='reversion',hold_period_days=60,max_drawdown_stop=-0.10)
    if detect_recovery(l1, closes):
        return SeasonResult(season='recovery',season_detail='recovery',regime=regime,
            position_pct=45,position_label='📈低仓试探(复苏确认)',
            scoring_strategy='mixed',hold_period_days=30,max_drawdown_stop=-0.07)
    
    chaos_type = classify_chaos(short_mom, breadth_ratio)
    
    # 2. 常规四季 (regime × raw_score × 仓位 × 策略)
    MAP = {
        # (regime, rs_gt, rs_lt) -> (season, detail, pos%, label, strategy, hold_days, stop)
        ('bull', 5, 99): ('summer','summer',80,'☀️高仓位(牛市冲浪)','momentum',20,-0.06),
        ('bull', 2, 5): ('spring','spring',65,'🌸中高仓位(牛市回调买点)','momentum',10,-0.05),
        ('bull', -1, 2): ('chaos',chaos_type or 'chaos_mild',50,'🌤️中性仓位(牛市盘整)','momentum',10,-0.05),
        ('bull', -99, -1): ('autumn','autumn',35,'🍂减仓(牛市回调)','mixed',20,-0.06),
        
        ('bear', 3, 99): ('chaos','chaos_mild',30,'🌤️低仓位(熊市反弹)','momentum',5,-0.04),
        ('bear', -1, 3): ('chaos',chaos_type or 'chaos',20,'🌪️极低仓位(熊市观望)','reversion',10,-0.05),
        ('bear', -3, -1): ('autumn','autumn',15,'🍂轻仓防守(熊市下跌)','reversion',30,-0.08),
        ('bear', -99, -3): ('winter','winter',5,'❄️空仓观望(熊市冬眠)','reversion',60,-0.10),
        
        ('range', 4, 99): ('summer','summer',55,'☀️中等仓位(震荡偏强)','momentum',10,-0.05),
        ('range', 1.5, 4): ('spring','spring',45,'🌸中等仓位(震荡上攻)','momentum',10,-0.04),
        ('range', -1.5, 1.5): ('chaos',chaos_type or 'chaos',35,'🌪️轻仓观望(震荡混沌)','mixed',5,-0.03),
        ('range', -3, -1.5): ('autumn','autumn',25,'🍂轻仓防守(震荡偏弱)','reversion',20,-0.05),
        ('range', -99, -3): ('winter','winter',15,'❄️极轻仓(震荡下行)','reversion',30,-0.08),
    }
    
    for (reg, lo, hi), (se, det, pos, label, strat, hld, stop) in MAP.items():
        if regime==reg and raw_score>lo and raw_score<=hi:
            risk = []
            if rsi_val>75: risk.append(f'RSI超买({rsi_val:.0f})')
            if rsi_val<25: risk.append(f'RSI超卖({rsi_val:.0f})')
            if abs(short_mom)>0.08: risk.append(f'动量极端({short_mom*100:+.1f}%)')
            if breadth_ratio>0.80: risk.append('极端多头广度')
            if breadth_ratio<0.20: risk.append('极端空头广度')
            return SeasonResult(season=se,season_detail=det,regime=regime,
                position_pct=pos,position_label=label,scoring_strategy=strat,
                hold_period_days=hld,max_drawdown_stop=stop,risk_flags=risk)
    
    # fallback
    return SeasonResult(season='chaos',season_detail='chaos',regime='range',
        position_pct=35,position_label='中性',scoring_strategy='mixed')

# ═══ 主引擎 ═══
class FourSeasonModel:
    def __init__(self, db=None):
        self.db = db or DB
        self.conn = None
    
    def _connect(self):
        if self.conn is None or not self.conn.open:
            self.conn = pymysql.connect(**self.db)
    
    def close(self):
        if self.conn and self.conn.open: self.conn.close()
    
    def _load_kline(self, ts_code, lookback=400):
        self._connect()
        cur = self.conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT trade_date,open,high,low,close,vol,change_pct FROM daily_kline WHERE ts_code=%s ORDER BY trade_date DESC LIMIT %s", (ts_code,lookback))
        rows = cur.fetchall(); cur.close(); rows.reverse()
        return rows
    
    def _load_breadth(self, trade_date):
        self._connect()
        cur = self.conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END) as up, SUM(CASE WHEN change_pct<0 THEN 1 ELSE 0 END) as down FROM daily_kline WHERE trade_date=%s", (trade_date,))
        r = cur.fetchone(); cur.close()
        if r and r['total']:
            return {'up_ratio':r['up']/r['total'],'up':r['up'],'down':r['down'],'total':r['total']}
        return {'up_ratio':0.5,'up':0,'down':0,'total':0}

    def analyze(self, ts_code, breadth_ratio=0.5):
        rows = self._load_kline(ts_code)
        if len(rows)<200: return {'error':'数据不足200日'}
        closes=[float(r['close']) for r in rows]
        highs=[float(r['high']) for r in rows]
        lows=[float(r['low']) for r in rows]
        vols=[float(r.get('vol',0) or 0) for r in rows]
        
        l1 = analyze_l1(closes)
        regime, rc = detect_regime(closes)
        sm5=roc(closes,5); mm20=roc(closes,20); r14=rsi(closes,14)
        
        v5m=sma(vols[-10:],5) if len(vols)>=10 else vols[-1]
        v20m=sma(vols[-25:],20) if len(vols)>=25 else v5m
        vreg='high' if v5m>v20m*1.3 else ('low' if v5m<v20m*0.7 else 'normal')
        
        rs = l1['score']*0.5 + (sm5*20+mm20*10)*0.3 + (float(breadth_ratio)-0.5)*10*0.2
        rs = max(-8,min(8,rs))
        
        se = determine_season(l1, regime, sm5, mm20, r14, breadth_ratio, rs, closes, vols)
        se.trade_date = rows[-1]['trade_date']
        se.raw_score = round(rs,2)
        se.confidence = round(rc,3)
        
        return {
            'ts_code':ts_code, 'name':IDX.get(ts_code,{}).get('name',''),
            'l1':l1, 'regime':regime, 'regime_conf':rc,
            'mom_5d':round(sm5*100,2), 'mom_20d':round(mm20*100,2),
            'rsi_14':round(r14,1), 'vol_regime':vreg,
            'raw_score':round(rs,2), 'season':se,
        }
    
    def judge(self, target_date=None):
        """全市场综合判定"""
        results = {}
        for code in IDX:
            results[code] = self.analyze(code)
        
        # 获取最新日期
        latest = None
        for code, r in results.items():
            if 'error' not in r and hasattr(r.get('season'),'trade_date'):
                d = r['season'].trade_date
                if latest is None or d>latest: latest=d
        
        breadth = self._load_breadth(latest) if latest else {}
        br = breadth.get('up_ratio',0.5)
        
        # 重新分析（含 breadth）
        for code in IDX:
            results[code] = self.analyze(code, br)
        
        # 加权季节投票
        votes = defaultdict(float)
        wsum = 0; wscore = 0
        for code, r in results.items():
            if 'error' in r: continue
            w = IDX[code]['w']
            s = r['season']
            votes[s.season] += w
            if s.season_detail != s.season: votes[s.season_detail] += w*0.5
            wscore += r['raw_score']*w; wsum += w
        
        mkt_season = max(votes, key=votes.get) if votes else 'chaos'
        main = results.get('000300.SH', {}).get('season')
        
        # 信号看板
        if main and hasattr(main,'season'):
            chain = (f"全市场: {EMOJI.get(mkt_season,'')}{mkt_season} | "
                    f"{REGIME_LABEL.get(main.regime,'震荡')} | "
                    f"仓位{main.position_pct:.0f}% | "
                    f"{main.scoring_strategy} | "
                    f"持有{main.hold_period_days}日 | "
                    f"止损{main.max_drawdown_stop*100:+.0f}%")
            if main.risk_flags:
                chain += f" | ⚠️{' '.join(main.risk_flags)}"
            main.rule_chain = chain
        
        return {
            'trade_date': str(latest) if latest else None,
            'market_season': mkt_season,
            'market_emoji': EMOJI.get(mkt_season,''),
            'market_score': round(wscore/wsum,2) if wsum>0 else 0,
            'market_result': main,
            'breadth': breadth,
            'details': results,
            'votes': dict(votes),
        }

# ═══ CLI ═══
if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='四季趋势投资模型 v3.0')
    ap.add_argument('--now', action='store_true', help='实时判定')
    ap.add_argument('--json', action='store_true', help='JSON输出')
    args = ap.parse_args()
    
    m = FourSeasonModel()
    try:
        r = m.judge()
        if args.json:
            print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
        else:
            mr = r.get('market_result')
            br = r.get('breadth',{})
            print(f"┌──────────────────────────────────────────┐")
            print(f"│  四季趋势模型 v3.0 — 信号看板             │")
            print(f"├──────────────────────────────────────────┤")
            print(f"│  日期: {r['trade_date']}                           │")
            print(f"│  季节: {r.get('market_emoji','')} {r['market_season']:<14s}              │")
            if mr and hasattr(mr,'regime'):
                print(f"│  状态: {REGIME_LABEL.get(mr.regime,'震荡'):<14s}                      │")
                print(f"│  仓位: {mr.position_pct:5.1f}% — {mr.position_label:<28s}│")
                print(f"│  策略: {mr.scoring_strategy:<10s} 持有:{mr.hold_period_days}日  止损:{mr.max_drawdown_stop*100:+.0f}%   │")
                if mr.risk_flags:
                    for f in mr.risk_flags:
                        print(f"│  ⚠️  {f:<38s}│")
            print(f"│  涨跌比: {br.get('up_ratio',0)*100:.0f}% ({br.get('up',0)}/{br.get('down',0)})                       │")
            print(f"│  综合评分: {r.get('market_score',0):+.1f}                          │")
            print(f"│  投票分布: {r.get('votes',{})}                         │")
            print(f"└──────────────────────────────────────────┘")
    finally:
        m.close()
