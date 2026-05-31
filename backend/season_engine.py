from db_config import get_connection, DB_CONFIG
#!/usr/bin/env python3
"""
恒纪元四季判定引擎 v2.1
=========================
v2.1 升级:
- 新增三态识别: 牛市(bull)/熊市(bear)/震荡(range) — RegimeDetector
- 动态季节阈值: 牛市中混沌区间更大，熊市中秋季更敏感
- 混沌细分: 偏多混沌/偏空混沌/中性混沌
- 评分策略切换: 输出 scoring_strategy (momentum=动量 / reversion=均值回归)

判定层面：大盘指数（沪深300/上证综指/创业板指/深证成指/科创50综合）
判定维度：6维度 >15项指标
输出：season + confidence + regime + scoring_strategy + rule_chain

设计者: May (首席模型设计师)
数据源: Tushare Pro → stock_db.daily_kline
"""

import os
from db_config import db_cursor, get_connection
import sys
import math
import json
import pymysql
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import warnings
warnings.filterwarnings("ignore")


