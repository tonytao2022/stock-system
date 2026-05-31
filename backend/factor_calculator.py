# ⚠️ DEPRECATED: 此文件已废弃，保留仅作参考。
# 当前评分用 score_engine.py + engine/ 模块
# 数据源：Tushare Pro (daily_kline_qfq)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
陶的投资预测模型 - 因子计算模块
紧急开发版本 v1.0
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

class FactorCalculator:
    """因子计算器 - 计算四维分析因子"""
    
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = DATA_DIR / "tipm_data.db"
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
    
    def calculate_all_factors(self, stock_codes):
        """计算所有因子"""
        print("开始计算股票因子...")
        
        all_factors = {}
        
        for code in stock_codes:
            try:
                print(f"\n计算股票 {code} 的因子...")
                
                # 获取基础数据
                basic_data = self.get_basic_data(code)
                quotes_data = self.get_quotes_data(code)
                financials_data = self.get_financials_data(code)
                money_flow_data = self.get_money_flow_data(code)
                
                # 计算各类因子
                fundamental_factors = self.calculate_fundamental_factors(code, financials_data)
                technical_factors = self.calculate_technical_factors(code, quotes_data)
                sentiment_factors = self.calculate_sentiment_factors(code, quotes_data, money_flow_data)
                cycle_factors = self.calculate_cycle_factors(code, quotes_data, basic_data)
                
                # 汇总因子
                stock_factors = {
                    'basic_info': basic_data.to_dict('records')[0] if not basic_data.empty else {},
                    'fundamental': fundamental_factors,
                    'technical': technical_factors,
                    'sentiment': sentiment_factors,
                    'cycle': cycle_factors,
                    'timestamp': datetime.now().isoformat()
                }
                
                all_factors[code] = stock_factors
                
                # 打印因子摘要
                self.print_factor_summary(code, stock_factors)
                
            except Exception as e:
                print(f"计算股票 {code} 因子失败: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        return all_factors
    
    def get_basic_data(self, stock_code):
        """获取股票基本信息"""
        query = "SELECT * FROM stock_basic WHERE ts_code = ?"
        return pd.read_sql_query(query, self.conn, params=[stock_code])
    
    def get_quotes_data(self, stock_code, days=250):
        """获取行情数据"""
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        
        query = """
        SELECT * FROM daily_quotes 
        WHERE ts_code = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date DESC
        """
        return pd.read_sql_query(query, self.conn, params=[stock_code, start_date, end_date])
    
    def get_financials_data(self, stock_code):
        """获取财务数据"""
        query = """
        SELECT * FROM financials 
        WHERE ts_code = ? 
        ORDER BY end_date DESC 
        LIMIT 1
        """
        return pd.read_sql_query(query, self.conn, params=[stock_code])
    
    def get_money_flow_data(self, stock_code, days=30):
        """获取资金流向数据"""
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        
        query = """
        SELECT * FROM money_flow 
        WHERE ts_code = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date DESC
        """
        return pd.read_sql_query(query, self.conn, params=[stock_code, start_date, end_date])
    
    def calculate_fundamental_factors(self, stock_code, financials_data):
        """计算基本面因子"""
        factors = {}
        
        try:
            if not financials_data.empty:
                # ROE（净资产收益率）
                roe = financials_data.iloc[0]['roe']
                factors['roe'] = {
                    'value': float(roe),
                    'score': self.score_roe(float(roe)),
                    'weight': 10  # 总分10分
                }
                
                # 营收增长率（模拟）
                revenue_growth = self.estimate_revenue_growth(stock_code)
                factors['revenue_growth'] = {
                    'value': revenue_growth,
                    'score': self.score_revenue_growth(revenue_growth),
                    'weight': 10
                }
                
                # 毛利率
                gross_margin = financials_data.iloc[0]['gross_margin']
                factors['gross_margin'] = {
                    'value': float(gross_margin),
                    'score': self.score_gross_margin(float(gross_margin)),
                    'weight': 5
                }
                
                # 负债率
                debt_ratio = financials_data.iloc[0]['debt_ratio']
                factors['debt_ratio'] = {
                    'value': float(debt_ratio),
                    'score': self.score_debt_ratio(float(debt_ratio)),
                    'weight': 5
                }
                
                # 基本面总分（30分）
                total_score = sum(factor['score'] for factor in factors.values())
                factors['total_score'] = {
                    'value': total_score,
                    'max_score': 30,
                    'percentage': (total_score / 30) * 100
                }
            
        except Exception as e:
            print(f"计算基本面因子失败: {e}")
        
        return factors
    
    def calculate_technical_factors(self, stock_code, quotes_data):
        """计算技术面因子"""
        factors = {}
        
        try:
            if not quotes_data.empty and len(quotes_data) >= 20:
                quotes_data = quotes_data.sort_values('trade_date')
                quotes_data['close'] = pd.to_numeric(quotes_data['close'], errors='coerce')
                
                # 计算技术指标
                closes = quotes_data['close'].values
                
                # 1. 趋势强度 - 20日均线位置
                if len(closes) >= 20:
                    ma20 = np.mean(closes[-20:])
                    current_price = closes[-1]
                    ma_position = (current_price - ma20) / ma20
                    
                    factors['trend_strength'] = {
                        'value': ma_position,
                        'score': self.score_trend_strength(ma_position),
                        'weight': 10,
                        'ma20': ma20,
                        'current_price': current_price
                    }
                
                # 2. RSI相对强弱指标
                if len(closes) >= 14:
                    rsi = self.calculate_rsi(closes, period=14)
                    factors['rsi'] = {
                        'value': rsi,
                        'score': self.score_rsi(rsi),
                        'weight': 8
                    }
                
                # 3. 成交量比率
                if 'vol' in quotes_data.columns:
                    recent_vol = quotes_data['vol'].tail(5).mean()
                    avg_vol = quotes_data['vol'].mean()
                    volume_ratio = recent_vol / avg_vol if avg_vol > 0 else 1
                    
                    factors['volume_ratio'] = {
                        'value': volume_ratio,
                        'score': self.score_volume_ratio(volume_ratio),
                        'weight': 7
                    }
                
                # 技术面总分（25分）
                total_score = sum(factor['score'] for factor in factors.values())
                factors['total_score'] = {
                    'value': total_score,
                    'max_score': 25,
                    'percentage': (total_score / 25) * 100
                }
            
        except Exception as e:
            print(f"计算技术面因子失败: {e}")
        
        return factors
    
    def calculate_sentiment_factors(self, stock_code, quotes_data, money_flow_data):
        """计算情绪面因子"""
        factors = {}
        
        try:
            # 1. 价格动量（近期表现）
            if not quotes_data.empty and len(quotes_data) >= 10:
                recent_data = quotes_data.head(10)  # 最近10天
                if len(recent_data) >= 2:
                    price_change = (recent_data.iloc[0]['close'] - recent_data.iloc[-1]['close']) / recent_data.iloc[-1]['close']
                    
                    factors['price_momentum'] = {
                        'value': price_change,
                        'score': self.score_price_momentum(price_change),
                        'weight': 10
                    }
            
            # 2. 资金流向
            if not money_flow_data.empty:
                net_flow = money_flow_data['net_mf_amount'].mean()
                
                factors['money_flow'] = {
                    'value': net_flow,
                    'score': self.score_money_flow(net_flow),
                    'weight': 8
                }
            
            # 3. 波动率（风险情绪）
            if not quotes_data.empty and len(quotes_data) >= 20:
                returns = quotes_data['close'].pct_change().dropna()
                volatility = returns.std() * np.sqrt(252)  # 年化波动率
                
                factors['volatility'] = {
                    'value': volatility,
                    'score': self.score_volatility(volatility),
                    'weight': 7
                }
            
            # 情绪面总分（25分）
            total_score = sum(factor['score'] for factor in factors.values())
            factors['total_score'] = {
                'value': total_score,
                'max_score': 25,
                'percentage': (total_score / 25) * 100
            }
            
        except Exception as e:
            print(f"计算情绪面因子失败: {e}")
        
        return factors
    
    def calculate_cycle_factors(self, stock_code, quotes_data, basic_data):
        """计算周期面因子"""
        factors = {}
        
        try:
            # 1. 市场周期位置（基于价格走势）
            if not quotes_data.empty and len(quotes_data) >= 60:
                # 计算60日内的价格变化
                price_data = quotes_data.head(60)['close'].values
                if len(price_data) >= 2:
                    price_change_60d = (price_data[0] - price_data[-1]) / price_data[-1]
                    
                    factors['market_cycle'] = {
                        'value': price_change_60d,
                        'score': self.score_market_cycle(price_change_60d),
                        'weight': 10,
                        'description': self.describe_market_cycle(price_change_60d)
                    }
            
            # 2. 行业地位因子（简化版）
            industry = basic_data.iloc[0]['industry'] if not basic_data.empty else '未知'
            industry_score = self.score_industry_position(industry, stock_code)
            
            factors['industry_position'] = {
                'value': industry_score,
                'score': industry_score,
                'weight': 10,
                'industry': industry
            }
            
            # 周期面总分（20分）
            total_score = sum(factor['score'] for factor in factors.values())
            factors['total_score'] = {
                'value': total_score,
                'max_score': 20,
                'percentage': (total_score / 20) * 100
            }
            
        except Exception as e:
            print(f"计算周期面因子失败: {e}")
        
        return factors
    
    # ========== 评分函数 ==========
    
    def score_roe(self, roe):
        """ROE评分"""
        if roe > 0.20: return 10
        elif roe > 0.15: return 8
        elif roe > 0.10: return 6
        elif roe > 0.05: return 4
        else: return 2
    
    def score_revenue_growth(self, growth):
        """营收增长评分"""
        if growth > 0.30: return 10
        elif growth > 0.20: return 8
        elif growth > 0.10: return 6
        elif growth > 0: return 4
        else: return 2
    
    def score_gross_margin(self, margin):
        """毛利率评分"""
        if margin > 0.40: return 5
        elif margin > 0.30: return 4
        elif margin > 0.20: return 3
        elif margin > 0.10: return 2
        else: return 1
    
    def score_debt_ratio(self, ratio):
        """负债率评分（越低越好）"""
        if ratio < 0.30: return 5
        elif ratio < 0.40: return 4
        elif ratio < 0.50: return 3
        elif ratio < 0.60: return 2
        else: return 1
    
    def score_trend_strength(self, position):
        """趋势强度评分"""
        if position > 0.05: return 10  # 强势上涨
        elif position > 0: return 8    # 上涨
        elif position > -0.05: return 6 # 震荡
        elif position > -0.10: return 4 # 下跌
        else: return 2                 # 强势下跌
    
    def score_rsi(self, rsi):
        """RSI评分"""
        if 30 <= rsi <= 70: return 8   # 正常区间
        elif rsi > 70: return 4        # 超买
        elif rsi < 30: return 6        # 超卖（可能反弹）
        else: return 5
    
    def score_volume_ratio(self, ratio):
        """成交量比率评分"""
        if ratio > 1.5: return 7       # 放量
        elif ratio > 1.0: return 6     # 正常
        elif ratio > 0.5: return 5     # 缩量
        else: return 4
    
    def score_price_momentum(self, change):
        """价格动量评分"""
        if change > 0.10: return 10    # 强势上涨
        elif change > 0.05: return 8   # 上涨
        elif change > 0: return 6      # 微涨
        elif change > -0.05: return 4  # 微跌
        elif change > -0.10: return 2  # 下跌
        else: return 1                 # 大幅下跌
    
    def score_money_flow(self, flow):
        """资金流向评分"""
        if flow > 10000000: return 8   # 大幅流入
        elif flow > 0: return 6        # 流入
        elif flow > -10000000: return 4 # 流出
        else: return 2                 # 大幅流出
    
    def score_volatility(self, vol):
        """波动率评分（越低越好）"""
        if vol < 0.20: return 7        # 低波动
        elif vol < 0.30: return 6      # 正常波动
        elif vol < 0.40: return 5      # 较高波动
        elif vol < 0.50: return 4      # 高波动
        else: return 3                 # 极高波动
    
    def score_market_cycle(self, change):
        """市场周期评分"""
        if change > 0.20: return 10    # 牛市
        elif change > 0.10: return 8   # 上涨市
        elif change > 0: return 6      # 震荡市
        elif change > -0.10: return 4  # 下跌市
        else: return 2                 # 熊市
    
    def score_industry_position(self, industry, stock_code):
        """行业地位评分（简化版）"""
        # 根据行业和股票给予基础评分
        industry_scores = {
            '电力设备': 8,  # 阳光电源所在行业
            '电子': 9,      # 胜宏科技所在行业
            '医药': 7,
            '消费': 6,
            '金融': 5
        }
        return industry_scores.get(industry, 5)
    
    # ========== 辅助函数 ==========
    
    def estimate_revenue_growth(self, stock_code):
        """估计营收增长率（模拟）"""
        # 实际项目中应从财务数据计算
        growth_rates = {
            '300274.SZ': 0.15,  # 阳光电源
            '300476.SZ': 0.80   # 胜宏科技
        }
        return growth_rates.get(stock_code, 0.10)
    
    def calculate_rsi(self, prices, period=14):
        """计算RSI指标"""
        if len(prices) < period + 1:
            return 50  # 默认值
        
        deltas = np.diff(prices)
        seed = deltas[:period+1]
        up = seed[seed >= 0].sum() / period
        down = -seed[seed < 0].sum() / period
        
        if down == 0:
            return 100
        else:
            rs = up / down
            return 100 - (100 / (1 + rs))
    
    def describe_market_cycle(self, change):
        """描述市场周期"""
        if change > 0.20: return "牛市阶段"
        elif change > 0.10: return "上涨阶段"
        elif change > 0: return "震荡阶段"
        elif change > -0.10: return "调整阶段"
        else: return "熊市阶段"
    
    def print_factor_summary(self, stock_code, factors):
        """打印因子摘要"""
        print(f"\n{stock_code} 因子计算完成:")
        print("=" * 40)
        
        for factor_type in ['fundamental', 'technical', 'sentiment', 'cycle']:
            if factor_type in factors:
                factor_data = factors[factor_type]
                if 'total_score' in factor_data:
                    score_info = factor_data['total_score']
                    print(f"{factor_type:12s}: {score_info['value']:.1f}/{score_info['max_score']} ({score_info['percentage']:.1f}%)")
        
        # 计算总分
        total_score = 0
        max_score = 0
        for factor_type in ['fundamental', 'technical', 'sentiment', 'cycle']:
            if factor_type in factors and 'total_score' in factors[factor_type]:
                total_score += factors[factor_type]['total_score']['value']
                max_score += factors[factor_type]['total_score']['max_score']
        
        if max_score > 0:
            total_percentage = (total_score / max_score) * 100
            print(f"\n{'总分':12s}: {total_score:.1f}/{max_score} ({total_percentage:.1f}%)")
            
            # 给出初步评级
            rating = self.get_investment_rating(total_percentage)
            print(f"{'初步评级':12s}: {rating}")
    
    def get_investment_rating(self, percentage):
        """根据总分百分比给出投资评级"""
        if percentage >= 85:
            return "强烈买入"
        elif percentage >= 75:
            return "买入"
        elif percentage >= 65:
            return "持有"
        elif percentage >= 55:
            return "谨慎持有"
        else:
            return "卖出"
    
    def save_factors(self, factors, output_file=None):
        """保存因子数据"""
        if output_file is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = OUTPUT_DIR / f"factors_{timestamp}.json"
        
        import json
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(factors, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n因子数据已保存到: {output_file}")
        return output_file
    
    def close(self):
        """关闭连接"""
        if hasattr(self, 'conn'):
            self.conn.close()

def main():
    """主函数"""
    print("=" * 50)
    print("陶的投资预测模型 - 因子计算模块")
    print("=" * 50)
    
    # 试点股票
    stock_codes = ['300274.SZ', '300476.SZ']
    
    # 创建因子计算器
    calculator = FactorCalculator()
    
    try:
        # 计算所有因子
        factors = calculator.calculate_all_factors(stock_codes)
        
        if factors:
            # 保存因子数据
            output_file = calculator.save_factors(factors)
            
            # 生成简要报告
            print("\n" + "=" * 50)
            print("投资建议摘要:")
            print("=" * 50)
            
            for code, factor_data in factors.items():
                stock_name = factor_data.get('basic_info', {}).get('name', '未知')
                
                # 计算总分
                total_score = 0
                max_score = 0
                for factor_type in ['fundamental', 'technical', 'sentiment', 'cycle']:
                    if factor_type in factor_data and 'total_score' in factor_data[factor_type]:
                        total_score += factor_data[factor_type]['total_score']['value']
                        max_score += factor_data[factor_type]['total_score']['max_score']
                
                if max_score > 0:
                    percentage = (total_score / max_score) * 100
                    rating = calculator.get_investment_rating(percentage)
                    
                    print(f"\n{stock_name} ({code}):")
                    print(f"  综合评分: {total_score:.1f}/{max_score} ({percentage:.1f}%)")
                    print(f"  投资建议: {rating}")
                    
                    # 显示各维度评分
                    for factor_type in ['fundamental', 'technical', 'sentiment', 'cycle']:
                        if factor_type in factor_data and 'total_score' in factor_data[factor_type]:
                            score_info = factor_data[factor_type]['total_score']
                            print(f"  {factor_type}: {score_info['value']:.1f}/{score_info['max_score']}")
        
        print("\n因子计算完成！")
        
    except Exception as e:
        print(f"因子计算过程中出现错误: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        calculator.close()

if __name__ == "__main__":
    main()