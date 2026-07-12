#!/usr/bin/env python3
"""
V4 全量资金回测 - 从backtest_score_v4读取V4评分
买入线70 / 持仓6只 / 爆量过滤 / 动态仓位 / 止损衰减 / P1延判
"""
import sys, os, json, time, math
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, '/opt/stock-analyzer')
from step_strategy_engine import get_conn, PWD, DB


def safe(v, defv=0.0):
    try:
        f = float(v or 0)
        return f if not (math.isnan(f) or math.isinf(f)) else defv
    except:
        return defv


class PoolBacktestV4:
    def __init__(self):
        self.p = {
            'pool_money': 1_000_000,
            'max_positions': 6,
            'buy_threshold': 70,
            'hold_limit': 30,
            'cool_days': 20,
            'checkpoints': [5, 15, 25, 30],
            'p1': 60,
            'p1_grace_days': 2,
            'p2': 30,
            'p3': 20,
            'sl_time_decay': [(5, 5), (7, 10), (8, 999)],
            'ts_pct': 15,
            'commission_pct': 0.025,
            'stamp_tax_pct': 0.10,
            'slippage_pct': 0.10,
        }

    def run(self):
        self._reset()
        self._load_data()
        ad = sorted(self.all_dates)
        print(f"交易日: {len(ad)}天, V4评分: {sum(len(v) for v in self.scores.values())}条/{len(self.scores)}只")

        t0 = time.time()
        for i, td in enumerate(ad):
            if i % 120 == 0:
                e = time.time() - t0
                print(f"  [{i}/{len(ad)}] 持仓{len(self.holdings)} 现金${self.cash:,.0f} 交易{len(self.trades)} {e:.0f}s", flush=True)
            self._chk(td)
            self._buy(td)
            hv = sum(self.holdings[c]['q'] * self._kp(c, td) for c in list(self.holdings.keys()))
            self.port_val = self.cash + hv
            self.daily_log.append({'d': td, 'c': round(self.cash, 2), 'h': len(self.holdings), 'pv': round(self.port_val, 2)})

        for code in list(self.holdings.keys()):
            self._sell(code, ad[-1], '回测结束')

        return self._sum(ad)

    def _reset(self):
        self.cash = 1_000_000
        self.holdings = {}
        self.cooldowns = {}
        self.trades = []
        self.daily_log = []
        self.port_val = 1_000_000
        self.all_dates = set()
        self.scores = defaultdict(dict)
        self.klines = defaultdict(list)
        self.pool = set()

    def _load_data(self):
        conn = get_conn()
        cur = conn.cursor()

        print("加载V4评分...")
        try:
            cur.execute("""
                SELECT ts_code, trade_date, total_score, track,
                       filtered, filter_reason
                FROM backtest_score_v4
                WHERE total_score IS NOT NULL
                ORDER BY trade_date ASC
            """)
            for r in cur.fetchall():
                self.scores[r[0]][str(r[1])] = {
                    's': safe(r[2]),
                    'track': r[3] or '',
                    'filtered': int(r[4] or 0),
                    'filter_reason': r[5] or ''
                }
                self.all_dates.add(str(r[1]))
        except Exception as e:
            print(f"⚠️ V4表不存在: {e}")
            return

        print("加载K线...")
        cur.execute("""
            SELECT ts_code, trade_date, close, high, low
            FROM daily_kline_qfq
            ORDER BY ts_code, trade_date ASC
        """)
        for r in cur.fetchall():
            self.klines[r[0]].append({'d': str(r[1]), 'c': safe(r[2]), 'h': safe(r[3]), 'l': safe(r[4])})

        print("加载监控池...")
        cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
        self.pool = set(r[0] for r in cur.fetchall())

        cur.close()
        conn.close()

    def _gs(self, code, td):
        ds = self.scores.get(code, {})
        s = ds.get(td)
        if s:
            return s
        for o in range(1, 10):
            s = ds.get((date.fromisoformat(td) - timedelta(days=o)).isoformat())
            if s:
                return s
        return None

    def _fk(self, kl, td):
        for k in kl:
            if k['d'] == td:
                return k
        for k in reversed(kl):
            if k['d'] <= td:
                return k
        return None

    def _fki(self, kl, td):
        for i, k in enumerate(kl):
            if k['d'] == td:
                return i
        for i in range(len(kl) - 1, -1, -1):
            if kl[i]['d'] <= td:
                return i
        return None

    def _kp(self, code, td):
        k = self._fk(self.klines.get(code, []), td)
        return k['c'] if k else 0

    def _tc(self, amount, is_buy=True):
        comm = amount * 0.00025
        stamp = amount * 0.001 if not is_buy else 0
        slip = amount * 0.001
        return comm + stamp + slip

    def _chk(self, td):
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

            # 止损时间衰减
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

            # 移动止盈
            pk_arr = [k['h'] for k in kl[bi:ci + 1]]
            pk = max(pk_arr) if pk_arr else cp
            if pk > cost > 0:
                dd = (cp - pk) / pk * 100
                if dd <= -self.p['ts_pct']:
                    pft = (cp - cost) / cost * 100
                    self._sell(code, td, f'移动止盈回撤{-dd:.1f}%盈利{pft:.1f}%')
                    continue

            # 检视点检查
            sc_data = self._gs(code, td)
            sc = sc_data['s'] if sc_data else None
            for cp_num in self.p['checkpoints']:
                if hd >= cp_num and hd < cp_num + 1:
                    th_map = {5: self.p['p1'], 15: self.p['p2'], 25: self.p['p3'], 30: self.p['p3']}
                    th = th_map[cp_num]
                    if sc is not None and sc < th:
                        if cp_num == 5:
                            if 'p1_grace' not in h:
                                h['p1_grace'] = {'count': 0, 'first_low_score': sc}
                            h['p1_grace']['count'] += 1
                            if sc < 60:
                                self._sell(code, td, f'P1评分退坡({sc:.0f})')
                                break
                        else:
                            self._sell(code, td, f'P{cp_num}评分{sc:.0f}<{th}')
                            break

            if hd >= self.p['hold_limit']:
                self._sell(code, td, f'到期{self.p["hold_limit"]}日')

    def _buy(self, td):
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

            sc_data = self._gs(code, td)
            if not sc_data:
                continue
            sc = sc_data['s']
            if sc is None or sc < self.p['buy_threshold']:
                continue

            ck = self._fk(self.klines.get(code, []), td)
            if not ck:
                continue

            # V4过滤层：被V4引擎标记为过滤的，或爆量的
            if sc_data.get('filtered', 0):
                continue

            cand.append((code, sc, ck['c']))

        if not cand:
            return
        cand.sort(key=lambda x: -x[1])
        tb = cand[:av]
        if not tb:
            return

        ts = sum(c[1] for c in tb)
        ac = self.cash
        total_money = 1_000_000

        for code, sc, pr in tb:
            if ac <= 0:
                break
            w = sc / ts if ts > 0 else 1.0 / len(tb)
            inv = min(ac * w, ac)

            max_pos_pct = 30 if sc >= 80 else 20
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
            self.trades.append({'c': code, 'd': td, 'a': 'BUY', 's': sc, 'p': pr, 'q': q, 'am': ac2, 'tc': round(tc_buy, 2)})

    def _sell(self, code, td, reason):
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
        self.trades.append({'c': code, 'd': td, 'a': 'SELL', 'r': reason, 'p': sp, 'q': h['q'], 'am': gross, 'net': round(net, 2), 'pp': round(pp, 2), 'hd': hd, 'tc': round(tc_sell, 2)})
        del self.holdings[code]

    def _sum(self, ad):
        bt = [t for t in self.trades if t['a'] == 'BUY']
        st = [t for t in self.trades if t['a'] == 'SELL']
        ws = [t for t in st if t.get('pp', 0) > 0]
        ls = [t for t in st if t.get('pp', 0) <= 0]

        sv = 1_000_000
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
        sharpe = 0
        calmar = annual_ret / abs(md) if md != 0 else 0

        exit_reasons = defaultdict(lambda: {'count': 0, 'total_pp': 0.0})
        for t in st:
            r = t.get('r', '未知').split('(')[0]
            exit_reasons[r]['count'] += 1
            exit_reasons[r]['total_pp'] += t.get('pp', 0)

        exit_summary = {}
        for r, d in exit_reasons.items():
            exit_summary[r] = {'count': d['count'], 'avg_return': round(d['total_pp'] / d['count'], 2)}

        hold_segments = {'1-5日': [], '6-10日': [], '11-15日': [], '16-20日': [], '21-30日': [], '31-60日': []}
        for t in st:
            hd = t.get('hd', 0)
            pp = t.get('pp', 0)
            if hd <= 5: hold_segments['1-5日'].append(pp)
            elif hd <= 10: hold_segments['6-10日'].append(pp)
            elif hd <= 15: hold_segments['11-15日'].append(pp)
            elif hd <= 20: hold_segments['16-20日'].append(pp)
            elif hd <= 30: hold_segments['21-30日'].append(pp)
            else: hold_segments['31-60日'].append(pp)

        hold_stats = {}
        for seg, pps in hold_segments.items():
            if pps:
                hold_stats[seg] = {'count': len(pps), 'avg_return': round(sum(pps) / len(pps), 2), 'win_rate': round(sum(1 for p in pps if p > 0) / len(pps) * 100, 1)}
            else:
                hold_stats[seg] = {'count': 0, 'avg_return': 0, 'win_rate': 0}

        return {
            'strategy': 'V4_买入线70_持仓6_爆量过滤_资金因子_止损衰减',
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
    print('='*60)
    print(' V4 全量资金回测')
    print(' 规则: 买入线70 / 持仓6只 / 爆量过滤 / 资金因子 / 止损衰减 / P1延判')
    print('='*60)
    t0 = time.time()
    bt = PoolBacktestV4()
    r = bt.run()
    elapsed = time.time() - t0

    print('\n' + '='*60)
    print(f' 回测结果 (耗时{elapsed:.0f}s)')
    print('='*60)
    print(f"  策略: {r['strategy']}")
    print(f"  周期: {r['period']} ({r['days']}天/{r['years']}年)")
    print(f"  初始: {r['start_cap']:,.0f} → 期末: {r['end_cap']:,.0f}")
    print(f"  总收益: {r['return_pct']}% | 年化: {r['annual_return_pct']}%")
    print(f"  回撤: {r['max_dd_pct']}% | 夏普: {r['sharpe']} | 卡玛: {r['calmar']}")
    print(f"  交易: {r['trades']}笔 | 胜率: {r['win_rate']}% | 盈亏比: {r['profit_factor']}")
    print(f"  均盈: +{r['avg_win_pct']}% | 均亏: -{r['avg_lose_pct']}%")
    print(f"  均持仓: {r['avg_hd']}日")
    print()
    print('--- 持仓时间分段 ---')
    for seg in ['1-5日','6-10日','11-15日','16-20日','21-30日','31-60日']:
        hs = r['hold_stats'].get(seg, {})
        if hs.get('count', 0) > 0:
            print(f"  {seg}: {hs['count']}笔 | 均收益={hs['avg_return']:+.2f}% | 胜率={hs['win_rate']}%")
    print()
    print('--- 退出原因 (前10) ---')
    for reason in sorted(r['exit_reasons'].keys(), key=lambda x: -r['exit_reasons'][x]['count'])[:10]:
        er = r['exit_reasons'][reason]
        print(f"  {reason}: {er['count']}笔 | 均收益={er['avg_return']:+.2f}%")

    with open('/tmp/p6_backtest_v4_final.json', 'w') as f:
        json.dump(r, f, indent=2, ensure_ascii=False)
    print(f'\n结果保存: /tmp/p6_backtest_v4_final.json')
