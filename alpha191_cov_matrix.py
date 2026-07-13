#!/usr/bin/env python3
"""
Alpha191 因子协方差矩阵 (Phase 0.1) - 修正版
输入: alpha191_ic_20260713_0131.json
输出: alpha191_factor_corr_190.json / alpha191_factor_cov_190.json

方法: 从已计算的日级IC(每个因子每日的截面IC)构建190×190协方差矩阵
IC = Spearman Rank IC(因子值, 未来N日收益)
因子i和因子j在日期t的协方差 = E[IC_i(t) * IC_j(t)] - E[IC_i] * E[IC_j]
"""

import json
import numpy as np
import os
import time

OUT_DIR = '/opt/stock-analyzer'
FACTOR_IC_FILE = os.path.join(OUT_DIR, 'alpha191_ic_20260713_0131.json')

def main():
    print("=" * 60)
    print("Alpha191 因子协方差矩阵 (Phase 0.1)")
    print(f"Started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # 加载IC数据
    print(f"Loading IC data from {FACTOR_IC_FILE}...")
    if not os.path.exists(FACTOR_IC_FILE):
        print(f"[ERROR] IC file not found: {FACTOR_IC_FILE}")
        return
        
    with open(FACTOR_IC_FILE, 'r') as f:
        ic_data = json.load(f)
    
    print(f"  Status: {ic_data.get('status', 'unknown')}")
    
    # 提取IC矩阵: 每个因子/每个周期的IC序列
    # ic_data结构: { "status": "...", "alpha_001": {"ic_5d": [...], "ic_10d": [...], ...}, ... }
    
    factor_names = sorted([k for k in ic_data.keys() if k.startswith('alpha_')])
    print(f"  Found {len(factor_names)} factors")
    
    # 为每个horizon构建矩阵
    horizons = ['ic_1d', 'ic_5d', 'ic_10d', 'ic_20d']
    
    results = {}
    
    for horizon in horizons:
        # 收集每个因子的IC序列
        ic_series = {}
        max_len = 0
        factor_list = []
        
        for fname in factor_names:
            fdata = ic_data.get(fname, {})
            series = fdata.get(horizon, [])
            if len(series) > 10:  # 至少10个交易日才有意义
                ic_series[fname] = np.array(series, dtype=np.float64)
                factor_list.append(fname)
                if len(series) > max_len:
                    max_len = len(series)
        
        n_factors = len(factor_list)
        print(f"\n  {horizon}: {n_factors} factors × {max_len} days")
        
        if n_factors < 10:
            print(f"    [SKIP] Too few factors")
            continue
        
        # 构建对齐矩阵（截断到最短长度）
        min_len = min(len(ic_series[f]) for f in factor_list)
        ic_matrix = np.zeros((n_factors, min_len))
        
        for i, fname in enumerate(factor_list):
            ic_matrix[i] = ic_series[fname][:min_len]
        
        # 计算协方差矩阵 (n_factors × n_factors)
        cov_matrix = np.cov(ic_matrix)
        
        # 计算相关系数矩阵
        std_dev = np.sqrt(np.diag(cov_matrix))
        corr_matrix = cov_matrix / np.outer(std_dev, std_dev)
        corr_matrix = np.nan_to_num(corr_matrix, 0.0)
        
        # 统计
        avg_corr = np.mean(corr_matrix[np.triu_indices(n_factors, k=1)])
        min_corr = np.min(corr_matrix)
        max_corr = np.max(corr_matrix)
        n_high_corr = np.sum(np.abs(corr_matrix) > 0.5) - n_factors  # 剔除对角线
        
        results[horizon] = {
            'n_factors': n_factors,
            'n_days': min_len,
            'avg_correlation': round(float(avg_corr), 4),
            'min_correlation': round(float(min_corr), 4),
            'max_correlation': round(float(max_corr), 4),
            'high_corr_pairs': int(n_high_corr // 2),
            'correlation_matrix': corr_matrix.tolist(),
            'covariance_matrix': cov_matrix.tolist(),
            'factor_names': factor_list
        }
        
        print(f"    Avg corr: {avg_corr:.4f}")
        print(f"    Min corr: {min_corr:.4f}  Max corr: {max_corr:.4f}")
        print(f"    High corr pairs (|r|>0.5): {int(n_high_corr//2)}")
    
    # 保存结果
    out_path = os.path.join(OUT_DIR, 'alpha191_factor_corr_190.json')
    output = {
        'version': '1.0',
        'status': 'completed',
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'alpha191_ic_20260713_0131.json',
        'horizons': results
    }
    
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    out_cov_path = os.path.join(OUT_DIR, 'alpha191_factor_cov_190.json')
    cov_output = output.copy()
    # 协方差矩阵单独存（更大）
    with open(out_cov_path, 'w') as f:
        json.dump(cov_output, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*60}")
    print(f"Done! Saved to:")
    print(f"  {out_path}")
    print(f"  {out_cov_path}")
    print(f"Finished at: {time.strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == '__main__':
    main()
