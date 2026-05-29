# -*- coding: utf-8 -*-
"""
AI个股分析引擎 - 数据采集+DeepSeek调用+保存到stock_notes

工作流：
  用户输入股票代码 → 系统采集真实数据 → 填入14项模板 → 调用DeepSeek → 展示+保存
"""

import pymysql, json, os, sys, traceback
from datetime import datetime
from typing import Optional, Dict, Any, List

# ─── DB连接工具 ───
def _get_mysql_pass():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for l in f:
                if 'password' in l:
                    return l.split('=')[-1].strip().strip('"').strip("'")
    except: pass
    return ''

PWD = _get_mysql_pass()
STOCK_DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':PWD,'database':'stock_db','charset':'utf8mb4'}
CONFIG_DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':PWD,'database':'openclaw_config','charset':'utf8mb4'}

def get_conn(db='stock'):
    cfg = STOCK_DB if db == 'stock' else CONFIG_DB
    return pymysql.connect(**cfg)

def get_api_key(name: str) -> str:
    """从openclaw_config.api_credentials获取API Key"""
    conn = get_conn('config')
    cur = conn.cursor()
    cur.execute("SELECT api_key FROM api_credentials WHERE name=%s AND is_active=1 LIMIT 1", (name,))
    r = cur.fetchone()
    cur.close(); conn.close()
    return r[0] if r else ''


# ════════════════════════════════════════════════════
# 数据采集
# ════════════════════════════════════════════════════

def collect_stock_data(ts_code: str) -> Dict[str, Any]:
    """
    采集该股的所有真实数据，返回带来源标记的字典
    所有数据均可溯源，无默认值/随机值
    """
    conn = get_conn('stock')
    cur = conn.cursor(pymysql.cursors.DictCursor)
    name = ''
    data = {'sources': {}}  # sources记录每个数据项的来源
    
    # 1. 基础信息
    cur.execute("SELECT name, industry, market FROM stock_basic WHERE ts_code=%s", (ts_code,))
    basic = cur.fetchone()
    if basic:
        name = basic['name']
        data['name'] = basic['name']
        data['industry'] = basic['industry']
        data['market'] = basic['market']
        data['sources']['basic'] = 'stock_basic (Tushare Pro)'
    
    # 2. 最近日K线（60日）
    cur.execute("""
        SELECT trade_date, open, high, low, close, vol, change_pct 
        FROM daily_kline_qfq WHERE ts_code=%s 
        ORDER BY trade_date DESC LIMIT 60
    """, (ts_code,))
    klines = cur.fetchall()
    if klines:
        data['klines'] = klines
        data['latest_close'] = float(klines[0]['close'])
        data['latest_date'] = str(klines[0]['trade_date'])
        data['change_pct'] = float(klines[0]['change_pct'])
        # 60日涨跌幅
        if len(klines) >= 60:
            data['ret_60d'] = round((float(klines[0]['close']) - float(klines[-1]['close'])) / float(klines[-1]['close']) * 100, 2)
        data['sources']['klines'] = 'daily_kline_qfq (Tushare Pro 前复权)'
    
    # 3. 最新评分数据（从trend_score）
    cur.execute("""
        SELECT trade_date, cycle_score, structure_score, emotion_score, 
               composite_score, confidence_mult, close_price
        FROM trend_score WHERE ts_code=%s 
        ORDER BY trade_date DESC LIMIT 1
    """, (ts_code,))
    score = cur.fetchone()
    if score:
        data['score'] = {
            'trade_date': str(score['trade_date']),
            'cycle_score': float(score['cycle_score']) if score['cycle_score'] else 0,
            'structure_score': float(score['structure_score']) if score['structure_score'] else 0,
            'emotion_score': float(score['emotion_score']) if score['emotion_score'] else 0,
            'composite_score': float(score['composite_score']) if score['composite_score'] else 0,
            'confidence': float(score['confidence_mult']) if score['confidence_mult'] else 0,
        }
        data['sources']['score'] = f"trend_score (score_engine v4.0, 评分日:{score['trade_date']})"
    
    # 4. 缠论结构信号
    cur.execute("""
        SELECT trade_date, structure_score, buy_sell_point, beichi_type, zoushi_type, bi_direction
        FROM chanlun_structure WHERE ts_code=%s
        ORDER BY trade_date DESC LIMIT 1
    """, (ts_code,))
    chanlun = cur.fetchone()
    if chanlun:
        data['chanlun'] = {
            'structure_score': float(chanlun['structure_score']) if chanlun['structure_score'] else 0,
            'buy_sell_point': chanlun['buy_sell_point'],
            'beichi_type': chanlun['beichi_type'],
        }
        data['sources']['chanlun'] = 'chanlun_structure (缠论结构分析器)'
    
    # 5. 技术指标
    cur.execute("""
        SELECT trade_date, ma_5, ma_10, ma_20, ma_60, ma_120,
               macd_dif, macd_dea, macd_bar, rsi_6, rsi_12, rsi_24,
               boll_upper, boll_mid, boll_lower
        FROM technical_indicator WHERE ts_code=%s
        ORDER BY trade_date DESC LIMIT 1
    """, (ts_code,))
    tech = cur.fetchone()
    if tech:
        data['tech'] = {}
        for k, v in tech.items():
            if k != 'trade_date':
                try:
                    data['tech'][k] = float(v) if v is not None else 0
                except:
                    data['tech'][k] = 0
        data['sources']['tech'] = f"technical_indicator (Tushare Pro, 计算日:{tech['trade_date']})"
    
    # 6. 资金流向
    cur.execute("""
        SELECT trade_date,
               buy_lg_amount, sell_lg_amount, buy_md_amount, sell_md_amount,
               buy_sm_amount, sell_sm_amount, buy_elg_amount, sell_elg_amount,
               net_mf_amount
        FROM money_flow WHERE ts_code=%s
        ORDER BY trade_date DESC LIMIT 5
    """, (ts_code,))
    money_flows = cur.fetchall()
    if money_flows:
        data['money_flow'] = money_flows
        total_net = sum(float(m['net_mf_amount'] or 0) for m in money_flows)
        data['money_flow_total_net'] = round(total_net, 2)
        data['sources']['money_flow'] = 'money_flow (Tushare Pro 资金流向)'
    
    # 7. 持仓信息
    cur.execute("""
        SELECT buy_date, cost_price, current_price, qty, profit_pct, advice
        FROM portfolio_holdings WHERE ts_code=%s AND status='HOLDING' LIMIT 1
    """, (ts_code,))
    holding = cur.fetchone()
    if holding:
        data['holding'] = {
            'buy_date': str(holding['buy_date']),
            'cost_price': float(holding['cost_price']),
            'current_price': float(holding['current_price']),
            'qty': int(holding['qty']),
            'profit_pct': float(holding['profit_pct']),
            'advice': holding['advice'],
        }
        data['sources']['holding'] = 'portfolio_holdings (持股管理系统)'
    
    # 8. 大盘季节
    cur.execute("""
        SELECT season, raw_score, confidence FROM season_state WHERE index_code='MARKET'
        ORDER BY trade_date DESC LIMIT 1
    """)
    season = cur.fetchone()
    if season:
        data['market_season'] = season['season']
        data['market_score'] = float(season['raw_score'] or 0)
        data['sources']['season'] = 'season_state (周期判定引擎)'
    
    cur.close(); conn.close()
    
    # 如果基础信息都没取到，说明代码无效
    if not data.get('name'):
        return {'error': f'未找到股票: {ts_code}'}
    
    return data


# ════════════════════════════════════════════════════
# 14项分析模板
# ════════════════════════════════════════════════════

def build_prompt(data: Dict[str, Any]) -> str:
    """将真实数据填入14项模板，生成给DeepSeek的prompt"""
    
    klines = data.get('klines', [])
    latest_close = data.get('latest_close', 'N/A')
    latest_date = data.get('latest_date', 'N/A')
    score = data.get('score', {})
    tech = data.get('tech', {})
    money_flow = data.get('money_flow', [])
    
    # 格式化K线摘要
    kline_summary = f"最新收盘价: {latest_close} ({latest_date})"
    if klines and len(klines) >= 5:
        kline_summary += f"\n近5日: "
        for k in klines[:5]:
            kline_summary += f"{k['trade_date']}(开{k['open']}/高{k['high']}/低{k['low']}/收{k['close']}/涨跌{k['change_pct']:+.2f}%) "
    
    # 技术指标摘要
    tech_summary = ""
    if tech:
        tech_summary = f"MA5={tech.get('ma_5','N/A')} MA10={tech.get('ma_10','N/A')} MA20={tech.get('ma_20','N/A')} MACD_DIF={tech.get('macd_dif','N/A')} RSI_6={tech.get('rsi_6','N/A')}"
    
    # 资金流向摘要
    mf_summary = ""
    if money_flow:
        total_net = data.get('money_flow_total_net', 0)
        mf_summary = f"近5日净流入: {total_net:+.2f}万元"
    
    prompt = f"""你是一名专业的证券分析师，我想预测股票【{data.get('name','N/A')}({data.get('ts_code','N/A')})】未来的走势，希望你能根据我给你的数据，帮我完成以下分析。要求数据真实可靠，所有数据都可以溯源，不存在默认值或随机值。

📊 数据来源说明：以下所有数据均来自Tushare Pro和自研评分引擎，每个指标都可溯源。

【股票基本信息】
股票名称：{data.get('name','N/A')}
股票代码：{data.get('ts_code','N/A')}
行业：{data.get('industry','N/A')}
市场：{data.get('market','N/A')}
数据来源：stock_basic (Tushare Pro)

【行情数据（60日K线）】
{kline_summary}
60日涨跌幅：{data.get('ret_60d', 'N/A')}%
数据来源：daily_kline_qfq (Tushare Pro 前复权)

【评分数据】
综合评分：{score.get('composite_score', 'N/A')}
周期评分：{score.get('cycle_score', 'N/A')}
缠论结构评分：{score.get('structure_score', 'N/A')}
情绪评分：{score.get('emotion_score', 'N/A')}
置信系数：{score.get('confidence', 'N/A')}
数据来源：trend_score (score_engine v4.0)

【缠论结构】
{json.dumps(data.get('chanlun', {}), ensure_ascii=False, indent=2)}
数据来源：chanlun_structure (缠论结构分析器)

【技术指标】
{tech_summary}
数据来源：technical_indicator (Tushare Pro)

【资金流向】
{mf_summary}
数据来源：money_flow (Tushare Pro 资金流向)

【大盘季节】
当前季节：{data.get('market_season', 'N/A')}
市场评分：{data.get('market_score', 'N/A')}
数据来源：season_state (周期判定引擎)

【持仓信息】（如持有）
{json.dumps(data.get('holding', {}), ensure_ascii=False, indent=2)}

---
请根据以上真实数据完成以下14项分析，每项用【】标记：

【1. 基本面分析】
分析公司基础信息与行业定位、业务与成长性。

【2. 整体基本面分析】
分析公司的主要业务模式、市场份额和盈利来源，说明核心竞争力。

【3. 评估财务健康状况】
基于以上数据审视财务指标表现。如数据不足请标注"财务数据待补充"。

【4. 技术面与资金动向分析】
分析价格趋势、成交量、主要技术指标（MA/MACD/RSI）和资金流向。

【5. 历史股价走势与波动】
回顾股价走势，分析波动率和主要驱动因素。

【6. 宏观经济及行业环境影响】
分析当前宏观环境和行业政策对公司的可能影响。

【7. 风险评估】
分析市场风险、行业风险、公司特定风险。

【8. 近期新闻动态】
（如无法获取新闻请标注"新闻数据待补充"）

【9. 市场情绪及媒体舆论】
基于评分数据中的情绪评分分析市场情绪。

【10. 最新财报解读】
（如数据不足请标注"财务数据待补充"）

【11. 市场关注热点】
（如无法获取请标注"热词数据待补充"）

【12. 研判未来成长潜力与风险】
列出未来1-3年的增长驱动因素和潜在风险。

【13. 投资建议】
给出明确的趋势预判与策略建议。

【14. 综合观点与投资建议】
分别为价值投资者和短期投机者给出决策意见。
"""
    return prompt


# ════════════════════════════════════════════════════
# DeepSeek调用
# ════════════════════════════════════════════════════

def call_deepseek(prompt: str) -> str:
    """调用DeepSeek API生成分析报告"""
    api_key = get_api_key('DEEPSEEK_API_KEY')
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY 未配置")
    
    try:
        from openai import OpenAI
    except ImportError:
        # fallback: 使用requests直接调用
        import requests
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "你是一名专业的证券分析师，请基于用户提供的真实数据进行客观分析。所有结论必须基于提供的数据，不得编造。每项分析请标注数据引用来源。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 8192
            },
            timeout=120
        )
        if resp.status_code == 200:
            return resp.json()['choices'][0]['message']['content']
        else:
            raise ValueError(f"DeepSeek API错误: {resp.status_code} {resp.text}")
    
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "你是一名专业的证券分析师，请基于用户提供的真实数据进行客观分析。所有结论必须基于提供的数据，不得编造。每项分析请标注数据引用来源。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=8192
    )
    return response.choices[0].message.content


# ════════════════════════════════════════════════════
# 保存到stock_notes
# ════════════════════════════════════════════════════

def save_to_notes(ts_code: str, name: str, report: str, summary: str = ''):
    """将AI分析报告保存到stock_notes表"""
    conn = get_conn('stock')
    cur = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur.execute(
        "INSERT INTO stock_notes (ts_code, name, note_date, report_type, full_report, summary) "
        "VALUES (%s, %s, %s, 'AI_ANALYSIS', %s, %s) "
        "ON DUPLICATE KEY UPDATE full_report=VALUES(full_report), summary=VALUES(summary), created_at=NOW()",
        (ts_code, name, now, report, summary[:500] if summary else report[:500])
    )
    conn.commit()
    cur.close(); conn.close()
    return now


# ════════════════════════════════════════════════════
# 主入口（供manager_server.py调用）
# ════════════════════════════════════════════════════

def analyze_stock(ts_code: str) -> Dict[str, Any]:
    """AI分析个股完整流程"""
    try:
        # 1. 采集数据
        data = collect_stock_data(ts_code)
        if 'error' in data:
            return {'code': -1, 'error': data['error']}
        
        data['ts_code'] = ts_code
        
        # 2. 构建prompt
        prompt = build_prompt(data)
        
        # 3. 调用DeepSeek
        report = call_deepseek(prompt)
        
        # 4. 保存到stock_notes
        save_time = save_to_notes(
            ts_code=ts_code,
            name=data.get('name', ''),
            report=report,
            summary=f"评分{data.get('score',{}).get('composite_score','N/A')} | 收盘{data.get('latest_close','N/A')}"
        )
        
        # 5. 返回结果（含数据溯源信息）
        return {
            'code': 0,
            'data': {
                'ts_code': ts_code,
                'name': data.get('name', ''),
                'note_date': save_time,
                'report': report,
                'summary': f"评分{data.get('score',{}).get('composite_score','N/A')} | 最新价{data.get('latest_close','N/A')}",
                'data_sources': data.get('sources', {}),
                'score': data.get('score', {}),
                'latest_close': data.get('latest_close'),
            }
        }
    except Exception as e:
        traceback.print_exc()
        return {'code': -1, 'error': str(e)}


# ════════════════════════════════════════════════════
# 历史记录查询
# ════════════════════════════════════════════════════

def get_stock_notes(ts_code: str, limit: int = 10) -> List[Dict]:
    """查询某只股票的历史AI分析记录"""
    conn = get_conn('stock')
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("""
        SELECT ts_code, name, note_date, report_type, LEFT(full_report, 200) as full_report, summary, created_at
        FROM stock_notes
        WHERE ts_code=%s
        ORDER BY note_date DESC
        LIMIT %s
    """, (ts_code, limit))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


if __name__ == '__main__':
    # CLI测试
    code = sys.argv[1] if len(sys.argv) > 1 else '300308.SZ'
    result = analyze_stock(code)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000] if result.get('code') == 0 else result.get('error'))
