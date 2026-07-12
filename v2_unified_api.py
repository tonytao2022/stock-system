#!/usr/bin/env python3
"""V2统一API服务 (完整版) — 数据源: stock_db_v2 (P6引擎)"""
import sys, os, json, subprocess, re
sys.path.insert(0, '/opt/stock-analyzer')
from flask import Flask, jsonify, request
import pymysql

_pwd = os.environ.get('MYSQL_PASS', '')
if not _pwd:
    _pwd = re.search(r'password\s*=\s*(\S+)', open('/etc/mysql/debian.cnf').read()).group(1)

def conn():
    return pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=_pwd, database='stock_db_v2', charset='utf8mb4')

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

def ok(d):
    return jsonify({"code": 0, "data": d, "message": "success"})

def err(m):
    return jsonify({"code": -1, "data": None, "message": m})

@app.route('/api/v2/auth/token', methods=['POST'])
def auth():
    d = request.get_json() or {}
    if d.get('username') and d.get('password'):
        return ok({'token': 'v2-token-2026', 'user': d['username'],
                   'display_name': 'V2User', 'role': 'admin'})
    return err('invalid')

@app.route('/api/v2/dashboard')
def dash():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date <= CURDATE()")
    td = str(cu.fetchone()[0])
    
    cu.execute("SELECT season, hengjiyuan_level, raw_score, regime FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
    sr = cu.fetchone()
    mkt = {'season': sr[0], 'hengji': sr[1] or 'weak_heng', 'score': float(sr[2] or 0), 'regime': sr[3] or 'range'} if sr else {}
    
    cu.execute("""SELECT ss.ts_code,sb.name,ss.calibrated_score,ss.composite_score,ss.direction,ss.signal_label FROM strategy_signal ss LEFT JOIN stock_basic sb ON ss.ts_code=sb.ts_code WHERE ss.trade_date=%s AND ss.is_calculable=1 AND ss.gate_triggered=0 ORDER BY ss.calibrated_score DESC LIMIT 5""", (td,))
    t5 = []
    for r in cu.fetchall():
        t5.append({'ts_code': r[0], 'name': r[1] or '', 'score': max(float(r[2] or 0), float(r[3] or 0)), 'direction': r[4] or '', 'signal_label': r[5] or ''})
    
    cu.execute("""SELECT CASE WHEN calibrated_score>=75 THEN 'strong_buy' WHEN calibrated_score>=60 THEN 'buy' WHEN calibrated_score>=40 THEN 'hold' ELSE 'wait' END as st,COUNT(*) FROM strategy_signal WHERE trade_date=%s AND is_calculable=1 AND gate_triggered=0 GROUP BY st""", (td,))
    sd = dict(cu.fetchall())
    
    cu.execute("SELECT ts_code,name,qty,profit_pct,market_value FROM portfolio_holdings WHERE status='HOLDING'")
    hs = [{'ts_code':r[0],'name':r[1] or '','qty':int(r[2] or 0),'profit_pct':float(r[3] or 0),'market_value':float(r[4] or 0)} for r in cu.fetchall()]
    
    cu.close(); c.close()
    return ok({'trade_date':td,'market':mkt,'top5':t5,'signal_distribution':sd,'holdings':hs})

@app.route('/api/v2/strategy/signals')
def signals():
    limit = int(request.args.get('limit', 50))
    c = conn(); cu = c.cursor()
    cu.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date <= CURDATE()")
    td = str(cu.fetchone()[0])
    sql = """SELECT ss.*, sb.name, sb.industry FROM strategy_signal ss LEFT JOIN stock_basic sb ON ss.ts_code=sb.ts_code WHERE ss.trade_date=%s AND ss.is_calculable=1 AND ss.gate_triggered=0 ORDER BY ss.calibrated_score DESC, ss.composite_score DESC"""
    if limit > 0:
        sql += " LIMIT " + str(limit)
    cu.execute(sql, (td,))
    cols = [d[0] for d in cu.description]
    sigs = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok({'trade_date':td,'count':len(sigs),'signals':sigs})

@app.route('/api/v2/strategy/checkpoints')
def checkpoints():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT ph.*, ss.calibrated_score as score FROM portfolio_holdings ph LEFT JOIN strategy_signal ss ON ph.ts_code=ss.ts_code AND ss.trade_date=(SELECT MAX(trade_date) FROM strategy_signal) WHERE ph.status='HOLDING'")
    cols = [d[0] for d in cu.description]
    cps = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok({'count':len(cps),'checkpoints':cps})

@app.route('/api/v2/strategy/config')
def sconfig():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT id,name,season_type,buy_min_score,max_pos_pct,stop_loss_pct,cool_days,trailing_stop_pct,p1_score,p2_score,p3_score FROM strategy_config WHERE is_active=1 ORDER BY id")
    cols = [d[0] for d in cu.description]
    cfg = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok(cfg)

@app.route('/api/v2/holdings')
def holdings():
    c = conn(); cu = c.cursor()
    st = request.args.get('status', '')
    sql = "SELECT ph.*, ss.calibrated_score as score, ss.signal_label FROM portfolio_holdings ph LEFT JOIN strategy_signal ss ON ph.ts_code=ss.ts_code AND ss.trade_date=(SELECT MAX(trade_date) FROM strategy_signal)"
    if st:
        sql += " WHERE ph.status='" + st + "'"
    sql += " ORDER BY ph.created_at DESC"
    cu.execute(sql)
    cols = [d[0] for d in cu.description]
    cu.close(); c.close()
    return ok({'holdings':[dict(zip(cols, r)) for r in cu.fetchall()]})

@app.route('/api/v2/holdings/calc', methods=['GET','POST'])
def calc_h():
    try:
        from trade_manager import sync_to_portfolio
        c = conn(); cu = c.cursor()
        cu.execute("SELECT ts_code FROM portfolio_holdings WHERE status='HOLDING'")
        codes = [r[0] for r in cu.fetchall()]
        cu.close(); c.close()
        for code in codes:
            sync_to_portfolio(code, None)
        return ok({'status':'ok','synced':len(codes)})
    except Exception as e:
        return err(str(e))

@app.route('/api/v2/backtest/pool')
def bpool():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT ts_code,name FROM watch_pool WHERE is_active=1 ORDER BY name")
    stks = [{'ts_code':r[0],'name':r[1] or ''} for r in cu.fetchall()]
    cu.close(); c.close()
    return ok({'stocks':stks,'total':len(stks)})

@app.route('/api/v2/backtest/run', methods=['POST'])
def brun():
    return ok({'status':'ok','message':'回测由定时管道执行'})

@app.route('/api/v2/backtest/history')
def bhist():
    return ok({'runs':[],'total':0})

@app.route('/api/v2/system/health')
def shealth():
    try:
        c = conn(); cu = c.cursor()
        cu.execute("SELECT 1"); cu.close(); c.close()
        db_ok = True
    except:
        db_ok = False
    disk = subprocess.run(['df','-h','/'], capture_output=True, text=True)
    dp = disk.stdout.split('\n')[1].split() if disk.stdout else ['']*6
    return ok({'service':'v2-unified','port':8891,'status':'running','database':'connected' if db_ok else 'disconnected','disk_usage':dp[4] if len(dp)>4 else 'unknown'})

@app.route('/api/v2/system/config')
def sconfig2():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT id, config_key, config_value, description FROM system_config ORDER BY id")
    cols = [d[0] for d in cu.description]
    cfg = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok(cfg)

@app.route('/api/v2/sector/rotation')
def srotation():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT si.ts_code, si.name, si.trade_date, si.pct_change, si.amount FROM sector_index_daily si WHERE si.trade_date=(SELECT MAX(trade_date) FROM sector_index_daily) ORDER BY si.pct_change DESC LIMIT 30")
    cols = [d[0] for d in cu.description]
    sec = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok({'sectors':sec})

@app.route('/api/v2/health')
def health():
    return jsonify({"status":"ok","version":"v2-unified","database":"stock_db_v2"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8891))
    print(f"V2统一API :{port} 数据源: stock_db_v2")
    app.run(host='0.0.0.0', port=port, debug=False)
