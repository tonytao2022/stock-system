#!/usr/bin/env python3
# 简化版投资预测模型测试

import json
import sys
import os

class SimpleStockAnalyzer:
    """简化版股票分析器"""
    
    def __init__(self):
        self.mock_data = self._create_mock_data()
        
    def _create_mock_data(self):
        """创建模拟数据"""
        return {
            '300750': {  # 宁德时代
                'name': '宁德时代',
                'industry': '电气设备',
                'current_price': 180.5,
                'change_percent': 2.3,
                'roe': 18.5,
                'revenue_growth': 25.3,
                'gross_margin': 28.7,
                'debt_ratio': 45.2,
                'rsi': 65.2,
                'volume_ratio': 1.8,
                'fund_flow': 150000000,
                'volatility': 2.5,
                'market_position': 8.5,
                'industry_rank': 9.0
            },
            '300274': {  # 阳光电源
                'name': '阳光电源',
                'industry': '电气设备',
                'current_price': 85.3,
                'change_percent': 1.8,
                'roe': 22.1,
                'revenue_growth': 35.7,
                'gross_margin': 32.5,
                'debt_ratio': 38.9,
                'rsi': 72.3,
                'volume_ratio': 2.1,
                'fund_flow': 85000000,
                'volatility': 3.2,
                'market_position': 7.8,
                'industry_rank': 8.5
            },
            '300476': {  # 胜宏科技
                'name': '胜宏科技',
                'industry': '电子',
                'current_price': 45.6,
                'change_percent': -0.5,
                'roe': 12.8,
                'revenue_growth': 15.4,
                'gross_margin': 25.3,
                'debt_ratio': 52.7,
                'rsi': 48.5,
                'volume_ratio': 0.9,
                'fund_flow': -25000000,
                'volatility': 4.1,
                'market_position': 6.2,
                'industry_rank': 7.0
            }
        }
    
    def calculate_fundamental_score(self, data):
        """计算基本面得分（30分）"""
        score = 0
        
        # ROE评分 (10分)
        roe = data['roe']
        if roe >= 20: score += 10
        elif roe >= 15: score += 8
        elif roe >= 10: score += 6
        elif roe >= 5: score += 4
        else: score += 2
        
        # 营收增长评分 (10分)
        revenue_growth = data['revenue_growth']
        if revenue_growth >= 30: score += 10
        elif revenue_growth >= 20: score += 8
        elif revenue_growth >= 10: score += 6
        elif revenue_growth >= 0: score += 4
        else: score += 2
        
        # 毛利率评分 (5分)
        gross_margin = data['gross_margin']
        if gross_margin >= 40: score += 5
        elif gross_margin >= 30: score += 4
        elif gross_margin >= 20: score += 3
        elif gross_margin >= 10: score += 2
        else: score += 1
        
        # 负债率评分 (5分，负债率越低越好)
        debt_ratio = data['debt_ratio']
        if debt_ratio <= 30: score += 5
        elif debt_ratio <= 40: score += 4
        elif debt_ratio <= 50: score += 3
        elif debt_ratio <= 60: score += 2
        else: score += 1
        
        return score
    
    def calculate_technical_score(self, data):
        """计算技术面得分（25分）"""
        score = 0
        
        # RSI动量评分 (8分)
        rsi = data['rsi']
        if 30 <= rsi <= 70:  # 正常范围
            if 40 <= rsi <= 60: score += 8  # 最佳范围
            elif 30 <= rsi < 40 or 60 < rsi <= 70: score += 6
        else:
            score += 3  # 超买或超卖
        
        # 成交量比率评分 (7分)
        volume_ratio = data['volume_ratio']
        if volume_ratio >= 1.5: score += 7
        elif volume_ratio >= 1.2: score += 6
        elif volume_ratio >= 1.0: score += 5
        elif volume_ratio >= 0.8: score += 4
        else: score += 3
        
        # 价格动量评分 (10分)
        change = data['change_percent']
        if change >= 5: score += 10
        elif change >= 3: score += 8
        elif change >= 1: score += 6
        elif change >= 0: score += 4
        else: score += 2
        
        return score
    
    def calculate_sentiment_score(self, data):
        """计算情绪面得分（25分）"""
        score = 0
        
        # 资金流向评分 (8分)
        fund_flow = data['fund_flow']
        if fund_flow >= 100000000: score += 8
        elif fund_flow >= 50000000: score += 7
        elif fund_flow >= 10000000: score += 6
        elif fund_flow >= 0: score += 4
        else: score += 2
        
        # 波动率评分 (7分，波动率越低越好)
        volatility = data['volatility']
        if volatility <= 2: score += 7
        elif volatility <= 3: score += 6
        elif volatility <= 4: score += 5
        elif volatility <= 5: score += 4
        else: score += 3
        
        # 市场情绪综合 (10分)
        market_score = (data['change_percent'] * 2 + min(data['volume_ratio'], 2) * 3) / 5
        score += min(int(market_score), 10)
        
        return score
    
    def calculate_cycle_score(self, data):
        """计算周期面得分（20分）"""
        score = 0
        
        # 市场周期位置评分 (10分)
        market_position = data['market_position']
        if market_position >= 9: score += 10
        elif market_position >= 8: score += 8
        elif market_position >= 7: score += 6
        elif market_position >= 6: score += 4
        else: score += 2
        
        # 行业地位评分 (10分)
        industry_rank = data['industry_rank']
        if industry_rank >= 9: score += 10
        elif industry_rank >= 8: score += 8
        elif industry_rank >= 7: score += 6
        elif industry_rank >= 6: score += 4
        else: score += 2
        
        return score
    
    def analyze_stock(self, stock_code):
        """分析单只股票"""
        if stock_code not in self.mock_data:
            return None
        
        data = self.mock_data[stock_code]
        
        # 计算各维度得分
        fundamental = self.calculate_fundamental_score(data)
        technical = self.calculate_technical_score(data)
        sentiment = self.calculate_sentiment_score(data)
        cycle = self.calculate_cycle_score(data)
        
        total_score = fundamental + technical + sentiment + cycle
        
        # 生成投资建议
        if total_score >= 85:
            recommendation = "🔥 强烈买入"
            action = "BUY_STRONG"
        elif total_score >= 75:
            recommendation = "✅ 买入"
            action = "BUY"
        elif total_score >= 65:
            recommendation = "🔄 持有"
            action = "HOLD"
        elif total_score >= 55:
            recommendation = "⚠️ 谨慎持有"
            action = "HOLD_CAUTIOUS"
        else:
            recommendation = "❌ 卖出"
            action = "SELL"
        
        return {
            'stock_code': stock_code,
            'stock_name': data['name'],
            'industry': data['industry'],
            'current_price': data['current_price'],
            'change_percent': data['change_percent'],
            'scores': {
                'fundamental': fundamental,
                'technical': technical,
                'sentiment': sentiment,
                'cycle': cycle,
                'total': total_score
            },
            'recommendation': recommendation,
            'action': action,
            'key_insights': self._generate_insights(data, fundamental, technical, sentiment, cycle)
        }
    
    def _generate_insights(self, data, fundamental, technical, sentiment, cycle):
        """生成关键洞察"""
        insights = []
        
        # 基本面洞察
        if data['roe'] >= 20:
            insights.append("💪 ROE表现优秀，盈利能力强劲")
        elif data['roe'] < 10:
            insights.append("⚠️ ROE偏低，需关注盈利能力")
        
        if data['revenue_growth'] >= 30:
            insights.append("🚀 营收高速增长，成长性突出")
        elif data['revenue_growth'] < 10:
            insights.append("📉 营收增长放缓，需关注业务发展")
        
        # 技术面洞察
        if data['rsi'] > 70:
            insights.append("📈 RSI显示可能超买，注意短期回调风险")
        elif data['rsi'] < 30:
            insights.append("📉 RSI显示可能超卖，存在反弹机会")
        
        if data['volume_ratio'] > 1.5:
            insights.append("💰 成交量活跃，市场关注度高")
        elif data['volume_ratio'] < 0.8:
            insights.append("🔇 成交量低迷，市场关注度不足")
        
        # 情绪面洞察
        if data['fund_flow'] > 0:
            insights.append("📊 资金呈净流入状态，市场情绪积极")
        else:
            insights.append("💸 资金呈净流出状态，需谨慎对待")
        
        # 周期面洞察
        if data['market_position'] >= 8:
            insights.append("🌟 市场地位稳固，竞争优势明显")
        
        if data['industry_rank'] >= 8:
            insights.append("🏆 行业地位领先，具备龙头潜力")
        
        return insights
    
    def analyze_multiple_stocks(self, stock_codes):
        """分析多只股票"""
        results = []
        for code in stock_codes:
            result = self.analyze_stock(code)
            if result:
                results.append(result)
        
        # 按总分排序
        results.sort(key=lambda x: x['scores']['total'], reverse=True)
        return results
    
    def generate_report(self, results):
        """生成分析报告"""
        report = []
        report.append("=" * 60)
        report.append("📊 股票投资分析报告")
        report.append("=" * 60)
        report.append(f"分析时间: 2026-04-10 17:20")
        report.append(f"分析股票数量: {len(results)}")
        report.append("")
        
        for i, result in enumerate(results, 1):
            report.append(f"{i}. {result['stock_name']} ({result['stock_code']})")
            report.append(f"   行业: {result['industry']}")
            report.append(f"   当前价格: {result['current_price']}元 ({result['change_percent']:+}%)")
            report.append(f"   综合评分: {result['scores']['total']}/100")
            report.append(f"   投资建议: {result['recommendation']}")
            report.append(f"   得分详情:")
            report.append(f"     - 基本面: {result['scores']['fundamental']}/30")
            report.append(f"     - 技术面: {result['scores']['technical']}/25")
            report.append(f"     - 情绪面: {result['scores']['sentiment']}/25")
            report.append(f"     - 周期面: {result['scores']['cycle']}/20")
            report.append(f"   关键洞察:")
            for insight in result['key_insights'][:3]:  # 显示前3个关键洞察
                report.append(f"     • {insight}")
            report.append("")
        
        # 推荐总结
        report.append("🎯 投资推荐总结")
        report.append("-" * 40)
        buy_stocks = [r for r in results if r['action'] in ['BUY_STRONG', 'BUY']]
        if buy_stocks:
            report.append("推荐买入:")
            for stock in buy_stocks[:3]:  # 显示前3个推荐
                report.append(f"  • {stock['stock_name']} ({stock['stock_code']}) - {stock['recommendation']}")
        else:
            report.append("暂无强烈买入推荐")
        
        report.append("")
        report.append("⚠️ 风险提示: 本分析基于模拟数据，仅供参考。")
        report.append("实际投资需结合更多因素，谨慎决策。")
        report.append("=" * 60)
        
        return "\n".join(report)

def main():
    """主函数"""
    print("🚀 启动简化版投资预测模型测试...")
    
    # 创建分析器
    analyzer = SimpleStockAnalyzer()
    
    # 分析关注的股票
    focus_stocks = ['300750', '300274', '300476']  # 宁德时代、阳光电源、胜宏科技
    print(f"📈 分析股票: {', '.join(focus_stocks)}")
    
    # 执行分析
    results = analyzer.analyze_multiple_stocks(focus_stocks)
    
    # 生成报告
    report = analyzer.generate_report(results)
    print(report)
    
    # 保存报告
    with open('simple_analysis_report.txt', 'w', encoding='utf-8') as f:
        f.write(report)
    
    print("✅ 分析完成！报告已保存到 simple_analysis_report.txt")
    
    # 输出JSON格式结果（用于后续处理）
    json_results = []
    for result in results:
        json_results.append({
            'stock_code': result['stock_code'],
            'stock_name': result['stock_name'],
            'total_score': result['scores']['total'],
            'recommendation': result['recommendation'],
            'action': result['action']
        })
    
    with open('simple_analysis_results.json', 'w', encoding='utf-8') as f:
        json.dump(json_results, f, ensure_ascii=False, indent=2)
    
    print("✅ JSON结果已保存到 simple_analysis_results.json")

if __name__ == "__main__":
    main()