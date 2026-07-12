#!/usr/bin/env python3
"""
Alpha191 因子库 — V2 适配版
=============================
从 OmniAlpha 项目搬运 Alpha191 因子计算引擎，适配 V2 的 MySQL 数据源。

主要改动:
1. 数据源从本地CSV改为MySQL stock_db_v2.daily_kline_qfq
2. 移除 benchmark_open/close 依赖（指数数据暂不可用）
3. 移除 vwap 字段依赖（从 amount/volume 实时计算）
4. 输出格式适配 V2 的批量因子计算

用法:
  from alpha191_v2 import AlphaFactory
  factory = AlphaFactory()
  df = factory.load_data('2024-01-02', '2026-07-10')
  alpha_values = factory.compute_alpha('alpha001', df)
"""

import os, sys, json, math, time
import numpy as np
import pandas as pd
from scipy.stats import rankdata
sys.path.insert(0, '/opt/stock-analyzer')
import db_config


# ============================================================
# 基础算子（与 OmniAlpha 保持一致的命名和签名）
# ============================================================

def Log(sr):
    return np.log(sr)

def Rank(sr):
    """列-升序排序并转化成百分比"""
    return sr.rank(axis=1, method='min', pct=True)

def Delta(sr, period):
    """period日差分"""
    return sr.diff(period)

def Delay(sr, period):
    """period阶滞后项"""
    return sr.shift(period)

def Corr(x, y, window):
    """window日滚动相关系数"""
    r = x.rolling(window).corr(y).fillna(0)
    r.iloc[:(window-1), :] = None
    return r

def Cov(x, y, window):
    """window日滚动协方差"""
    return x.rolling(window).cov(y)

def Sum(sr, window):
    """window日滚动求和"""
    return sr.rolling(window).sum()

def Prod(sr, window):
    """window日滚动求乘积"""
    return sr.rolling(window).apply(lambda x: np.prod(x))

def Mean(sr, window):
    """window日滚动求均值"""
    return sr.rolling(window).mean()

def Std(sr, window):
    """window日滚动求标准差"""
    return sr.rolling(window).std()

def Tsrank(sr, window):
    """window日序列末尾值的顺位"""
    return sr.rolling(window).apply(lambda x: rankdata(x)[-1])

def Tsmax(sr, window):
    """window日滚动求最大值"""
    return sr.rolling(window).max()

def Tsmin(sr, window):
    """window日滚动求最小值"""
    return sr.rolling(window).min()

def Sign(sr):
    """符号函数"""
    return np.sign(sr)

def Max(sr1, sr2):
    return np.maximum(sr1, sr2)

def Min(sr1, sr2):
    return np.minimum(sr1, sr2)

def Rowmax(sr):
    return sr.max(axis=1)

def Rowmin(sr):
    return sr.min(axis=1)

def Sma(sr, n, m):
    """sma均值（指数加权）"""
    return sr.ewm(alpha=m/n, adjust=False).mean()

def Abs(sr):
    return sr.abs()

def Sequence(n):
    """生成 1~n 的等差序列"""
    return np.arange(1, n+1)

def Regbeta(sr, x):
    """滚动回归beta"""
    window = len(x)
    return sr.rolling(window).apply(lambda y: np.polyfit(x, y, deg=1)[0])

def Decaylinear(sr, window):
    """线性衰减加权平均"""
    weights = np.array(range(1, window+1))
    sum_weights = np.sum(weights)
    return sr.rolling(window).apply(lambda x: np.sum(weights * x) / sum_weights)

def Lowday(sr, window):
    """window天内最低价距今天数"""
    return sr.rolling(window).apply(lambda x: len(x) - x.values.argmin())

def Highday(sr, window):
    """window天内最高价距今天数"""
    return sr.rolling(window).apply(lambda x: len(x) - x.values.argmax())

def Wma(sr, window):
    """指数加权移动平均（衰减系数0.9）"""
    weights = np.array(range(window-1, -1, -1))
    weights = np.power(0.9, weights)
    sum_weights = np.sum(weights)
    return sr.rolling(window).apply(lambda x: np.sum(weights * x) / sum_weights)

def Count(cond, window):
    """window天内条件为真的天数"""
    return cond.rolling(window).apply(lambda x: x.sum())

def Sumif(sr, window, cond):
    """window天内满足条件时求和"""
    sr2 = sr.copy()
    sr2[~cond] = 0
    return sr2.rolling(window).sum()

def Returns(df):
    """日收益率"""
    return df.rolling(2).apply(lambda x: x.iloc[-1] / x.iloc[0]) - 1


# ============================================================
# AlphaFactory — 适配 V2 数据库
# ============================================================

class AlphaFactory:
    """
    Alpha191 因子计算工厂
    数据直接从 MySQL stock_db_v2 加载
    
    用法:
        af = AlphaFactory()
        af.load_data('2024-01-02', '2026-07-10')
        af.compute_alpha('alpha001')  # 返回 Series
        af.compute_all()              # 返回 DataFrame
    """
    
    def __init__(self):
        self.data = None  # pivot后的DataFrame，index=date, columns=MultiIndex(field, code)
        self.codes = None
        self.dates = None
    
    def load_data(self, start_date='2024-01-02', end_date='2026-07-10'):
        """从MySQL加载K线数据，返回pivot表"""
        print(f"  ⏳ 加载数据 {start_date}~{end_date}...", end='', flush=True)
        
        pwd = db_config._get_password()
        conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                               password=pwd, database='stock_db_v2',
                               charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
        cur = conn.cursor()
        
        cur.execute("SELECT ts_code FROM backtest_pool")
        all_codes = [r['ts_code'] for r in cur.fetchall()]
        
        # 取K线 + 计算vwap
        ph = ','.join(['%s']*len(all_codes))
        cur.execute(f"""
            SELECT ts_code, trade_date, `open`, high, low, `close`, vol as volume, amount
            FROM daily_kline_qfq
            WHERE ts_code IN ({ph}) AND trade_date>=%s AND trade_date<=%s
            ORDER BY trade_date, ts_code
        """, (*all_codes, start_date, end_date))
        rows = cur.fetchall()
        conn.close()
        print(f" {len(rows)}条 共{len(all_codes)}只")
        
        self.all_codes = all_codes
        
        # 构建宽表
        records = []
        for r in rows:
            td = r['trade_date'].strftime('%Y-%m-%d')
            vwap_val = float(r['amount']) / (float(r['volume']) * 100) if r['amount'] is not None and r['volume'] is not None and float(r['volume']) > 0 else None
            records.append({
                'date': td,
                'code': r['ts_code'],
                'open': float(r['open'] or 0),
                'high': float(r['high'] or 0),
                'low': float(r['low'] or 0),
                'close': float(r['close'] or 0),
                'volume': float(r['volume'] or 0),
                'amount': float(r['amount'] or 0),
                'vwap': vwap_val
            })
        
        df = pd.DataFrame(records)
        df['returns'] = df.groupby('code')['close'].pct_change()
        
        # pivot成多索引宽表
        self.data = df.pivot(index='date', columns='code')
        self.codes = sorted(df['code'].unique())
        self.dates = sorted(df['date'].unique())
        
        print(f"  ✅ {len(self.dates)}天 × {len(self.codes)}只 = {len(self.dates)*len(self.codes):,}格")
        return self.data
    
    def _get(self, field):
        """获取某个数据字段的完整宽表"""
        if self.data is None:
            raise ValueError("请先调用 load_data()")
        cols = [(field, c) for c in self.codes if (field, c) in self.data.columns]
        return self.data[cols]
    
    @property
    def open(self):
        return self._get('open')
    
    @property
    def high(self):
        return self._get('high')
    
    @property
    def low(self):
        return self._get('low')
    
    @property
    def close(self):
        return self._get('close')
    
    @property
    def volume(self):
        return self._get('volume')
    
    @property
    def amount(self):
        return self._get('amount')
    
    @property
    def vwap(self):
        return self._get('vwap')
    
    @property
    def returns(self):
        return self._get('returns')
    
    # ============================================================
    # Alpha 因子（前30个，搬运自 alpha191.py）
    # 这些都是 WorldQuant 风格的alpha因子
    # ============================================================
    
    def alpha001(self):
        """(-1 * CORR(RANK(DELTA(LOG(VOLUME), 1)), RANK(((CLOSE - OPEN) / OPEN)), 6))"""
        return (-1 * Corr(Rank(Delta(Log(self.volume), 1)), 
                          Rank(((self.close - self.open) / self.open)), 6))
    
    def alpha002(self):
        """-1 * delta((((close-low)-(high-close))/(high-low)),1)"""
        return -1 * Delta((((self.close - self.low) - (self.high - self.close)) / 
                          (self.high - self.low)), 1)
    
    def alpha003(self):
        """SUM((CLOSE=DELAY(CLOSE,1)?0:CLOSE-(CLOSE>DELAY(CLOSE,1)?MIN(LOW,DELAY(CLOSE,1)):MAX(HIGH,DELAY(CLOSE,1)))),6)"""
        cond1 = (self.close == Delay(self.close, 1))
        cond2 = (self.close > Delay(self.close, 1))
        part = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        part[cond2] = (self.close - Min(self.low, Delay(self.close, 1)))
        part[~cond2 & ~cond1] = (self.close - Max(self.high, Delay(self.close, 1)))
        return Sum(part, 6)
    
    def alpha004(self):
        """((((SUM(CLOSE, 8) / 8) + STD(CLOSE, 8)) < (SUM(CLOSE, 2) / 2)) ? -1 : ...)"""
        cond1 = ((Sum(self.close, 8)/8 + Std(self.close, 8)) < Sum(self.close, 2)/2)
        cond2 = ((Sum(self.close, 8)/8 - Std(self.close, 8)) > Sum(self.close, 2)/2)
        cond3 = (self.volume / Mean(self.volume, 20) >= 1)
        result = pd.DataFrame(-1.0, index=self.close.index, columns=self.close.columns)
        result[cond2] = 1.0
        result[~cond1 & ~cond2 & cond3] = 1.0
        return result
    
    def alpha005(self):
        """(-1 * TSMAX(CORR(TSRANK(VOLUME, 5), TSRANK(HIGH, 5), 5), 3))"""
        return -1 * Tsmax(Corr(Tsrank(self.volume, 5), Tsrank(self.high, 5), 5), 3)
    
    def alpha006(self):
        """(RANK(SIGN(DELTA((((OPEN * 0.85) + (HIGH * 0.15))), 4)))* -1)"""
        return -1 * Rank(Sign(Delta(((self.open * 0.85) + (self.high * 0.15)), 4)))
    
    def alpha007(self):
        """((RANK(MAX((VWAP - CLOSE), 3)) + RANK(MIN((VWAP - CLOSE), 3))) * RANK(DELTA(VOLUME, 3)))"""
        return ((Rank(Tsmax((self.vwap - self.close), 3)) + 
                 Rank(Tsmin((self.vwap - self.close), 3))) * 
                Rank(Delta(self.volume, 3)))
    
    def alpha008(self):
        """RANK(DELTA(((((HIGH + LOW) / 2) * 0.2) + (VWAP * 0.8)), 4) * -1)"""
        return Rank(Delta(((((self.high + self.low) / 2) * 0.2) + (self.vwap * 0.8)), 4) * -1)
    
    def alpha009(self):
        """SMA(((HIGH+LOW)/2-(DELAY(HIGH,1)+DELAY(LOW,1))/2)*(HIGH-LOW)/VOLUME,7,2)"""
        return Sma(((self.high + self.low)/2 - 
                   (Delay(self.high,1) + Delay(self.low,1))/2) * 
                   (self.high - self.low) / self.volume, 7, 2)
    
    def alpha010(self):
        """(RANK(MAX(((RET < 0) ? STD(RET, 20) : CLOSE)^2),5))""" 
        cond = (self.returns < 0)
        part = pd.DataFrame(np.nan, index=self.returns.index, columns=self.returns.columns)
        part[cond] = Std(self.returns, 20)
        part[~cond] = self.close
        part = part ** 2
        return Rank(Tsmax(part, 5))
    
    def alpha011(self):
        """SUM(((CLOSE-LOW)-(HIGH-CLOSE))/(HIGH-LOW)*VOLUME,6)"""
        return Sum(((self.close - self.low) - (self.high - self.close)) / 
                  (self.high - self.low) * self.volume, 6)
    
    def alpha012(self):
        """(RANK((OPEN - (SUM(VWAP, 10) / 10)))) * (-1 * (RANK(ABS((CLOSE - VWAP)))))"""
        return (Rank((self.open - (Sum(self.vwap, 10) / 10)))) * \
               (-1 * (Rank(Abs((self.close - self.vwap)))))
    
    def alpha013(self):
        """(((HIGH * LOW)^0.5) - VWAP)"""
        return (((self.high * self.low) ** 0.5) - self.vwap)
    
    def alpha014(self):
        """CLOSE - DELAY(CLOSE, 5)"""
        return self.close - Delay(self.close, 5)
    
    def alpha015(self):
        """OPEN / DELAY(CLOSE, 1) - 1"""
        return self.open / Delay(self.close, 1) - 1
    
    def alpha016(self):
        """(-1 * TSMAX(RANK(CORR(RANK(VOLUME), RANK(VWAP), 5)), 5))"""
        return (-1 * Tsmax(Rank(Corr(Rank(self.volume), Rank(self.vwap), 5)), 5))
    
    def alpha017(self):
        """RANK((VWAP - MAX(VWAP, 15)))^DELTA(CLOSE, 5)"""
        return Rank((self.vwap - Tsmax(self.vwap, 15))) ** Delta(self.close, 5)
    
    def alpha018(self):
        """CLOSE / DELAY(CLOSE, 5)"""
        return self.close / Delay(self.close, 5)
    
    def alpha019(self):
        """(CLOSE<DELAY(CLOSE,5)?(CLOSE-DELAY(CLOSE,5))/DELAY(CLOSE,5):(CLOSE=DELAY(CLOSE,5)?0:(CLOSE-DELAY(CLOSE,5))/CLOSE))"""
        cond1 = (self.close < Delay(self.close, 5))
        cond2 = (self.close == Delay(self.close, 5))
        part = pd.DataFrame(np.nan, index=self.close.index, columns=self.close.columns)
        part[cond1] = (self.close - Delay(self.close, 5)) / Delay(self.close, 5)
        part[cond2] = 0
        part[~cond1 & ~cond2] = (self.close - Delay(self.close, 5)) / self.close
        return part
    
    def alpha020(self):
        """(CLOSE - DELAY(CLOSE, 6)) / DELAY(CLOSE, 6) * 100"""
        return (self.close - Delay(self.close, 6)) / Delay(self.close, 6) * 100
    
    def alpha031(self):
        """(CLOSE - MEAN(CLOSE, 12)) / MEAN(CLOSE, 12) * 100"""
        return (self.close - Mean(self.close, 12)) / Mean(self.close, 12) * 100
    
    def alpha032(self):
        """(-1 * SUM(RANK(CORR(RANK(HIGH), RANK(VOLUME), 3)), 3))"""
        return (-1 * Sum(Rank(Corr(Rank(self.high), Rank(self.volume), 3)), 3))
    
    def alpha034(self):
        """MEAN(CLOSE, 12) / CLOSE"""
        return Mean(self.close, 12) / self.close
    
    def alpha036(self):
        """RANK(SUM(CORR(RANK(VOLUME), RANK(VWAP), 6), 2))"""
        return Rank(Sum(Corr(Rank(self.volume), Rank(self.vwap), 6), 2))
    
    def alpha040(self):
        """SUM(CLOSE>DELAY(CLOSE,1)?VOLUME:0,26)/SUM(CLOSE<=DELAY(CLOSE,1)?VOLUME:0,26)*100"""
        cond = (self.close > Delay(self.close, 1))
        up_vol = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        up_vol[cond] = self.volume
        dn_vol = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        dn_vol[~cond] = self.volume
        return Sum(up_vol, 26) / Sum(dn_vol, 26) * 100
    
    def alpha043(self):
        """SUM((CLOSE>DELAY(CLOSE,1)?VOLUME:(CLOSE<DELAY(CLOSE,1)?-VOLUME:0)),6)"""
        cond1 = (self.close > Delay(self.close, 1))
        cond2 = (self.close < Delay(self.close, 1))
        part = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        part[cond1] = self.volume
        part[cond2] = -self.volume
        return Sum(part, 6)
    
    def alpha046(self):
        """(MEAN(CLOSE,3)+MEAN(CLOSE,6)+MEAN(CLOSE,12)+MEAN(CLOSE,24))/(4*CLOSE)"""
        return (Mean(self.close, 3) + Mean(self.close, 6) + 
                Mean(self.close, 12) + Mean(self.close, 24)) / (4 * self.close)
    
    def alpha047(self):
        """SMA((TSMAX(HIGH,6)-CLOSE)/(TSMAX(HIGH,6)-TSMIN(LOW,6))*100,9,1)"""
        return Sma((Tsmax(self.high, 6) - self.close) / 
                  (Tsmax(self.high, 6) - Tsmin(self.low, 6)) * 100, 9, 1)
    
    def alpha048(self):
        """(-1*((RANK(((SIGN((CLOSE-DELAY(CLOSE,1)))+SIGN((DELAY(CLOSE,1)-DELAY(CLOSE,2))))+SIGN((DELAY(CLOSE,2)-DELAY(CLOSE,3))))))*SUM(VOLUME,5))/SUM(VOLUME,20))"""
        return (-1 * ((Rank(((Sign((self.close - Delay(self.close, 1))) + 
                             Sign((Delay(self.close, 1) - Delay(self.close, 2)))) + 
                            Sign((Delay(self.close, 2) - Delay(self.close, 3)))))) * 
                     Sum(self.volume, 5)) / Sum(self.volume, 20))
    
    # ──────── 以下是不依赖vwap的额外alpha ────────
    
    def alpha055(self):
        """(CLOSE - TSMIN(LOW,12)) / (TSMAX(HIGH,12) - TSMIN(LOW,12)) * 100"""
        return (self.close - Tsmin(self.low, 12)) / (Tsmax(self.high, 12) - Tsmin(self.low, 12)) * 100
    
    def alpha056(self):
        """(-1 * RANK((OPEN - TSMIN(OPEN, 12)) / (TSMAX(HIGH,12) - TSMIN(HIGH,12))))"""
        return (-1 * Rank((self.open - Tsmin(self.open, 12)) / 
                         (Tsmax(self.high, 12) - Tsmin(self.high, 12))))
    
    def alpha061(self):
        """(-1 * RANK((DELTA(CLOSE, 1) + CORR(CLOSE, VOLUME, 10) * (CLOSE - OPEN) / (OPEN))))"""
        return (-1 * Rank((Delta(self.close, 1) + 
                          Corr(self.close, self.volume, 10) * 
                          (self.close - self.open) / (self.open))))
    
    def alpha062(self):
        """(-1 * CORR(HIGH, VOLUME, 5))"""
        return (-1 * Corr(self.high, self.volume, 5))
    
    def alpha064(self):
        """(CLOSE - OPEN) / OPEN * VOLUME"""
        return (self.close - self.open) / self.open * self.volume
    
    def alpha084(self):
        """SUM((CLOSE-DELAY(CLOSE,1)>0?CLOSE-DELAY(CLOSE,1):0),20)"""
        diff = self.close - Delay(self.close, 1)
        pos = pd.DataFrame(0.0, index=diff.index, columns=diff.columns)
        pos[diff > 0] = diff[diff > 0]
        return Sum(pos, 20)
    
    def alpha087(self):
        """(-1 * RANK(CLOSE - OPEN) + RANK(HIGH - LOW))"""
        return (-1 * Rank(self.close - self.open) + Rank(self.high - self.low))
    
    def alpha089(self):
        """(-1 * RANK(CORR(RANK(HIGH), RANK(VOLUME), 3)) * RANK(CORR(RANK(VOLUME), RANK(HIGH), 5)))"""
        corr1 = Corr(Rank(self.high), Rank(self.volume), 3)
        corr2 = Corr(Rank(self.volume), Rank(self.high), 5)
        return (-1 * Rank(corr1) * Rank(corr2))
    
    def alpha092(self):
        """(-1 * RANK((DELTA(CLOSE, 3) * (1 - RANK(DECAYLINEAR(VOLUME/MEAN(VOLUME,20), 9))))))"""
        return (-1 * Rank((Delta(self.close, 3) * 
                          (1 - Rank(Decaylinear(self.volume / Mean(self.volume, 20), 9))))))
    
    def alpha094(self):
        """(-1 * RANK((CLOSE - OPEN) / OPEN) * RANK(VOLUME))"""
        return (-1 * Rank((self.close - self.open) / self.open) * Rank(self.volume))
    
    def alpha102(self):
        """(-1 * RANK(CLOSE - OPEN) * RANK(CLOSE - DELAY(CLOSE, 1)) * RANK(HIGH - LOW))"""
        return (-1 * Rank(self.close - self.open) * 
                Rank(self.close - Delay(self.close, 1)) * 
                Rank(self.high - self.low))
    
    def alpha108(self):
        """(-1 * RANK((HIGH - LOW) / CLOSE) * RANK(HIGH / LOW))"""
        return (-1 * Rank((self.high - self.low) / self.close) * Rank(self.high / self.low))
    
    # ──────── 可用的因子方法列表 ────────
    
    def get_available_alphas(self):
        """返回可以计算的alpha方法列表"""
        methods = []
        for m in dir(self):
            if m.startswith('alpha') and callable(getattr(self, m)):
                methods.append(m)
        return sorted(methods)
    
    def compute_alpha(self, method_name):
        """计算单个alpha因子"""
        if not hasattr(self, method_name):
            raise ValueError(f"未知因子: {method_name}")
        fn = getattr(self, method_name)
        return fn()
    
    def compute_all(self, verbose=True):
        """计算全部可用的alpha因子，返回DataFrame {date+code: value}"""
        methods = self.get_available_alphas()
        if verbose:
            print(f"  计算 {len(methods)} 个Alpha因子...")
        
        results = {}
        for i, m in enumerate(methods):
            t0 = time.time()
            try:
                val = self.compute_alpha(m)
                results[m] = val
                if verbose:
                    print(f"    [{i+1}/{len(methods)}] {m} {time.time()-t0:.1f}s", end='')
                    # 统计有效数据量
                    if val is not None:
                        valid = val.notna().sum().sum()
                        print(f" 有效{valid:,}格")
            except Exception as e:
                if verbose:
                    print(f"    [{i+1}/{len(methods)}] {m} ❌ {e}")
        
        # 重塑为{code_date: alpha_value} 长表格式
        output = {}
        for m, df_val in results.items():
            if df_val is None or df_val.empty:
                continue
            # unpivot (MultiIndex列: field, code -> 堆叠成两列: code, value)
            for date_idx in df_val.index:
                row = df_val.loc[date_idx]
                if isinstance(row, pd.Series) and row.ndim == 1:
                    for col_name in row.index:
                        if isinstance(col_name, tuple) and len(col_name) == 2:
                            code = col_name[1] if col_name[0] == 'open' else col_name[1]
                        else:
                            code = col_name
                        v = row[col_name]
                        if pd.notna(v):
                            output[(code, str(date_idx), m)] = float(v)
                elif isinstance(row, pd.DataFrame):
                    for col in row.columns:
                        for date2 in row.index:
                            v = row.loc[date2, col]
                            if pd.notna(v):
                                output[(col, str(date2), m)] = float(v)
        
        print(f"  ✅ 共产出 {len(output):,} 条因子值")
        return output


# ============================================================
# 主入口：测试
# ============================================================

if __name__ == '__main__':
    import pymysql
    
    af = AlphaFactory()
    af.load_data('2024-01-02', '2026-07-10')
    
    # 测试计算前5个alpha
    for alpha_name in ['alpha001', 'alpha002', 'alpha003', 'alpha011', 'alpha014']:
        t0 = time.time()
        try:
            result = af.compute_alpha(alpha_name)
            if result is not None:
                valid = result.notna().sum().sum()
                print(f"\n{alpha_name}: {valid:,}有效值, {time.time()-t0:.1f}s")
        except Exception as e:
            print(f"\n{alpha_name}: ❌ {e}")
    
    # 全量计算
    print("\n尝试全量计算...")
    results = af.compute_all(verbose=True)
    
    # 统计每个因子有效记录数
    from collections import Counter
    alpha_counter = Counter()
    for key in results:
        alpha_counter[key[2]] += 1
    
    print(f"\n{'='*60}")
    print(f"  因子产出统计")
    print(f"{'='*60}")
    print(f"  {'因子':15s} {'有效记录':>10s}")
    print(f"  {'─'*27}")
    for name, cnt in sorted(alpha_counter.items(), key=lambda x: -x[1]):
        print(f"  {name:15s} {cnt:>10,}")
