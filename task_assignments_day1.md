# Day 1 任务分配 (2026-07-13 周一)

## 已启动 ✅
- [RUNNING] Phase 0.1: 因子协方差矩阵 (timeout 300s → ~2-3min完成)
  - 负责人: Main
  - 脚本: alpha191_cov_matrix.py
  - 输出: alpha191_factor_corr_190.json / alpha191_factor_cov_190.json

## 待启动

### Main (用户确认后)
- [ ] Phase 0.2: 日级IC时间序列面板 (3h)
  - 从alpha191_ic_20260713_0131.json提取
  - 输出: daily_ic_panel.csv

### MAY (需要调度)
- [ ] Phase 1.1: 筛选流水线 (Step 2-5)
  - 依赖: Phase 0.1/0.3 完成
  - 输出: factor_combo_recommend.json

### Hugo (需要调度) 
- [ ] Phase 0.5: 候选因子批量评分计算
  - 依赖: Phase 1.1 完成
  - 更新: strategy_signal.alpha_xxx 列

## 任务分配决策

| 子任务 | 分配给 | 理由 |
|:------|:------|:-----|
| 0.1 协方差矩阵 | Main | 纯数据分析，JSON处理 |
| 0.2 日级IC面板 | Main | 同上，JSON→CSV |
| 0.3 分季节协方差 | Main | 需要0.1+season_state |
| 0.4 条件IC计算 | Main | 需要0.2 |
| 0.5 批量因子评分 | Hugo | 涉及DB更新，需cd |
| 1.1 筛选流水线 | MAY | 因子组合专业评估 |
| 1.2 情景路由器 | Tony | 系统架构设计 |
| 1.3 权重矩阵 | MAY | 量化优化 |
| 1.4 平滑衰减 | Tony | 边际效应处理 |

## 依赖链
0.1 → 0.3 → 1.1 → 0.5
   ↘ 0.2 → 0.4
         ↘ 1.1

## 实际排期建议
- 上午: 0.1(已跑) → 0.2 → 0.3
- 下午: 1.1(MAY) → 0.4 → 0.5(Hugo)
- 傍晚: 1.2(Tony) → 1.3(MAY) → 1.4(Tony)
