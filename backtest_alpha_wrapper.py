#!/usr/bin/env python3
"""
Alpha因子回测包装器
====================
在bt_m1_score的m1_score基础上，用alpha062/046再做一层计算，
生成临时表用于回测。
"""
import os, sys, time, json, subprocess, numpy as np
import pymysql

MYSQL_PWD = "iXve1rVBXfdA4tL9"
DB = "stock_db_v2"

def fill_blended_scores(a062_w, a046_gate, a046_min):
    """创建或更新bt_blended_score表"""
    t0 = time.time()
    conn = pymysql.connect(host='localhost', user='debian-sys-maint', password='iXve1rVBXfdA4tL9', database='stock_db_v2')
    cur = conn.cursor()
    
    # 建表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bt_blended_score (
            ts_code varchar(16) NOT NULL,
            trade_date date NOT NULL,
            score decimal(6,2) NOT NULL,
            PRIMARY KEY (ts_code, trade_date),
            INDEX idx_date_score (trade_date, score)
        )
    """)
    conn.commit()
    
    # 清空
    cur.execute("TRUNCATE TABLE bt_blended_score")
    conn.commit()
    
    # 从bt_m1_score读，JOIN strategy_signal拿alpha因子
    sql = """
        INSERT INTO bt_blended_score (ts_code, trade_date, score)
        SELECT m.ts_code, m.trade_date,
               ROUND(m.m1_score * (1-%s) + IFNULL(s.alpha062_score, 50) * %s, 1) as blended
        FROM bt_m1_score m
        LEFT JOIN strategy_signal s ON m.ts_code=s.ts_code AND m.trade_date=s.trade_date
    """
    cur.execute(sql, (a062_w, a062_w))
    conn.commit()
    
    # alpha046门控：标记那些分低的删除
    if a046_gate:
        del_sql = """
            DELETE b FROM bt_blended_score b
            LEFT JOIN strategy_signal s ON b.ts_code=s.ts_code AND b.trade_date=s.trade_date
            WHERE s.alpha046_score IS NOT NULL AND s.alpha046_score < %s
        """
        cur.execute(del_sql, (a046_min,))
        conn.commit()
    
    cur.execute("SELECT COUNT(*) FROM bt_blended_score")
    cnt = cur.fetchone()[0]
    cur.close()
    conn.close()
    print(f"  📊 bt_blended_score: {cnt}条, {time.time()-t0:.0f}s")
    return cnt


# ====== 配置列表 ======
configs = [
    ('1 M1季节补仓(基准)', 0.0, False, 0),
    ('2 M1+α062×5%', 0.05, False, 0),
    ('3 M1+α062×10%', 0.10, False, 0),
    ('4 M1+α062×15%', 0.15, False, 0),
    ('5 M1+α062×20%', 0.20, False, 0),
    ('6 M1+α062×10%+α046门控', 0.10, True, 30),
    ('7 M1+α062×5%+α046×5%(当前)', 0.05, False, 0),
]

results = []
for label, w, gate, gmin in configs:
    print(f"\n{'='*70}")
    print(f"  🔄 {label}")
    print(f"{'='*70}")
    
    # 填充混合评分
    fill_blended_scores(w, gate, gmin)
    
    # 用原版backtest_season_74_replenish.py跑——从bt_m1_score改为bt_blended_score
    # 最简单方式：把原脚本的FROM改为bt_blended_score，用临时文件
    with open('/opt/stock-analyzer/backtest_season_74_replenish.py') as f:
        code = f.read()
    
    modified = code.replace(
        "FROM bt_m1_score WHERE m1_score IS NOT NULL",
        "FROM bt_blended_score WHERE score IS NOT NULL"
    ).replace(
        "m1_score as score",
        "score"
    )
    
    # 写入临时脚本并执行
    tmp_script = '/tmp/bt_tmp_run.py'
    with open(tmp_script, 'w') as f:
        f.write(modified)
    
    # 捕获结果
    import subprocess as sp
    proc = sp.run(['python3', tmp_script], capture_output=True, text=True, timeout=600)
    
    # 解析输出
    output = proc.stdout + proc.stderr
    ret = None
    dd = None
    trades = None
    win = None
    pl = None
    pf = None
    repl = None
    hp = None
    
    for line in output.split('\n'):
        l = line.strip()
        if '总收益率' in l:
            try: ret = float(l.split()[-1].replace('%',''))
            except: pass
        if '最大回撤' in l:
            try: dd = float(l.split()[-1].replace('%',''))
            except: pass
        if '交易次数' in l:
            try: trades = int(l.split()[-1].replace('笔',''))
            except: pass
        if '胜率' in l:
            try: win = float(l.split()[-1].replace('%',''))
            except: pass
        if '盈亏比' in l and ':' in l:
            try: pl = float(l.split(':')[-1].strip())
            except: pass
        if '盈利因子' in l:
            try: pf = float(l.split()[-1])
            except: pass
        if '补仓执行' in l:
            try: repl = int(l.split()[2].replace('次',''))
            except: pass
        if '半仓止盈' in l:
            try: hp = int(l.split()[2].replace('次',''))
            except: pass
    
    r = {
        'label': label, 'ret': ret, 'dd': dd, 'trades': trades,
        'win': win, 'pl': pl, 'pf': pf, 'repl': repl, 'hp': hp
    }
    results.append(r)
    print(f"  ✅ {label}: ret={ret}%, dd={dd}%, win={win}%, pl={pl}, pf={pf}")

# 汇总
print(f"\n\n{'='*80}")
print(f"  📋 M1增强版 全量回测对比")
print(f"{'='*80}")
print(f"{'方案':35s} {'收益%':>8s} {'回撤%':>7s} {'胜率':>6s} {'盈亏比':>7s} {'因子':>7s} {'交易':>5s} {'补仓':>4s}")
print('─'*85)
for r in sorted(results, key=lambda x: -(x['ret'] or 0)):
    print(f"{r['label']:35s} {r['ret']:>+8.2f} -{abs(r['dd'] or 0):>5.2f}% {r['win'] or 0:>6.1f}% {r['pl'] or 0:>7.2f} {r['pf'] or 0:>7.2f} {r['trades'] or 0:>5d} {r['repl'] or 0:>4d}")

print(f"\n⏱ 总耗时: {time.time()-t0:.0f}s")
