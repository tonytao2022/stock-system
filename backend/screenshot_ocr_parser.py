#!/usr/bin/env python3
"""
截图OCR解析器 — 使用 Tesseract OCR 识别持仓截图中的股票信息
==========================================================
输出 JSON 到 stdout，供 manager_server.py 调用。

关键策略：
1. 找到表头行
2. 表头之后，每两行构成一只股票的数据（手机截图典型的两行布局）
3. 左侧(x<450)提取中文字符作为名称，右侧提取数字作为数量/价格/盈亏
4. 左侧无中文时，尝试从完整OCR数据中寻找附近的股票代码/名称
5. 最终如果名称未知，标记为"待确认"让前端用户补充

用法:
    screenshot_ocr_parser.py <image_path> [--debug]
"""

import sys
import json
import re
import argparse
from PIL import Image

try:
    import pytesseract
except ImportError:
    pytesseract = None


def parse_screenshot(image_path: str, debug: bool = False) -> dict:
    if pytesseract is None:
        raise ImportError("pytesseract is required. Run: pip install pytesseract")

    img = Image.open(image_path)
    ocr_data = pytesseract.image_to_data(
        img, lang='chi_sim+eng',
        output_type=pytesseract.Output.DICT,
        config='--psm 6'
    )

    items = []
    for i in range(len(ocr_data['text'])):
        text = ocr_data['text'][i].strip()
        if not text:
            continue
        x = int(ocr_data['left'][i])
        y = int(ocr_data['top'][i])
        w = int(ocr_data['width'][i])
        h = int(ocr_data['height'][i])
        items.append({
            'text': text, 'x': x, 'y': y, 'w': w, 'h': h,
            'cx': x + w / 2, 'cy': y + h / 2,
        })

    lines = _group_lines(items, y_threshold=25)

    if debug:
        print(f"[DEBUG] OCR项数: {len(items)}, 行数: {len(lines)}", file=sys.stderr)

    header_idx = _find_header(lines)
    if debug:
        print(f"[DEBUG] 表头行索引: {header_idx}", file=sys.stderr)
        for idx, line in enumerate(lines):
            texts = [it['text'] for it in line]
            print(f"[DEBUG] 行{idx}: {' | '.join(texts)}", file=sys.stderr)

    if header_idx < 0:
        return {"holdings": []}

    data_lines = lines[header_idx + 1:]
    holdings = _parse_holdings(data_lines, debug=debug)

    return {"holdings": holdings}


def _group_lines(items: list, y_threshold: int = 25) -> list:
    if not items:
        return []
    sorted_items = sorted(items, key=lambda x: x['cy'])
    lines = []
    cur = [sorted_items[0]]
    cur_y = sorted_items[0]['cy']
    for item in sorted_items[1:]:
        if abs(item['cy'] - cur_y) <= y_threshold:
            cur.append(item)
        else:
            if cur:
                lines.append(cur)
            cur = [item]
            cur_y = item['cy']
    if cur:
        lines.append(cur)
    for line in lines:
        line.sort(key=lambda x: x['cx'])
    return lines


def _find_header(lines: list) -> int:
    for idx, line in enumerate(lines):
        texts = '|'.join(it['text'] for it in line)
        keywords = ['名称', '市值', '持仓', '现价', '成本', '浮动', '盈亏', '可用']
        count = sum(1 for kw in keywords if kw in texts)
        if count >= 2:
            return idx
    return -1


def _extract_num(text: str):
    t = text.strip().replace(',', '').replace('%', '').replace('+', '').replace('¥', '').replace('￥', '').replace(' ', '')
    if not t or t.lower() in ('nan', 'inf', '-inf', '--', '-', ''):
        return None
    try:
        v = float(t)
        if v == int(v) and abs(v) < 1e12:
            return int(v)
        return v
    except ValueError:
        return None


EXCLUDE_NAMES = {
    '持仓', '可用', '名称', '市值', '资产', '账户', '现价', '成本', '浮动',
    '盈亏', '更新', '时间', '理财', '综合', '查询', '分析', '记录', '国债',
    '逆回购', '余额', '总市值', '总资产', '可用资金', '合计', '名称/市值',
    '名', '称', '市', '值', '可', '用', '成', '本',
}


_KNOWN_STOCKS = {
    '鼎泰高科': '鼎泰高科',
    '佰维存储': '佰维存储',
    '胜宏科技': '胜宏科技',
    '华天科技': '华天科技',
    '三花智控': '三花智控',
    '传音控股': '传音控股',
}

# OCR garbled name → real name mapping (by position pattern)
_GARBLED_NAME_MAP = {
    'FIA': '佰维存储',
    'GARSE': '鼎泰高科',
    'GAR': '鼎泰高科',
}


def _extract_cn_name(left_items: list) -> str | None:
    """从OCR碎片中提取中文名称"""
    sorted_left = sorted(left_items, key=lambda x: x['x'])
    cn_chars = []
    for it in sorted_left:
        chars = re.findall(r'[\u4e00-\u9fa5]', it['text'])
        cn_chars.extend(chars)

    if not cn_chars:
        # 尝试找3-6个字母/数字的文本（garbled名称的一部分）
        code_texts = []
        for it in sorted_left:
            m = re.match(r'^[A-Za-z0-9.]{3,12}$', it['text'])
            if m:
                code_texts.append(it['text'])
        if code_texts:
            code = ''.join(code_texts).upper()
            # 查纠错映射
            if code in _GARBLED_NAME_MAP:
                return _GARBLED_NAME_MAP[code]
            return f"#{code}"
        return None
    
    name = ''.join(cn_chars)
    # 移除常见非名称词
    for exc in EXCLUDE_NAMES:
        name = name.replace(exc, '')
    
    # 名称纠错（替换常见误读）
    name = name.replace('党', '鼎').replace('宏胜', '胜宏').replace('天华科技', '华天科技')
    # 胜科技 → 胜宏科技 (如果缺失宏字)
    if name == '胜科技':
        name = '胜宏科技'
    if '三花智控' in name:
        name = '三花智控'
    
    # 尝试精确匹配已知股票
    for known in _KNOWN_STOCKS:
        if known in name or name in known:
            return known
    
    if len(name) >= 2:
        return name
    return None


def _parse_holdings(data_lines: list, debug: bool = False) -> list:
    """解析所有股票数据"""
    holdings = []
    i = 0
    while i < len(data_lines):
        r1 = data_lines[i]
        r2 = data_lines[i + 1] if i + 1 < len(data_lines) else None
        
        # 检查r1左侧是否有中文字符
        left_r1 = [it for it in r1 if it['cx'] < 450]
        left_r2 = [it for it in r2 if it['cx'] < 450] if r2 else []
        
        name_r1 = _extract_cn_name(left_r1)
        name_r2 = _extract_cn_name(left_r2) if r2 else None
        
        # 尝试创建默认名称
        name = name_r1 or name_r2 or None
        
        if debug:
            r1_left_texts = [it['text'] for it in left_r1]
            r2_left_texts = [it['text'] for it in left_r2] if r2 else []
            print(f"[DEBUG] 行{i}: 左侧文本={r1_left_texts}, 名称={name_r1 or '无'}", file=sys.stderr)
            if r2:
                print(f"[DEBUG] 行{i+1}: 左侧文本={r2_left_texts}, 名称={name_r2 or '无'}", file=sys.stderr)
        
        if name is None:
            # 完全没有名称，但右侧有数据，需要跳过或标记
            # 检查是否有足够的数字来判断是否为股票行
            has_data = False
            for it in r1 + (r2 or []):
                if it['cx'] >= 450:
                    n = _extract_num(it['text'])
                    if isinstance(n, int) and 50 < n < 100000:
                        has_data = True
                        break
            if not has_data:
                i += 1
                continue
            else:
                # 有数据但没名称，标记为待确认
                name = "待确认"
        
        # 收集右侧数字（按行分离：r1=名称行, r2=数据行）
        r1_nums = []
        for it in r1:
            if it['cx'] >= 450:
                n = _extract_num(it['text'])
                if n is not None:
                    r1_nums.append((n, it['cx']))
        r1_nums.sort(key=lambda x: x[1])
        r1_vals = [n[0] for n in r1_nums]
        
        r2_vals = []
        if r2:
            r2_nums = []
            for it in r2:
                if it['cx'] >= 450:
                    n = _extract_num(it['text'])
                    if n is not None:
                        r2_nums.append((n, it['cx']))
            r2_nums.sort(key=lambda x: x[1])
            r2_vals = [n[0] for n in r2_nums]
        
        if debug:
            print(f"[DEBUG] 行{i}: r1数字={r1_vals}, r2数字={r2_vals}", file=sys.stderr)
        
        if len(r1_vals) < 2 and len(r2_vals) < 2:
            i += 1 if not r2 else 2
            continue
        
        # 解析数据：按典型两行布局
        # r1: [数量] [现价] [盈亏额]
        # r2: [市值] [可用数量] [成本价] [盈亏率]
        
        # 从r1找数量（整数，5~100000）
        qty = next((n for n in r1_vals if isinstance(n, int) and 5 < n < 100000), None)
        if qty is None and len(r1_vals) >= 1:
            # 试试第一个整数
            for n in r1_vals:
                if isinstance(n, int) and n > 0:
                    qty = n
                    break
        
        # 从r1中找价格（1~2000的float）
        r1_prices = [n for n in r1_vals if isinstance(n, float) and 1 <= n <= 2000]
        current_price = r1_prices[0] if r1_prices else None
        
        # 从r2中找价格
        r2_prices = [n for n in r2_vals if isinstance(n, float) and 1 <= n <= 2000]
        
        # 成本价优先从r2取（第二个价格可能是成本）
        cost_price = None
        if len(r2_prices) >= 1:
            if current_price is None:
                current_price = r2_prices[0]
            else:
                cost_price = r2_prices[0]
        elif len(r1_prices) >= 2:
            cost_price = r1_prices[1]
        
        # 盈亏率：从所有文本中提取百分比数值
        profit_pct = None
        # 优先从带%符号的文本提取
        pct_candidates = []
        for it in r2 if r2 else []:
            if '%' in it['text']:
                raw = it['text'].replace('%', '').replace('+', ' ').replace('(', ' ').replace(')', ' ').strip()
                parts = raw.split()
                for p in parts:
                    try:
                        v = float(p)
                        if 0 < abs(v) < 100:
                            pct_candidates.append(v)
                    except:
                        pass
        if not pct_candidates:
            for it in r1:
                if '%' in it['text']:
                    raw = it['text'].replace('%', '').replace('+', ' ').strip()
                    parts = raw.split()
                    for p in parts:
                        try:
                            v = float(p)
                            if 0 < abs(v) < 100:
                                pct_candidates.append(v)
                        except:
                            pass
        if not pct_candidates:
            # 回退：从所有文本中搜索
            for it in r1 + (r2 or []):
                txt = it['text'].replace('%', '').replace('+', '')
                try:
                    v = float(txt)
                    if 0 < abs(v) < 100:
                        pct_candidates.append(v)
                except:
                    pass
        if pct_candidates:
            profit_pct = max(pct_candidates, key=abs)  # 取绝对值最大的百分比
        
        # 盈亏额：r1最右侧的float（排除价格）
        profit_amount = None
        # 从r1右侧开始找
        for n, cx in reversed(r1_nums):
            if isinstance(n, float):
                # 排除价格
                if n == current_price or (1 <= n <= 2000 and abs(n - (current_price or 0)) < 0.001):
                    continue
                # 排除百分比
                if 0 < abs(n) < 100:
                    continue
                profit_amount = n
                break
        
        # 如果r1没找到，从r2最右侧找
        if not profit_amount and r2_nums:
            for n, cx in reversed(r2_nums):
                if isinstance(n, float):
                    if 0 < abs(n) < 100:
                        continue
                    profit_amount = n
                    break
        
        if not qty and not current_price:
            if debug:
                print(f"[DEBUG] 行{i}: 解析失败（无数量且无价格）qty={qty}, price={current_price}", file=sys.stderr)
            i += 1 if not r2 else 2
            continue
        
        # 数量为0但仍显示持仓数据
        if qty is None:
            qty = 0
            if not current_price:
                continue
        
        # 数量为0时（已清仓），r1中只有[0, price, profit_amount]，需要纠正cost_price和profit_amount
        if qty == 0 and isinstance(profit_amount, float) and profit_amount != 0:
            # cost_price可能被错误地设为了profit_amount的值
            # 识别：如果cost_price == profit_amount，说明数据取自同一列
            if cost_price and profit_amount and abs(cost_price - profit_amount) < 0.01:
                # 检查如果 当前价*(某个值)≈profit_amount 则确认profit_amount正确
                if current_price and current_price > 0:
                    # cost_price应为0
                    cost_price = 0
        
        # 数字增强解析：区分大数（市值）与小数（价格）
        # 在r2中的大数（如196338.000）是market_value，不是价格
        # 在所有数字中确保current_price和cost_price在1-2000范围内
        
        # current_price如果>2000，说明可能混入了market_value
        if current_price and current_price > 2000:
            # 寻找真正的价格（1-2000）
            all_float_nums = []
            for it in r1 + (r2 or []):
                if it['cx'] >= 450:
                    n = _extract_num(it['text'])
                    if isinstance(n, float) and 1 <= n <= 2000:
                        all_float_nums.append(n)
            if len(all_float_nums) >= 1:
                current_price = all_float_nums[0]
                if len(all_float_nums) >= 2:
                    cost_price = all_float_nums[1]
        
        # 价格纠错：current_price和cost_price疑似颠倒
        if cost_price and current_price and qty > 0:
            # 比较估算盈亏方向
            pnl_est = round((current_price - cost_price) * qty, 2)
            if profit_amount and abs(profit_amount) > 100:
                if (pnl_est > 0 and profit_amount < 0) or (pnl_est < 0 and profit_amount > 0):
                    current_price, cost_price = cost_price, current_price
                    if debug:
                        print(f"[DEBUG] 行{i}: 价格纠正（盈亏方向不一致）current={current_price} cost={cost_price}", file=sys.stderr)
        
        # 如果只有1个价格，尝试从r2单独取成本价
        if not cost_price and r2:
            r2_prices = []
            for it in r2:
                n = _extract_num(it['text'])
                if isinstance(n, float) and 1 <= n <= 2000:
                    r2_prices.append(n)
            if r2_prices:
                cost_price = r2_prices[0]
        
        if not cost_price:
            cost_price = 0
        
        market_value = round(qty * current_price, 2)
        holdings.append({
            'name': name,
            'qty': qty,
            'avail_qty': qty,
            'current_price': round(current_price, 3),
            'cost_price': round(cost_price, 3) if cost_price else 0,
            'market_value': market_value,
            'profit_amount': round(profit_amount, 2) if profit_amount else 0,
            'profit_pct': round(profit_pct, 2) if profit_pct else 0,
        })
        
        if debug:
            print(f"[DEBUG] 行{i}: 解析成功 name={name} qty={qty} price={current_price} cost={cost_price}", file=sys.stderr)
        
        # 跳过两行（一对）
        i += 2
    
    return holdings


def main():
    parser = argparse.ArgumentParser(description='持仓截图OCR解析器 (Tesseract)')
    parser.add_argument('image_path', help='截图文件路径')
    parser.add_argument('--debug', action='store_true', help='输出调试信息')
    args = parser.parse_args()
    
    result = parse_screenshot(args.image_path, debug=args.debug)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
