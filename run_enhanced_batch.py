#!/usr/bin/env python3
"""
M1增强版批量回测运行器
"""
import os, sys, json, time
sys.path.insert(0, '/opt/stock-analyzer')

configs = [
    ('M1季节补仓 (基准)', 0.0, False, 0),
    ('M1+α062×5%', 0.05, False, 0),
    ('M1+α062×10%', 0.10, False, 0),
    ('M1+α062×15%', 0.15, False, 0),
    ('M1+α062×20%', 0.20, False, 0),
]

results = []
for label, w, gate, gmin in configs:
    print(f"\n{'='*70}")
    print(f"  🔄 {label}")
    print(f"{'='*70}")
    
    # 动态修改模块级变量
    import importlib.util
    spec = importlib.util.spec_from_file_location("bt_enhanced", "/opt/stock-analyzer/backtest_season_74_enhanced.py")
    mod = importlib.util.module_from_spec(spec)
    
    # 重写连接的端口（避免代码中hardcode冲突）
    import pymysql
    conn_override = pymysql.connect(host='localhost', user='debian-sys-maint', password='iXve1rVBXfdA4tL9', database='stock_db_v2')
    
    # 直接执行脚本内容，覆盖连接
    exec(open('/opt/stock-analyzer/backtest_season_74_enhanced.py').read().replace(
        "host='127.0.0.1',port=3306", "host='localhost',port=3306"
    ).replace(
        "MYSQL_PWD = 'iXve1rVBXfdA4tL9'", "MYSQL_PWD = 'iXve1rVBXfdA4tL9'"
    ), {'__name__': '__main__', 'A062_WEIGHT_OVERRIDE': w, 'A046_GATE_OVERRIDE': gate, 'A046_MIN_OVERRIDE': gmin,
        'pymysql': pymysql, 'conn_override': conn_override})
    
    # 收集结果（从日志输出）
    # 因为import hack比较复杂，不如直接用subprocess+HACK
    break

print("跳过直接用subprocess方式")
