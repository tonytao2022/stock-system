#!/usr/bin/env python3
import sys, pymysql
conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint', password='iXve1rVBXfdA4tL9', database='stock_db')
cur = conn.cursor()
cur.execute("SELECT trade_date, close FROM daily_kline WHERE ts_code='000300.SH' ORDER BY trade_date DESC LIMIT 120")
rows = cur.fetchall()
if len(rows) < 60:
    regime = 'oscillate'
else:
    closes = [float(r[1]) for r in rows]
    cr = (closes[0] - closes[-1]) / closes[-1] * 100
    sa = sum(closes[:20])/20
    fa = sum(closes[-20:])/20
    slope = (sa - fa) / fa * 100
    regime = 'bull' if cr > 8 and slope > 0 else 'bear' if cr < -5 and slope < 0 else 'oscillate'
    print(f'状态: {regime}, 收益:{cr:+.1f}%, 斜率:{slope:+.1f}%')

params = {'bull': {'buy':30,'stop':5,'c20':20,'c30':30,'l':'牛市'},
          'oscillate':{'buy':33,'stop':5,'c20':20,'c30':30,'l':'震荡'},
          'bear':{'buy':35,'stop':3,'c20':20,'c30':25,'l':'熊市'}}
p = params[regime]
for k, v in p.items():
    cur.execute('INSERT INTO system_config (config_key, config_value, description) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE config_value=%s',
        (f'regime_{k}', str(v), '', str(v)))
conn.commit()
cur.close(); conn.close()
print('写入完成')
