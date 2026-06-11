#!/usr/bin/env python3
"""
P6双轨引擎 V3 最终回测 — 应用所有新规则
规则确认：
  1. 买入线=75, 评分动态仓位：≥80→30%/65-79→20%/<65不买
  2. 最大同时持仓=6只
  3. 初始本金100万
  4. P1退坡加2天延判期
  5. P1门限降到60 + 延判期
  6. 止损时间衰减：1-5日-5% / 6-10日-7% / 11日起-8%
  7. 退出：P3/P4/P5/移动止盈沿用现有规则
"""
import sys, os, json, time, math
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, '/opt/stock-analyzer')
import pymysql
from step_strategy_engine import get_conn, PWD, DB


def safe(v, defv=0.0):
    try:
        f = float(v or 0)
        return f if not (math.isnan(f) or math.isinf(f)) else defv
    except:
        return defv


class PoolBacktestV3:
    """P6双轨引擎 V3 最终资金回测"""

    def __init__(self, params=None):
        self.p = {
            'pool_money': 1_000_000,
            'max_positions': 6,          # 最大同时持仓6只
            'buy_threshold': 75,         # 买入线75
            'hold_limit': 30,
            'cool_days': 20,
            'checkpoints': [5, 15, 25, 30],
            'p1': 60,                    # P1门限降到60
            'p1_grace_days': 2,          # P1延判期2天
            'p2': 30,
            'p3': 20,
            'sl_time_decay': [(5, 5), (7, 10), (8, 999)],  # (跌幅%, 天数上限)
            'ts_pct': 15,
            'commission_pct': 0.025,
            'stamp_tax_pct': 0.10,
            'slippage_pct': 0.10,
        }
        if params:
            self.p.update(params)
        self._reset()
        self._load_data()

    def _reset(self):
        self.cash = self.p['pool_money']
        self.holdings = {}
        self.cooldowns = {}
        self.trades = []
        self.daily_log = []
        self.port_val = self.p['pool_money']

    def _load_data(self):
        conn = get_conn()
        cur = conn.cursor()
        print("加载评分数据...")
        cur.execute("""
            SELECT ts_code, trade_date, total_score, close_price
            FROM backtest_score_daily
            WHERE total_score IS NOT NULL
            ORDER BY trade_date ASC
        """)
        self.scores = defaultdict(dict)
        for r in cur.fetchall():
            self.scores[r[0]][str(r[1])] = {'s': safe(r[2]), 'c': safe(r[3])}

        print("加载K线(前复权)...")
        cur.execute("""
            SELECT ts_code, trade_date, close, high, low
            FROM daily_kline_qfq
            ORDER BY ts_code, trade_date ASC
        """)
        self.klines = defaultdict(list)
        for r in cur.fetchall():
            self.klines[r[0]].append({'d': str(r[1]), 'c': safe(r[2]), 'h': safe(r[3]), 'l': safe(r[4])})

        print("加载监控池...")
        cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
        self.pool = set(r[0] for r in cur.fetchall())
        cur.close()
        conn.close()

        print(f"  评分: {sum(len(v) for v in self.scores.values())}条/{len(self.scores)}只")
        print(f"  K线: {sum(len(v) for v in self.klines.values())}条/{len(self.klines)}只")
        print(f"  池: {len(self.pool)}只")

    def _gs(self, code, td):
        """获取评分，最多往前找9天"""
        ds = self.scores.get(code, {})
        s = ds.get(td)
        if s:
            return s['s']
        for o in range(1, 10):
            s = ds.get((date.fromisoformat(td) - timedelta(days=o)).isoformat())
            if s:
                return s['s']
        return None

    def _fk(self, kl, td):
        """查找某日的K线"""
        for k in kl:
            if k['d'] == td:
                return k
        for k in reversed(kl):
            if k['d'] <= td:
                return k
        return None

    def _fki(self, kl, td):
        """查找某日在K线列表中的索引"""
        for i, k in enumerate(kl):
            if k['d'] == td:
                return i
        for i in range(len(kl) - 1, -1, -1):
            if kl[i]['d'] <= td:
                return i
        return None

    def _kp(self, code, td):
        """获取某日收盘价"""
        k = self._fk(self.klines.get(code, []), td)
        return k['c'] if k else 0

    def _tc(self, amount, is_buy=True):
        """交易成本"""
        comm = amount * self.p['commission_pct'] / 100
        stamp = amount * self.p['stamp_tax_pct'] / 100 if not is_buy else 0
        slippage = amount * self.p['slippage_pct'] / 100
        return comm + stamp + slippage

    def run(self):
        """主循环"""
        self._reset()
        ads = set()
        for kl in self.klines.values():
            for k in kl:
                ads.add(k['d'])
        ad = sorted(ads)
        print(f"\n交易日: {len(ad)}天")

        t0 = time.time()
        for i, td in enumerate(ad):
            if i % 180 == 0:
                e = time.time() - t0
                pct = (i + 1) / len(ad) * 100
                print(f"  {i+1}/{len(ad)}({pct:.0f}%) 持仓{len(self.holdings)} 现金￥{self.cash:,.0f} 交易{len(self.trades)} {e:.0f}s")
            self._chk(td)
            self._buy(td)
            hv = sum(self.holdings[c]['q'] * self._kp(c, td) for c in list(self.holdings.keys()))
            self.port_val = self.cash + hv
            self.daily_log.append({'d': td, 'c': round(self.cash, 2), 'h': len(self.holdings), 'pv': round(self.port_val, 2)})

        for code in list(self.holdings.keys()):
            self._sell(code, ad[-1], '回测结束')

        return self._sum(ad)

    def _chk(self, td):
        """每日持仓检查"""
        for code in list(self.holdings.keys()):
            h = self.holdings[code]
            kl = self.klines.get(code, [])
            if not kl:
                continue
            ck = self._fk(kl, td)
            bi = self._fki(kl, h['bd'])
            ci = self._fki(kl, td)
            if not ck or bi is None or ci is None:
                continue

            hd = ci - bi
            cp = ck['c']
            cost = h['cs']

            # --- 止损时间衰减 ---
            sl_threshold = -8
            for sl_pct, sl_days in self.p['sl_time_decay']:
                if hd <= sl_days:
                    sl_threshold = -sl_pct
                    break

            if cost > 0:
                lp = (cp - cost) / cost * 100
                if lp <= sl_threshold:
                    self._sell(code, td, f'止损{lp:.1f}%(衰减{hd}d/{sl_threshold}%)')
                    continue

            # --- 移动止盈 ---
            pk_arr = [k['h'] for k in kl[bi:ci + 1]]
            pk = max(pk_arr) if pk_arr else cp
            has_profit = (pk > cost > 0)
            if has_profit:
                dd = (cp - pk) / pk * 100
                if dd <= -self.p['ts_pct']:
                    pft = (cp - cost) / cost * 100
                    self._sell(code, td, f'移动止盈回撤{-dd:.1f}%盈利{pft:.1f}%')
                    continue

            # --- 检视点检查（P1/P2/P3）---
            sc = self._gs(code, td)
            for cp_num in self.p['checkpoints']:
                if hd >= cp_num and hd < cp_num + 1:
                    th_map = {5: self.p['p1'], 15: self.p['p2'], 25: self.p['p3'], 30: self.p['p3']}
                    th = th_map[cp_num]
                    if sc is not None and sc < th:
                        if cp_num == 5:
                            # P1退坡加延判期
                            if 'p1_grace' not in h:
                                h['p1_grace'] = {'count': 0, 'first_low_score': sc}
                            h['p1_grace']['count'] += 1
                            if sc < 60:
                                # 门限降到60以下才触发卖出
                                self._sell(code, td, f'P1评分退坡({sc:.0f})')
                                break
                            # else 评分在60-74之间但低于买入线75，继续持有观察
                        else:
                            self._sell(code, td, f'P{cp_num}评分{sc:.0f}<{th}')
                            break

            # --- 到期平仓 ---
            if hd >= self.p['hold_limit']:
                self._sell(code, td, f'到期{self.p["hold_limit"]}日')

    def _buy(self, td):
        """每日买入逻辑"""
        av = self.p['max_positions'] - len(self.holdings)
        if av <= 0:
            return

        cand = []
        for code in self.pool:
            if code in self.holdings:
                continue
            if code in self.cooldowns:
                ds = (date.fromisoformat(td) - date.fromisoformat(str(self.cooldowns[code]))).days
                if ds < self.p['cool_days']:
                    continue

            sc = self._gs(code, td)
            if sc is None:
                continue
            ck = self._fk(self.klines.get(code, []), td)
            if not ck:
                continue

            # 买入线75
            if sc < self.p['buy_threshold']:
                continue

            cand.append((code, sc, ck['c']))

        if not cand:
            return
        cand.sort(key=lambda x: -x[1])

        # 取候选（最多av只，不超过6只上限）
        tb = cand[:av]
        if not tb:
            return

        ts = sum(c[1] for c in tb)
        ac = self.cash
        total_money = self.p['pool_money']

        for code, sc, pr in tb:
            if ac <= 0:
                break
            w = sc / ts if ts > 0 else 1.0 / len(tb)
            inv = min(ac * w, ac)

            # 评分动态仓位：≥80→30%，65-79→20%，<65不买（买入线已过滤<75）
            if sc >= 80:
                max_pos_pct = 30
            else:
                max_pos_pct = 20
            max_inv = total_money * max_pos_pct / 100
            inv = min(inv, max_inv)

            if pr <= 0:
                continue
            q = int(inv / (pr * 100)) * 100
            if q <= 0:
                q = 100
            ac2 = q * pr
            if ac2 > ac:
                q = int(ac / (pr * 100)) * 100
                ac2 = q * pr if q > 0 else 0
            if q <= 0 or ac2 <= 0:
                continue
            if ac2 > max_inv:
                q = int(max_inv / (pr * 100)) * 100
                if q <= 0:
                    continue
                ac2 = q * pr

            tc_buy = self._tc(ac2, is_buy=True)
            if ac2 + tc_buy > ac:
                continue

            self.holdings[code] = {'bd': td, 'bp': pr, 'cs': pr, 'q': q, 'iv': ac2}
            self.cash -= (ac2 + tc_buy)
            ac -= (ac2 + tc_buy)
            self.cooldowns[code] = td
            ts -= sc
            self.trades.append({
                'c': code, 'd': td, 'a': 'BUY', 's': sc, 'p': pr, 'q': q,
                'am': ac2, 'tc': round(tc_buy, 2)
            })

    def _sell(self, code, td, reason):
        """卖出"""
        if code not in self.holdings:
            return
        h = self.holdings[code]
        ck = self._fk(self.klines.get(code, []), td)
        sp = ck['c'] if ck else h['bp']
        gross = sp * h['q']
        tc_sell = self._tc(gross, is_buy=False)
        net = gross - tc_sell
        pp = (net - h['iv']) / h['iv'] * 100 if h['iv'] > 0 else 0
        hd = (date.fromisoformat(td) - date.fromisoformat(str(h['bd']))).days

        self.cash += net
        self.trades.append({
            'c': code, 'd': td, 'a': 'SELL', 'r': reason,
            'p': sp, 'q': h['q'], 'am': gross, 'net': round(net, 2),
            'pp': round(pp, 2), 'hd': hd, 'tc': round(tc_sell, 2)
        })
        del self.holdings[code]

    def _sum(self, ad):
        """汇总统计"""
        bt = [t for t in self.trades if t['a'] == 'BUY']
        st = [t for t in self.trades if t['a'] == 'SELL']
        ws = [t for t in st if t.get('pp', 0) > 0]
        ls = [t for t in st if t.get('pp', 0) <= 0]

        sv = self.p['pool_money']
        ev = self.port_val
        tr = (ev - sv) / sv * 100
        pv = sv
        md = 0
        for l in self.daily_log:
            if l['pv'] > pv:
                pv = l['pv']
            dd = (l['pv'] - pv) / pv * 100
            if dd < md:
                md = dd

        avg_hd = sum(t.get('hd', 0) for t in st) / len(st) if st else 0
        avg_w = sum(t.get('pp', 0) for t in ws) / len(ws) if ws else 0
        avg_l = sum(t.get('pp', 0) for t in ls) / len(ls) if ls else 0
        pf = abs(sum(t.get('pp', 0) for t in ws) / max(sum(abs(t.get('pp', 0)) for t in ls), 1)) if ls else 0

        years = len(ad) / 252
        annual_ret = ((1 + tr / 100) ** (1 / years) - 1) * 100 if years > 0 else 0

        daily_returns = [self.daily_log[i]['pv'] / self.daily_log[i - 1]['pv'] - 1 for i in range(1, len(self.daily_log))]
        avg_daily = sum(daily_returns) / len(daily_returns) if daily_returns else 0
        var_daily = sum((r - avg_daily) ** 2 for r in daily_returns) / len(daily_returns) if daily_returns else 0
        std_daily = math.sqrt(var_daily) if var_daily > 0 else 1
        rf_daily = 0.025 / 252
        sharpe = (avg_daily - rf_daily) / std_daily * math.sqrt(252) if std_daily > 0 else 0
        calmar = annual_ret / abs(md) if md != 0 else 0

        # 退出原因统计
        exit_reasons = defaultdict(lambda: {'count': 0, 'total_pp': 0.0})
        for t in st:
            r = t.get('r', '未知')
            base_r = r.split('(')[0]  # 取退出的主类别
            exit_reasons[base_r]['count'] += 1
            exit_reasons[base_r]['total_pp'] += t.get('pp', 0)

        exit_summary = {}
        for r, d in exit_reasons.items():
            exit_summary[r] = {
                'count': d['count'],
                'avg_return': round(d['total_pp'] / d['count'], 2)
            }

        # 持仓时间分段
        hold_segments = {'1-5日': [], '6-10日': [], '11-15日': [], '16-20日': [], '21-30日': [], '31-60日': []}
        for t in st:
            hd = t.get('hd', 0)
            pp = t.get('pp', 0)
            if hd <= 5:
                hold_segments['1-5日'].append(pp)
            elif hd <= 10:
                hold_segments['6-10日'].append(pp)
            elif hd <= 15:
                hold_segments['11-15日'].append(pp)
            elif hd <= 20:
                hold_segments['16-20日'].append(pp)
            elif hd <= 30:
                hold_segments['21-30日'].append(pp)
            else:
                hold_segments['31-60日'].append(pp)

        hold_stats = {}
        for seg, pps in hold_segments.items():
            if pps:
                hold_stats[seg] = {
                    'count': len(pps),
                    'avg_return': round(sum(pps) / len(pps), 2),
                    'win_rate': round(sum(1 for p in pps if p > 0) / len(pps) * 100, 1)
                }
            else:
                hold_stats[seg] = {'count': 0, 'avg_return': 0, 'win_rate': 0}

        return {
            'strategy': f'V3_买入线{self.p["buy_threshold"]}_最多{self.p["max_positions"]}只_100万_P1门限{self.p["p1"]}_延判{self.p["p1_grace_days"]}天_止损衰减',
            'params': {k: v for k, v in self.p.items()},
            'period': f'{ad[0]}~{ad[-1]}',
            'days': len(ad),
            'years': round(years, 2),
            'start_cap': sv,
            'end_cap': round(ev, 2),
            'return_pct': round(tr, 2),
            'annual_return_pct': round(annual_ret, 2),
            'max_dd_pct': round(md, 2),
            'trades': len(self.trades),
            'buy': len(bt),
            'sell': len(st),
            'win': len(ws),
            'lose': len(ls),
            'win_rate': round(len(ws) / len(st) * 100, 2) if st else 0,
            'avg_hd': round(avg_hd, 1),
            'avg_win_pct': round(avg_w, 2),
            'avg_lose_pct': round(avg_l, 2),
            'profit_factor': round(pf, 2),
            'sharpe': round(sharpe, 2),
            'calmar': round(calmar, 2),
            'total_tc': round(sum(t.get('tc', 0) for t in self.trades), 2),
            'hold_stats': hold_stats,
            'exit_reasons': exit_summary,
        }


if __name__ == '__main__':
    print('=' * 70)
    print(' P6双轨 V3 最终回测')
    print(' 规则确认:')
    print('   买入线=75 | 动态仓位≥80→30%/65-79→20%/<65不买')
    print('   最大持仓=6只 | 初始本金=100万')
    print('   P1门限=60 | P1延判=2天')
    print('   止损时间衰减: 1-5日-5% / 6-10日-7% / 11日起-8%')
    print('=' * 70)

    t0 = time.time()
    bt = PoolBacktestV3()
    r = bt.run()
    elapsed = time.time() - t0

    print('\n' + '=' * 70)
    print(' 回测结果')
    print('=' * 70)
    print(f"  策略: {r['strategy']}")
    print(f"  周期: {r['period']} ({r['days']}个交易日, {r['years']}年)")
    print(f"  初始资金: {r['start_cap']:,.0f}")
    print(f"  期末资金: {r['end_cap']:,.0f}")
    print(f"  总收益率: {r['return_pct']}%")
    print(f"  年化收益: {r['annual_return_pct']}%")
    print(f"  最大回撤: {r['max_dd_pct']}%")
    print(f"  总交易: {r['trades']}笔 (买入{r['buy']}笔 / 卖出{r['sell']}笔)")
    print(f"  胜率: {r['win_rate']}%")
    print(f"  盈亏比: {r['profit_factor']}")
    print(f"  平均盈利: +{r['avg_win_pct']}%")
    print(f"  平均亏损: -{r['avg_lose_pct']}%")
    print(f"  平均持仓: {r['avg_hd']}日")
    print(f"  夏普比: {r['sharpe']}")
    print(f"  卡玛比: {r['calmar']}")
    print(f"  总交易成本: ￥{r['total_tc']:,.2f}")

    print('\n  --- 持仓时间分段 ---')
    for seg in ['1-5日', '6-10日', '11-15日', '16-20日', '21-30日', '31-60日']:
        hs = r['hold_stats'].get(seg, {})
        print(f"  {seg}: {hs.get('count', 0)}笔 | 均收益={hs.get('avg_return', 0):+.2f}% | 胜率={hs.get('win_rate', 0)}%")

    print('\n  --- 退出原因 ---')
    for reason in sorted(r['exit_reasons'].keys()):
        er = r['exit_reasons'][reason]
        print(f"  {reason}: {er['count']}笔 | 均收益={er['avg_return']:+.2f}%")

    result_path = '/tmp/p6_backtest_v3_final.json'
    with open(result_path, 'w') as f:
        json.dump(r, f, indent=2, ensure_ascii=False)
    print(f'\n结果已保存: {result_path}')
    print(f'耗时: {elapsed:.0f}s ({elapsed/3600:.1f}h)')
