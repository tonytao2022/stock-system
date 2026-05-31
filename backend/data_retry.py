#!/usr/bin/env python3
"""
数据重试工具模块
===============
铁律: API失败→等15秒重试→最多3次→仍失败置为-1并报警
"""
import time
import logging
from typing import Callable, Any, Tuple, Optional

logger = logging.getLogger('data_retry')

DATA_ERROR_MARKER = -1  # 铁律: 异常数据标记为-1


def retry_with_backoff(
    func: Callable,
    *args,
    max_retries: int = 3,
    base_wait: float = 15.0,
    return_on_fail: Any = None,
    error_marker: Any = -1,
    name: str = "api_call",
    **kwargs
) -> Tuple[Any, bool]:
    """
    带退避重试的函数包装器
    
    Args:
        func: 要调用的函数
        max_retries: 最大重试次数 (默认3)
        base_wait: 基础等待秒数 (默认15)
        return_on_fail: 全部失败后返回的值
        error_marker: 数据异常标记 (-1)
        name: 调用名称（用于日志）
    
    Returns:
        (result, success): 调用结果 + 是否成功
    """
    last_error = None
    
    for attempt in range(max_retries + 1):  # 1次原始 + 3次重试
        try:
            result = func(*args, **kwargs)
            if result is not None:
                return (result, True)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(
                    f"⚠️ {name} 失败 ({attempt+1}/{max_retries+1}), {base_wait}秒后重试: {e}"
                )
                time.sleep(base_wait)
            else:
                logger.error(
                    f"❌ {name} 全部 {max_retries+1} 次尝试失败: {e}"
                )
    
    # 全部失败 → 返回错误标记
    return (error_marker if return_on_fail is None else return_on_fail, False)


def is_data_error(value: Any) -> bool:
    """检查是否为数据异常标记（-1 或 None）"""
    if value is None:
        return True
    if value == -1 or value == DATA_ERROR_MARKER:
        return True
    if isinstance(value, (list, tuple)) and len(value) == 0:
        return True
    return False


def should_skip_calculation(value: Any) -> bool:
    """
    铁律: -1标记的数据不可参与计算
    返回 True 表示该数据应被跳过
    """
    return is_data_error(value)
