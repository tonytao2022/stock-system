"""
评分引擎 v4.0 模块统一入口
=========================
使用:
  from score_engine import ScoreEngineV4  # 主编排类
  from engine import sma, rsi, roc        # 指标计算
  from engine.cycle_scorer import score_cycle_enhanced
"""
from .indicators import sma, rsi, roc, stddev, atr
from .cycle_scorer import score_cycle_enhanced, CycleResult
from .chanlun_scorer import score_chanlun_enhanced, ChanlunResult
from .sentiment_scorer import score_sentiment, SentimentResult
from .block_weights import get_block_weights, apply_block_weights, BLOCK_WEIGHTS
from .stop_loss import calc_stop_loss
from .vmap import vmap_score, classify_signal
from .db_utils import DB_CONFIG, load_kline, get_industry, get_market_context, get_pool_stocks

__version__ = '4.0.0'
