# -*- coding: utf-8 -*-
"""
AI个股分析引擎 - 数据采集+DeepSeek调用+保存到stock_notes

工作流：
  用户输入股票代码 → 系统采集真实数据 → 填入14项模板 → 调用DeepSeek → 展示+保存

v2.0 新增:
  - Bocha API 新闻搜索（含情感分类）
  - Tushare Pro 财务数据拉取（最近4个季度）
  - 【8. 近期新闻动态】和【10. 最新财报解读】数据填充
"""

import pymysql, json, os, sys, traceback, requests, re
from datetime import datetime, timedelta
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


# ─── 情感分析工具 ───
# 正面/负面关键词词表（用于对新闻标题做简单情感分类）
_POSITIVE_KEYWORDS = [
    '涨停', '大涨', '突破', '创新高', '利好', '净利增', '营收增', '增长',
    '扭亏', '盈喜', '业绩超预期', '分红', '回购', '增持', '中标', '签约',
    '合作', '获批', '核准', '放量', '扩张', '提速', '回暖', '反弹',
    '入选', '获准', '突破性', '里程碑', '加速'
]

_NEGATIVE_KEYWORDS = [
    '跌停', '大跌', '暴跌', '破位', '利空', '亏损', '下滑', '营收降', '净利降',
    '减持', '处罚', '诉讼', '违规', '调查', '暴雷', '风险提示', '退市',
    'st', '暂停交易', '停牌', '立案', '监管', '问询', '警示', '收缩',
    '裁员', '违约', '债务', '下调', '评级下调', '做空', '看空'
]

def _classify_sentiment(title: str, snippet: str = '') -> str:
    """基于关键词对新闻标题进行简单情感分类：正面/负面/中性"""
    text = (title + ' ' + snippet).lower()
    pos_score = sum(1 for kw in _POSITIVE_KEYWORDS if kw.lower() in text)
    neg_score = sum(1 for kw in _NEGATIVE_KEYWORDS if kw.lower() in text)
    if pos_score > neg_score:
        return '正面'
    elif neg_score > pos_score:
        return '负面'
    return '中性'


# ════════════════════════════════════════════════════
# 新闻搜索模块（Bocha API）
# ════════════════════════════════════════════════════

BOCHA_API_URL = "https://api.bocha.cn/v1/web-search"

def fetch_bocha_news(ts_code: str, stock_name: str, days: int = 7, max_results: int = 8) -> Dict[str, Any]:
    """
    通过 Bocha API 搜索最近 N 天关于该股票的新闻
    
    Args:
        ts_code: 股票代码（如 300750.SZ）
        stock_name: 股票名称（如 宁德时代）
        days: 搜索最近天数（默认7）
        max_results: 最大返回条数
        
    Returns:
        {
            'news': [{'title':..., 'source':..., 'date':..., 'sentiment':..., 'url':...}, ...],
            'source': 'Bocha API (api.bocha.cn)',
            'status': 'ok' | 'empty' | 'error'
        }
        如失败返回 {'status': 'error', 'message': ..., 'source': 'Bocha API'}
    """
    api_key = get_api_key('BOCHA_API_KEYS')
    if not api_key:
        return {'status': 'error', 'message': 'BOCHA_API_KEYS 未配置', 'source': 'Bocha API',
                'news': []}

    # 构建搜索查询
    code_clean = ts_code.split('.')[0]  # 取纯数字代码
    query = f"{stock_name} {code_clean} 股票 新闻"

    # 时间范围
    freshness = "oneWeek"
    if days <= 1:
        freshness = "oneDay"
    elif days <= 3:
        freshness = "threeDays"
    elif days <= 7:
        freshness = "oneWeek"
    elif days <= 30:
        freshness = "oneMonth"

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    payload = {
        "query": query,
        "freshness": freshness,
        "summary": True,
        "count": min(max_results, 50)
    }

    try:
        resp = requests.post(BOCHA_API_URL, headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            return {'status': 'error', 'message': f'Bocha HTTP {resp.status_code}',
                    'source': 'Bocha API', 'news': []}

        data = resp.json()
        if data.get('code') != 200:
            return {'status': 'error', 'message': data.get('msg', 'Bocha API返回异常'),
                    'source': 'Bocha API', 'news': []}

        web_pages = data.get('data', {}).get('webPages', {})
        value_list = web_pages.get('value', [])

        if not value_list:
            return {'status': 'empty', 'message': '未搜索到相关新闻',
                    'source': 'Bocha API', 'news': []}

        news_list = []
        for item in value_list[:max_results]:
            title = item.get('name', '')
            snippet = item.get('summary') or item.get('snippet', '')
            url = item.get('url', '')
            source = item.get('siteName', '')
            date_str = item.get('datePublished', '')

            # 格式化日期（去掉时区信息）
            pub_date = date_str
            try:
                if date_str:
                    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    pub_date = dt.strftime('%Y-%m-%d %H:%M')
            except (ValueError, AttributeError):
                pass

            # 情感分类
            sentiment = _classify_sentiment(title, snippet)

            news_list.append({
                'title': title[:200],
                'source': source[:50] if source else '未知来源',
                'date': pub_date,
                'sentiment': sentiment,
                'url': url,
                'snippet': (snippet or '')[:300],
            })

        return {
            'status': 'ok',
            'source': 'Bocha API (api.bocha.cn)',
            'news': news_list,
        }

    except requests.exceptions.Timeout:
        return {'status': 'error', 'message': 'Bocha API 请求超时', 'source': 'Bocha API',
                'news': []}
    except requests.exceptions.RequestException as e:
        return {'status': 'error', 'message': f'Bocha API 网络错误: {str(e)[:100]}',
                'source': 'Bocha API', 'news': []}
    except Exception as e:
        return {'status': 'error', 'message': f'新闻搜索异常: {str(e)[:100]}',
                'source': 'Bocha API', 'news': []}


# ════════════════════════════════════════════════════
# 财务数据模块（Tushare Pro）
# ════════════════════════════════════════════════════

def fetch_financial_data(ts_code: str) -> Dict[str, Any]:
    """
    从 Tushare Pro 拉取最近4个季度的财务数据
    
    获取指标：
      - fina_indicator: 基本每股收益(eps)、毛利率、净利率、资产负债率、流动比率、每股经营现金流
      - income: 营业收入、营业利润、净利润
    
    Returns:
        {
            'quarters': [Q1, Q2, Q3, Q4],  # 每季度数据字典
            'source': 'Tushare Pro (fina_indicator / income)',
            'status': 'ok' | 'empty' | 'error'
        }
    """
    token = get_api_key('TUSHARE_TOKEN')
    if not token:
        return {'status': 'error', 'message': 'TUSHARE_TOKEN 未配置', 'source': 'Tushare Pro',
                'quarters': []}

    try:
        import tushare as ts
        ts.set_token(token)
        pro = ts.pro_api()
    except ImportError:
        return {'status': 'error', 'message': 'tushare 包未安装', 'source': 'Tushare Pro',
                'quarters': []}
    except Exception as e:
        return {'status': 'error', 'message': f'Tushare初始化失败: {str(e)[:100]}',
                'source': 'Tushare Pro', 'quarters': []}

    # 计算最近4个季度的起止日期
    today = datetime.now()
    # 最近的年度报告结束日期（上一个完整季度末）
    current_year = today.year
    current_month = today.month

    # 确定最近几个季度的 end_date
    if current_month >= 10:
        # Q3 (9月30日) 是最新的完整季度
        end_dates = [
            f'{current_year}0930',  # 三季报
            f'{current_year}0630',  # 中报
            f'{current_year}0331',  # 一季报
            f'{current_year - 1}1231',  # 年报
        ]
    elif current_month >= 7:
        end_dates = [
            f'{current_year}0630',
            f'{current_year}0331',
            f'{current_year - 1}1231',
            f'{current_year - 1}0930',
        ]
    elif current_month >= 4:
        end_dates = [
            f'{current_year}0331',
            f'{current_year - 1}1231',
            f'{current_year - 1}0930',
            f'{current_year - 1}0630',
        ]
    else:  # 1-3月
        end_dates = [
            f'{current_year - 1}1231',
            f'{current_year - 1}0930',
            f'{current_year - 1}0630',
            f'{current_year - 1}0331',
        ]

    # 1) 获取财务指标数据（每股收益、毛利率、资产负债率等）
    # 注意：gross_margin在Tushare中是毛利润绝对值(元)，不是百分比
    # 使用 grossprofit_margin 获取毛利率，netprofit_margin 获取净利率
    fina_fields = 'ts_code,ann_date,end_date,eps,roe,' \
                  'grossprofit_margin,netprofit_margin,debt_to_assets,' \
                  'current_ratio,quick_ratio,ocfps'
    fina_data = {}
    try:
        for end_date in end_dates:
            df = pro.fina_indicator(ts_code=ts_code, end_date=end_date, 
                                    fields=fina_fields)
            if df is not None and not df.empty:
                # 取最新的一条（按ann_date降序）
                row = df.sort_values('ann_date', ascending=False).iloc[0]
                fina_data[end_date] = row.to_dict()
    except Exception as e:
        # 单次失败不中断，继续尝试其他日期
        pass

    # 2) 获取利润表数据（营业收入、营业利润、净利润）
    income_fields = 'ts_code,ann_date,end_date,revenue,operate_cost,' \
                    'operate_profit,n_income'
    income_data = {}
    try:
        for end_date in end_dates:
            df = pro.income(ts_code=ts_code, end_date=end_date, fields=income_fields)
            if df is not None and not df.empty:
                row = df.sort_values('ann_date', ascending=False).iloc[0]
                income_data[end_date] = row.to_dict()
    except Exception as e:
        pass

    # 3) 合并数据构建季度列表
    quarters = []
    for end_date in end_dates:
        quarter_info = {}

        # 季度名称映射
        mm = end_date[4:6]
        q_map = {'03': 'Q1', '06': 'Q2', '09': 'Q3', '12': 'Q4'}
        quarter_label = q_map.get(mm, f'Q{int(mm)//3}')
        year_str = end_date[:4]
        quarter_info['label'] = f'{year_str} {quarter_label}'
        quarter_info['end_date'] = f'{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}'

        # 填充财务指标
        fina_row = fina_data.get(end_date, {})
        income_row = income_data.get(end_date, {})

        if fina_row:
            quarter_info['eps'] = _safe_float(fina_row.get('eps'))
            quarter_info['roe'] = _safe_float(fina_row.get('roe'))
            quarter_info['gross_margin'] = _safe_float(fina_row.get('grossprofit_margin'))
            quarter_info['net_margin'] = _safe_float(fina_row.get('netprofit_margin'))
            quarter_info['debt_to_assets'] = _safe_float(fina_row.get('debt_to_assets'))
            quarter_info['current_ratio'] = _safe_float(fina_row.get('current_ratio'))
            quarter_info['quick_ratio'] = _safe_float(fina_row.get('quick_ratio'))
            quarter_info['ocfps'] = _safe_float(fina_row.get('ocfps'))

        if income_row:
            quarter_info['revenue'] = _safe_float(income_row.get('revenue'))
            quarter_info['operate_profit'] = _safe_float(income_row.get('operate_profit'))
            quarter_info['n_income'] = _safe_float(income_row.get('n_income'))

        quarters.append(quarter_info)

    if not quarters or all(
        not q.get('eps') and not q.get('revenue')
        for q in quarters
    ):
        return {'status': 'empty', 'message': '未获取到财务数据',
                'source': 'Tushare Pro (fina_indicator / income)', 'quarters': []}

    return {
        'status': 'ok',
        'source': 'Tushare Pro (fina_indicator / income)',
        'quarters': quarters,
    }


def _safe_float(val) -> Optional[float]:
    """安全转换为float，None或异常返回None"""
    if val is None:
        return None
    try:
        f = float(val)
        return f
    except (ValueError, TypeError):
        return None


# ════════════════════════════════════════════════════
# 数据采集主函数
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
    
    # 3. 最新评分数据（从backtest_score_daily - P6引擎）
    cur.execute("""
        SELECT trade_date, total_score, trend_score, momentum_score,
               wave_score, volume_score, close_price
        FROM backtest_score_daily WHERE ts_code=%s 
        ORDER BY trade_date DESC LIMIT 1
    """, (ts_code,))
    score = cur.fetchone()
    if score:
        data['score'] = {
            'trade_date': str(score['trade_date']),
            'total_score': float(score['total_score']) if score['total_score'] else 0,
            'trend_score': float(score['trend_score']) if score['trend_score'] else 0,
            'momentum_score': float(score['momentum_score']) if score['momentum_score'] else 0,
            'wave_score': float(score['wave_score']) if score['wave_score'] else 0,
            'volume_score': float(score['volume_score']) if score['volume_score'] else 0,
            'composite_score': float(score['total_score']) if score['total_score'] else 0,
            'confidence': 0,
        }
        data['sources']['score'] = f"backtest_score_daily (P6引擎, 评分日:{score['trade_date']})"
    else:
        # fallback: 从strategy_signal取composite_score
        cur.execute("""
            SELECT trade_date, composite_score FROM strategy_signal
            WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        score2 = cur.fetchone()
        if score2 and score2['composite_score']:
            data['score'] = {
                'trade_date': str(score2['trade_date']),
                'composite_score': float(score2['composite_score']),
                'confidence': 0,
            }
            data['sources']['score'] = f"strategy_signal (P6引擎, 评分日:{score2['trade_date']})"
    
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
    
    # ── 9. Bocha API 获取新闻 ──
    try:
        name_for_news = data.get('name', '')
        news_data = fetch_bocha_news(ts_code, name_for_news, days=7, max_results=8)
        data['news'] = news_data
        if news_data.get('status') == 'ok':
            data['sources']['news'] = f"Bocha API (api.bocha.cn, 搜索最近7天 {name_for_news} 相关新闻)"
        else:
            data['sources']['news'] = f"Bocha API (api.bocha.cn) — {news_data.get('message', '新闻数据暂未获取到')}"
    except Exception as e:
        data['news'] = {
            'status': 'error',
            'message': f'新闻获取异常: {str(e)[:100]}',
            'source': 'Bocha API',
            'news': []
        }
        data['sources']['news'] = 'Bocha API — 新闻数据暂未获取到'
    
    # ── 10. Tushare Pro 获取财务数据 ──
    try:
        fin_data = fetch_financial_data(ts_code)
        data['financial'] = fin_data
        if fin_data.get('status') == 'ok':
            data['sources']['financial'] = "Tushare Pro (fina_indicator / income, 最近4个季度)"
        else:
            data['sources']['financial'] = f"Tushare Pro — {fin_data.get('message', '财务数据暂未获取到')}"
    except Exception as e:
        data['financial'] = {
            'status': 'error',
            'message': f'财务数据获取异常: {str(e)[:100]}',
            'source': 'Tushare Pro',
            'quarters': []
        }
        data['sources']['financial'] = 'Tushare Pro — 财务数据暂未获取到'
    
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
    
    # ── 格式化【8. 近期新闻动态】 ──
    news_section = _format_news_section(data.get('news', {}), data.get('name', 'N/A'))
    
    # ── 格式化【10. 最新财报解读】 ──
    financial_section = _format_financial_section(data.get('financial', {}))
    
    prompt = f"""你是一名专业的证券分析师，我想预测股票【{data.get('name','N/A')}({data.get('ts_code','N/A')})】未来的走势，希望你能根据我给你的数据，帮我完成以下分析。要求数据真实可靠，所有数据都可以溯源，不存在默认值或随机值。

📊 数据来源说明：以下所有数据均来自Tushare Pro、Bocha API和自研评分引擎，每个指标都可溯源。

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

{news_section}

{financial_section}

---
请根据以上真实数据完成以下14项分析，每项用【】标记：

【1. 基本面分析】
分析公司基础信息与行业定位、业务与成长性。

【2. 整体基本面分析】
分析公司的主要业务模式、市场份额和盈利来源，说明核心竞争力。

【3. 评估财务健康状况】
基于以上财务数据进行深入分析：
- 营收和净利润的环比/同比增长趋势
- 毛利率和净利率水平及变化
- 资产负债率和偿债能力
- 每股收益（EPS）和净资产收益率（ROE）
如数据不足请标注"财务数据待补充"。

【4. 技术面与资金动向分析】
分析价格趋势、成交量、主要技术指标（MA/MACD/RSI）和资金流向。

【5. 历史股价走势与波动】
回顾股价走势，分析波动率和主要驱动因素。

【6. 宏观经济及行业环境影响】
分析当前宏观环境和行业政策对公司的可能影响。

【7. 风险评估】
分析市场风险、行业风险、公司特定风险。

【8. 近期新闻动态】
基于以上新闻数据分析近期市场关注点和公司动态。
如数据不足请标注"新闻数据待补充"。

【9. 市场情绪及媒体舆论】
基于评分数据中的情绪评分和新闻情感倾向，分析市场情绪。

【10. 最新财报解读】
基于以上财务数据，分析公司最新一期的财报表现：
- 营收、净利润及其同比变化
- 盈利能力指标（毛利率、净利率、ROE）
- 资产负债表健康度（负债率、流动比率）
- 现金流情况
如数据不足请标注"财务数据待补充"。

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


def _format_news_section(news_data: Dict[str, Any], stock_name: str) -> str:
    """格式化新闻数据部分"""
    if not news_data or news_data.get('status') in ('error', 'empty', None):
        status = news_data.get('status', 'unknown') if news_data else 'unknown'
        msg = news_data.get('message', '新闻数据暂未获取到') if news_data else '新闻数据暂未获取到'
        return f"""【近期新闻动态】
新闻数据暂未获取到。
数据来源：Bocha API
说明：{msg}（状态：{status}）
"""

    news_list = news_data.get('news', [])
    if not news_list:
        return f"""【近期新闻动态】
新闻数据暂未获取到。
数据来源：Bocha API
说明：未搜索到 {stock_name} 相关新闻
"""

    lines = [f"【近期新闻动态】（数据来源：{news_data.get('source', 'Bocha API')}）"]
    for i, n in enumerate(news_list, 1):
        sentiment_emoji = '🟢' if n.get('sentiment') == '正面' else ('🔴' if n.get('sentiment') == '负面' else '⚪')
        lines.append(
            f"  {i}. [{n.get('sentiment', '中性')}]{sentiment_emoji} {n.get('title', '')}\n"
        )
        lines.append(f"     来源：{n.get('source', '未知')} | 时间：{n.get('date', '未知')}\n")

    lines.append(f"数据来源：{news_data.get('source', 'Bocha API (api.bocha.cn)')}")
    return "\n".join(lines)


def _format_financial_section(fin_data: Dict[str, Any]) -> str:
    """格式化财务数据部分"""
    if not fin_data or fin_data.get('status') in ('error', 'empty', None):
        status = fin_data.get('status', 'unknown') if fin_data else 'unknown'
        msg = fin_data.get('message', '财务数据暂未获取到') if fin_data else '财务数据暂未获取到'
        return f"""【最新财报数据】
财务数据暂未获取到。
数据来源：Tushare Pro
说明：{msg}（状态：{status}）
"""

    quarters = fin_data.get('quarters', [])
    if not quarters:
        return f"""【最新财报数据】
财务数据暂未获取到。
数据来源：Tushare Pro
说明：未获取到该股票的基本面财务数据
"""

    lines = [f"【最新财报数据】（数据来源：{fin_data.get('source', 'Tushare Pro')}）"]
    
    for q in quarters:
        label = q.get('label', '未知季度')
        lines.append(f"\n── {label} ──")
        
        eps = q.get('eps')
        if eps is not None:
            lines.append(f"基本每股收益(EPS): {eps:.4f} 元")
        else:
            lines.append("基本每股收益(EPS): 数据暂未获取到")
        
        roe = q.get('roe')
        if roe is not None:
            lines.append(f"净资产收益率(ROE): {roe:.2f}%")
        else:
            lines.append("净资产收益率(ROE): 数据暂未获取到")
        
        gross_margin = q.get('gross_margin')
        if gross_margin is not None:
            lines.append(f"毛利率: {gross_margin:.2f}%")
        else:
            lines.append("毛利率: 数据暂未获取到")
        
        net_margin = q.get('net_margin')
        if net_margin is not None:
            lines.append(f"净利率: {net_margin:.2f}%")
        else:
            lines.append("净利率: 数据暂未获取到")
        
        debt_to_assets = q.get('debt_to_assets')
        if debt_to_assets is not None:
            lines.append(f"资产负债率: {debt_to_assets:.2f}%")
        else:
            lines.append("资产负债率: 数据暂未获取到")
        
        current_ratio = q.get('current_ratio')
        if current_ratio is not None:
            lines.append(f"流动比率: {current_ratio:.2f}")
        else:
            lines.append("流动比率: 数据暂未获取到")
        
        ocfps = q.get('ocfps')
        if ocfps is not None:
            lines.append(f"每股经营现金流: {ocfps:.4f} 元")
        else:
            lines.append("每股经营现金流: 数据暂未获取到")
        
        # 营收和利润数据
        revenue = q.get('revenue')
        if revenue is not None:
            yiyuan = revenue / 1e8
            lines.append(f"营业收入: {yiyuan:.2f} 亿元")
        else:
            lines.append("营业收入: 数据暂未获取到")
        
        operate_profit = q.get('operate_profit')
        if operate_profit is not None:
            lines.append(f"营业利润: {operate_profit / 1e8:.2f} 亿元")
        else:
            lines.append("营业利润: 数据暂未获取到")
        
        n_income = q.get('n_income')
        if n_income is not None:
            lines.append(f"净利润: {n_income / 1e8:.2f} 亿元")
        else:
            lines.append("净利润: 数据暂未获取到")
    
    lines.append(f"\n数据来源：{fin_data.get('source', 'Tushare Pro (fina_indicator / income)')}")
    return "\n".join(lines)


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