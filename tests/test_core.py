import datetime
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import invest_core as core
from tests.fakes import (FakeDiscord, FakeMarket, FakeOpenAI, WEBHOOK, fetchers_for, make_dividends, make_hist,
                         make_info)

NOW = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=core.JST)


def ts(date_str):
    return datetime.datetime.fromisoformat(date_str).replace(tzinfo=core.JST).timestamp()


class TestBasics(unittest.TestCase):
    def test_normalize_ticker(self):
        n = core.normalize_ticker
        self.assertEqual(n("7203"), "7203.T")
        self.assertEqual(n(" 130a "), "130A.T")
        self.assertEqual(n("aapl"), "AAPL")
        self.assertEqual(n("7203.T"), "7203.T")
        self.assertEqual(n("brk-b"), "BRK-B")
        for empty in (None, "", "  ", float("nan"), "nan"):
            self.assertEqual(n(empty), "")

    def test_calc_div_yield(self):
        self.assertAlmostEqual(core.calc_div_yield({"dividendRate": 126, "dividendYield": 2.67}, 4715), 126 / 4715)
        self.assertAlmostEqual(core.calc_div_yield({"dividendYield": 0.31}, None), 0.0031)   # 百分率で返る
        self.assertAlmostEqual(core.calc_div_yield({"dividendYield": 2.67}, 0), 0.0267)
        self.assertIsNone(core.calc_div_yield({}, 100))

    def test_isnum_and_fmt(self):
        self.assertTrue(core.isnum(0) and core.isnum(1.5))
        self.assertFalse(core.isnum(None) or core.isnum(float("nan")) or core.isnum("1") or core.isnum(True))
        self.assertEqual(core.fmt_pct(0.0431), "4.31%")
        self.assertEqual(core.fmt_pct(None), "N/A")

    def test_is_unknown_ticker(self):
        self.assertTrue(core.is_unknown_ticker({}))
        self.assertTrue(core.is_unknown_ticker({"trailingPegRatio": None}))
        self.assertFalse(core.is_unknown_ticker({"shortName": "X"}))
        self.assertFalse(core.is_unknown_ticker({"currentPrice": 10}))


class TestFetchInfo(unittest.TestCase):
    def test_adds_fetched_at_and_price_fallback(self):
        m = FakeMarket().add("ETF1", info={"shortName": "ETF", "regularMarketPrice": 55.5, "currency": "USD"})
        with m.install():
            info = core.fetch_info("ETF1")
        self.assertEqual(info["currentPrice"], 55.5)
        self.assertIn("_fetched_at", info)

    def test_unknown_returns_empty(self):
        with FakeMarket().install():
            self.assertEqual(core.fetch_info("NOPE"), {})

    def test_rate_limit_falls_back_to_fast_info(self):
        m = FakeMarket().add("RL", raises=Exception("429 Too Many Requests"))
        with m.install():
            info = core.fetch_info("RL")
        self.assertTrue(info["_rate_limited"])
        self.assertEqual(info["currentPrice"], 100.0)

    def test_rate_limit_retries_then_falls_back(self):
        m = FakeMarket().add("RL", raises=Exception("Rate limited"))
        with m.install(), mock.patch.object(core.time, "sleep") as slp:
            core.fetch_info("RL", retries=2, wait=1)
        self.assertEqual(m.calls.count("RL"), 3)
        self.assertEqual(slp.call_count, 2)

    def test_other_errors_propagate(self):
        m = FakeMarket().add("ERR", raises=RuntimeError("boom"))
        with m.install(), self.assertRaises(RuntimeError):
            core.fetch_info("ERR")


class TestIndicators(unittest.TestCase):
    def test_technicals_none_when_short(self):
        self.assertIsNone(core.calc_technicals(make_hist(n=40)))
        self.assertIsNone(core.calc_technicals(pd.DataFrame()))
        self.assertIsNone(core.calc_technicals(None))

    def test_technicals_values(self):
        t = core.calc_technicals(make_hist(n=260, start=80, end=100))
        self.assertAlmostEqual(t["last_price"], 100.0)
        self.assertIsNotNone(t["sma200"])
        self.assertLess(t["sma200"], t["sma50"])
        self.assertEqual(t["rsi14"], 100.0)            # 一方的な上昇
        self.assertGreater(t["mo1_perf"], 0)
        self.assertIsNone(core.calc_technicals(make_hist(n=120))["sma200"])   # 200日に満たない

    def test_trend_label(self):
        self.assertIn("上昇トレンド", core.trend_label(110, 105, 100, "円"))
        self.assertIn("下降・安値圏", core.trend_label(90, 95, 100, "USD"))
        self.assertIn("USD", core.trend_label(90, 95, 100, "USD"))
        self.assertIn("もみ合い", core.trend_label(101, 99, 100, "円"))
        self.assertEqual(core.trend_label(100, None, 100, "円"), "トレンド判定不能")

    def test_analyze_dividends_progressive(self):
        d = core.analyze_dividends(make_dividends((2, 3, 4, 5, 5), 2022), "円", this_year=2026)
        self.assertEqual(d["progressive_years"], 4)
        self.assertIn("2026年: 5.0円（暫定）", d["trend_str"])
        self.assertEqual(d["months_str"], "3月")

    def test_analyze_dividends_cut_breaks_streak(self):
        d = core.analyze_dividends(make_dividends((5, 5, 3, 4, 5), 2022), "円", this_year=2026)
        self.assertEqual(d["progressive_years"], 2)          # 3→4→5 のみ
        self.assertEqual(core.analyze_dividends(pd.Series(dtype="float64"), "円")["trend_str"], "データなし")
        self.assertEqual(core.analyze_dividends(None, "円")["progressive_years"], 0)

    def test_rate_metric_boundaries(self):
        r = core.rate_metric
        self.assertEqual(r("pe", 14.9), "good")
        self.assertEqual(r("pe", 25), "neutral")
        self.assertEqual(r("pe", 25.1), "caution")
        self.assertEqual(r("pe", -3), "caution")
        self.assertEqual(r("div_yield", 0.035), "good")
        self.assertEqual(r("div_yield", 0.02), "neutral")
        self.assertEqual(r("div_yield", 0.07), "caution")
        self.assertEqual(r("payout", 0.6), "good")
        self.assertEqual(r("payout", 0.85), "caution")
        self.assertEqual(r("roe", 0.15), "good")
        self.assertEqual(r("roe", 0.05), "caution")
        self.assertEqual(r("de", 49), "good")
        self.assertEqual(r("de", 200), "caution")
        self.assertEqual(r("beta", 0.7), "good")
        self.assertEqual(r("beta", 1.5), "caution")
        self.assertEqual(r("rev_growth", -0.01), "caution")
        self.assertEqual(r("rsi", 25), "good")
        self.assertEqual(r("rsi", 75), "caution")
        self.assertEqual(r("pe", None), "na")
        self.assertEqual(r("pe", float("nan")), "na")

    def test_judge_signals(self):
        s = core.judge_signals(0.05, 0.3, 10, 0.2, 0.5, 4)
        self.assertEqual(len(s), 4)
        self.assertEqual(core.judge_signals(None, None, None, None, None, 0), [])
        self.assertEqual(len(core.judge_signals(0.05, 0.7, 30, 0.1, 1.0, 1)), 0)


class TestCollectAnalysis(unittest.TestCase):
    def setUp(self):
        self.m = FakeMarket().add("FAKE1")

    def test_normal(self):
        a = core.collect_analysis("FAKE1", fetchers_for(self.m))
        self.assertEqual(a["company_name"], "FAKE CO")
        self.assertEqual(a["sector_jp"], "情報技術")
        self.assertAlmostEqual(a["div_yield"], 0.05)
        self.assertEqual(a["rev_str"], "500 億円")
        self.assertIsNotNone(a["tech"])
        self.assertEqual(a["news"][0]["title"], "テストニュース")
        self.assertEqual(a["fetched_at"], "2026-10-24T10:00:00+09:00")
        self.assertEqual(a["notes"], [])

    def test_unknown_raises(self):
        with self.assertRaisesRegex(ValueError, "見つかりませんでした"):
            core.collect_analysis("NOPE", fetchers_for(self.m))

    def test_missing_values_do_not_crash(self):
        m = FakeMarket().add("LOSS", info=make_info(trailingPE=None, totalRevenue=None, dividendRate=None,
                                                    payoutRatio=None, returnOnEquity=None))
        a = core.collect_analysis("LOSS", fetchers_for(m))
        self.assertEqual(a["rev_str"], "N/A")
        self.assertTrue(any("取得できなかった項目" in n and "PER" in n for n in a["notes"]))
        prompt = core.build_report_prompt(a)
        self.assertIn("PER: N/A", prompt)
        self.assertIn("売上高: N/A", prompt)

    def test_rate_limited_note(self):
        m = FakeMarket().add("RL", info=make_info(_rate_limited=True))
        a = core.collect_analysis("RL", fetchers_for(m))
        self.assertTrue(a["rate_limited"])
        self.assertTrue(any("アクセス制限" in n for n in a["notes"]))

    def test_news_failure_text(self):
        a = core.collect_analysis("FAKE1", fetchers_for(self.m, news=([], "timeout")))
        self.assertEqual(a["news_text"], "ニュース取得に失敗しました")
        a = core.collect_analysis("FAKE1", fetchers_for(self.m, news=([], None)))
        self.assertEqual(a["news_text"], "関連ニュースなし")

    def test_prompts_and_report(self):
        a = core.collect_analysis("FAKE1", fetchers_for(self.m))
        web = core.build_report_prompt(a)
        self.assertIn("業界平均との比較評価（最重要）", web)
        self.assertIn("テストニュース", web)
        dc = core.build_report_prompt(a, discord=True)
        self.assertIn("Discord文字数制限2000字以内", dc)
        self.assertIn("次回権利落ち日", dc)
        FakeOpenAI.calls.clear()
        text = core.generate_report(FakeOpenAI(), a)
        self.assertEqual(text, "(fake) AIレポート本文")
        self.assertEqual(FakeOpenAI.calls[-1]["max_tokens"], 2000)
        core.generate_report(FakeOpenAI(), a, discord=True)
        self.assertEqual(FakeOpenAI.calls[-1]["max_tokens"], 1500)
        self.assertIn("基本データ" if False else "FAKE CO", core.basic_summary_text(a))
        self.assertIn("テクニカル", core.meeting_context(a))


class TestDiscord(unittest.TestCase):
    def test_split(self):
        text = "\n\n".join(f"### 銘柄{i}\n" + "あ" * 300 for i in range(15))
        chunks = core.split_for_discord(text)
        self.assertTrue(len(chunks) >= 3 and all(len(c) <= 1900 for c in chunks))
        self.assertEqual("\n\n".join(chunks), text)
        long = "い" * 4500
        self.assertEqual("".join(core.split_for_discord(long)), long)
        self.assertEqual(core.split_for_discord("短い"), ["短い"])

    def test_send(self):
        d = FakeDiscord()
        with d.install():
            n = core.send_discord(WEBHOOK, "\n\n".join("あ" * 900 for _ in range(5)), pause=0)
        self.assertEqual(n, len(d.sent))
        self.assertTrue(n >= 3 and all(len(s) <= 2000 for s in d.sent))


class TestScan(unittest.TestCase):
    def info(self, **kw):
        return make_info(**kw)

    def keys(self, info, hist=None, settings=None):
        r = core.evaluate_alerts(info, hist if hist is not None else make_hist(), settings, now=NOW)
        return [a["key"] for a in r["alerts"]], r

    def test_no_alerts(self):
        keys, r = self.keys(self.info(dividendRate=1.0, payoutRatio=0.3))
        self.assertEqual(keys, [])
        self.assertIn("上昇トレンド", r["trend"])

    def test_each_alert(self):
        keys, _ = self.keys(self.info(fiftyTwoWeekHigh=200.0))
        self.assertIn("drop", keys)
        keys, _ = self.keys(self.info(dividendRate=5.0))
        self.assertIn("yield", keys)
        keys, _ = self.keys(self.info(targetMeanPrice=150.0))
        self.assertIn("target", keys)
        keys, _ = self.keys(self.info(payoutRatio=0.9))
        self.assertIn("payout", keys)
        down = make_hist(start=100, end=60)
        keys, r = self.keys(self.info(currentPrice=60.0, fiftyTwoWeekHigh=60.0, dividendRate=1.0), hist=down)
        self.assertIn("rsi", keys)
        self.assertIn("下降・安値圏", r["trend"])

    def test_event_alerts_window(self):
        keys, _ = self.keys(self.info(earningsTimestamp=ts("2026-11-01"), exDividendDate=ts("2026-10-30")))
        self.assertIn("earnings:2026-11-01", keys)
        self.assertIn("exdiv:2026-10-30", keys)
        keys, _ = self.keys(self.info(earningsTimestamp=ts("2026-12-25"), exDividendDate=ts("2026-10-01")))
        self.assertFalse(any(k.startswith(("earnings", "exdiv")) for k in keys))   # 遠い未来・過去は対象外

    def test_settings_override(self):
        keys, _ = self.keys(self.info(dividendRate=5.0), settings={"yield_pct": 6.0})
        self.assertNotIn("yield", keys)

    def test_currency_in_trend(self):
        _, r = self.keys(self.info(currency="USD"), hist=make_hist(start=100, end=60))
        self.assertIn("USD", r["trend"])
        self.assertNotIn("円", r["trend"])

    def test_scan_tickers_isolates_failures(self):
        m = FakeMarket().add("A", info=self.info(fiftyTwoWeekHigh=200.0)).add("ERR", raises=RuntimeError("boom"))
        f = fetchers_for(m)
        seen = []
        res = core.scan_tickers(["A", "ERR", "NOPE"], {}, f.info, f.history, progress=lambda i, n, r: seen.append((i, n)), pause=0, now=NOW)
        self.assertEqual([r["symbol"] for r in res], ["A", "ERR", "NOPE"])
        self.assertIsNone(res[0]["error"])
        self.assertIn("boom", res[1]["error"])
        self.assertIn("見つかりません", res[2]["error"])
        self.assertEqual(seen, [(0, 3), (1, 3), (2, 3)])

    def test_format_message(self):
        m = FakeMarket().add("A", info=self.info(fiftyTwoWeekHigh=200.0)).add("B", info=self.info(dividendRate=1.0)).add("ERR", raises=RuntimeError("x"))
        f = fetchers_for(m)
        res = core.scan_tickers(["A", "B", "ERR"], {}, f.info, f.history, pause=0, now=NOW)
        msg = core.format_scan_message(res, "### TITLE")
        self.assertIn("### TITLE", msg)
        self.assertEqual(msg.count("### 🏢"), 1)
        self.assertIn("データ取得に失敗: ERR", msg)
        self.assertIsNone(core.format_scan_message(res, "T", only=set()))
        self.assertIsNotNone(core.format_scan_message(res, "T", only={("A", "drop")}))


class TestDedupe(unittest.TestCase):
    def res(self, sym, *keys, error=None):
        return {"symbol": sym, "name": sym, "error": error, "price": 100.0, "cur": "円", "trend": "",
                "alerts": [{"key": k, "text": k} for k in keys]}

    def test_flow(self):
        empty = pd.DataFrame(columns=core.STATE_COLUMNS)
        d1 = datetime.date(2026, 10, 1)
        new, st = core.filter_new_alerts([self.res("A", "drop", "yield")], empty, d1, 7)
        self.assertEqual(new, {("A", "drop"), ("A", "yield")})
        self.assertEqual(len(st), 2)
        # 翌日: 同じアラート → 通知しない（最終確認日だけ更新）
        new, st2 = core.filter_new_alerts([self.res("A", "drop", "yield")], st, datetime.date(2026, 10, 2), 7)
        self.assertEqual(new, set())
        self.assertEqual(set(st2["初回通知日"]), {"2026-10-01"})
        self.assertEqual(set(st2["最終確認日"]), {"2026-10-02"})
        # 7日後: 再通知
        new, st3 = core.filter_new_alerts([self.res("A", "drop", "yield")], st2, datetime.date(2026, 10, 8), 7)
        self.assertEqual(len(new), 2)
        self.assertEqual(set(st3["初回通知日"]), {"2026-10-08"})
        # 条件が解消 → 状態から消え、再発すると新規
        new, st4 = core.filter_new_alerts([self.res("A", "yield")], st3, datetime.date(2026, 10, 9), 7)
        self.assertEqual(new, set())
        self.assertEqual(list(st4["アラート種別"]), ["yield"])
        new, _ = core.filter_new_alerts([self.res("A", "drop", "yield")], st4, datetime.date(2026, 10, 10), 7)
        self.assertEqual(new, {("A", "drop")})

    def test_failed_and_unscanned_tickers_keep_state(self):
        st = pd.DataFrame([{"銘柄コード": "A", "アラート種別": "drop", "初回通知日": "2026-10-01", "最終確認日": "2026-10-01"},
                           {"銘柄コード": "B", "アラート種別": "yield", "初回通知日": "2026-10-01", "最終確認日": "2026-10-01"}])
        # A は取得失敗、B は今回スキャン対象外
        new, st2 = core.filter_new_alerts([self.res("A", error="boom")], st, datetime.date(2026, 10, 2), 7)
        self.assertEqual(new, set())
        self.assertEqual(len(st2), 2)

    def test_history_rows(self):
        res = [self.res("A", "drop", "earnings:2026-11-01")]
        rows = core.history_rows(res, {("A", "drop")}, "app", NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["銘柄コード"], "A")
        self.assertEqual(rows[0]["日時"], "2026-10-24 12:00")
        self.assertEqual(rows[0]["実行元"], "app")


class TestHistoryEvaluation(unittest.TestCase):
    def test_returns(self):
        hist = make_hist(n=300, start=100, end=130, last_date="2026-10-20")
        hdf = pd.DataFrame([{"日時": "2026-08-03 16:00", "銘柄コード": "A", "企業名": "A", "アラート種別": "drop",
                             "内容": "x", "現在値": 100.0, "実行元": "app"},
                            {"日時": "2026-10-19 16:00", "銘柄コード": "A", "企業名": "A", "アラート種別": "earnings:2026-11-01",
                             "内容": "x", "現在値": 125.0, "実行元": "app"},
                            {"日時": "2026-08-03 16:00", "銘柄コード": "B", "企業名": "B", "アラート種別": "drop",
                             "内容": "x", "現在値": "", "実行元": "app"}])
        ev = core.evaluate_alert_history(hdf, lambda s, p: hist if s == "A" else pd.DataFrame())
        self.assertFalse(pd.isna(ev.loc[0, "5日後"]) or pd.isna(ev.loc[0, "20日後"]))
        self.assertTrue(pd.isna(ev.loc[1, "20日後"]))        # まだ20営業日経っていない
        self.assertTrue(pd.isna(ev.loc[2, "5日後"]))         # 価格が無い
        summ = core.summarize_alert_history(ev)
        row = summ[summ["種別"] == "高値から急落"].iloc[0]
        self.assertEqual(row["件数"], 2)
        self.assertEqual(row["20日後 件数"], 1)
        self.assertEqual(row["20日後 上昇率"], 1.0)
        self.assertTrue(core.summarize_alert_history(pd.DataFrame()).empty)


class TestOverview(unittest.TestCase):
    def make(self):
        rows = []
        for sym, info, div in (("HI", make_info(dividendRate=5.0, trailingPE=10, returnOnEquity=0.2, payoutRatio=0.4), make_dividends((1, 2, 3, 4, 5))),
                               ("LO", make_info(dividendRate=1.0, trailingPE=40, returnOnEquity=0.05, payoutRatio=0.9), make_dividends((5, 4, 3, 2, 1))),
                               ("NA", {"shortName": "NA", "currentPrice": 50.0}, None)):
            rows.append(core.overview_row(sym, sym, info, make_hist(), div))
        return pd.DataFrame(rows)

    def test_row_values(self):
        df = self.make().set_index("銘柄コード")
        self.assertAlmostEqual(df.loc["HI", "配当利回り(%)"], 5.0)
        self.assertAlmostEqual(df.loc["HI", "前日比(%)"], (100 / 99 - 1) * 100)
        self.assertEqual(df.loc["HI", "非減配(年)"], 4)
        self.assertTrue(pd.isna(df.loc["NA", "PER"]))

    def test_filters(self):
        df = self.make()
        self.assertEqual(list(core.filter_overview(df, min_yield=4)["銘柄コード"]), ["HI"])
        self.assertEqual(list(core.filter_overview(df, max_pe=20)["銘柄コード"]), ["HI"])
        self.assertEqual(list(core.filter_overview(df, min_roe=10, max_payout=50)["銘柄コード"]), ["HI"])
        self.assertEqual(list(core.filter_overview(df, min_prog=3)["銘柄コード"]), ["HI"])
        self.assertEqual(len(core.filter_overview(df)), 3)          # 条件なしは全件
        self.assertEqual(len(core.filter_overview(df, min_yield=0.5)), 2)   # 値が取れない銘柄は除外

    def test_normalize_prices(self):
        n = core.normalize_prices({"A": make_hist(start=50, end=100), "B": make_hist(start=200, end=100), "C": pd.DataFrame()})
        self.assertEqual(list(n.columns), ["A", "B"])
        self.assertAlmostEqual(n["A"].iloc[0], 100.0)
        self.assertAlmostEqual(n["A"].iloc[-1], 200.0)
        self.assertAlmostEqual(n["B"].iloc[-1], 50.0)


class TestPortfolio(unittest.TestCase):
    def test_calc(self):
        holdings = pd.DataFrame([
            {"銘柄コード": "7203", "企業名": "トヨタ", "株数": "100", "取得単価": "2500", "メモ": ""},
            {"銘柄コード": "AAPL", "企業名": "Apple", "株数": 10, "取得単価": 150, "メモ": ""},
            {"銘柄コード": "ZZZ", "企業名": "価格なし", "株数": 5, "取得単価": 10, "メモ": ""},
            {"銘柄コード": "", "企業名": "空行", "株数": 5, "取得単価": 10, "メモ": ""},
            {"銘柄コード": "BAD", "企業名": "株数なし", "株数": "", "取得単価": 10, "メモ": ""},
        ])
        infos = {"7203.T": {"currentPrice": 3000.0, "currency": "JPY", "dividendRate": 100.0, "shortName": "TOYOTA"},
                 "AAPL": {"currentPrice": 200.0, "currency": "USD", "dividendRate": 1.0},
                 "ZZZ": {}}
        d, t = core.calc_portfolio(holdings, infos, fx_usdjpy=150.0)
        self.assertEqual(len(d), 3)
        toyota = d[d["銘柄コード"] == "7203.T"].iloc[0]
        self.assertEqual(toyota["取得額"], 250000)
        self.assertEqual(toyota["評価額"], 300000)
        self.assertEqual(toyota["損益"], 50000)
        self.assertAlmostEqual(toyota["損益率(%)"], 20.0)
        self.assertEqual(toyota["予想年間配当"], 10000)
        self.assertAlmostEqual(toyota["取得ベース利回り(%)"], 4.0)
        apple = d[d["銘柄コード"] == "AAPL"].iloc[0]
        self.assertEqual(apple["評価額(円換算)"], 2000 * 150)
        none = d[d["銘柄コード"] == "ZZZ"].iloc[0]
        self.assertTrue(pd.isna(none["評価額"]))
        tot = t.set_index("通貨")
        self.assertEqual(tot.loc["JPY", "損益"], 50000)
        self.assertEqual(tot.loc["USD", "評価額"], 2000)
        self.assertTrue(core.calc_portfolio(pd.DataFrame(columns=core.PORTFOLIO_COLUMNS), {})[0].empty)


class TestTenbagger(unittest.TestCase):
    def test_screen(self):
        self.assertEqual(core.screen_tenbagger("A", "a", {})[0], "skip")
        self.assertEqual(core.screen_tenbagger("A", "a", {"marketCap": 1e9, "revenueGrowth": 0.3, "currency": "USD"})[0], "valid")
        st, card = core.screen_tenbagger("A", "a", {"marketCap": 1e9, "revenueGrowth": -0.1, "currency": "USD"})
        self.assertEqual((st, card["reason"]), ("rejected", "マイナス成長"))
        st, card = core.screen_tenbagger("A", "a", {"marketCap": 5e12, "revenueGrowth": 0.1, "currency": "JPY"})
        self.assertEqual((st, card["reason"]), ("rejected", "時価総額が大きすぎる"))
        self.assertEqual(core.screen_tenbagger("A", "a", {"marketCap": 5e9, "currency": "USD"})[1]["growth"], "不明")


class TestParseCandidates(unittest.TestCase):
    def test_parse(self):
        text = "# 宇宙\n9348 ispace\nRKLB, Rocket Lab\n\n＃ 量子\nionq\tIonQ\n7203\n7203 重複\nこれは読めない行\nABCDEFGHIJKLMNOP x\n"
        items, bad, over = core.parse_candidates(text)
        self.assertEqual([(i["theme"], i["symbol"], i["name"]) for i in items],
                         [("宇宙", "9348.T", "ispace"), ("宇宙", "RKLB", "Rocket Lab"), ("量子", "IONQ", "IonQ"), ("量子", "7203.T", "")])
        self.assertEqual(bad, ["これは読めない行", "ABCDEFGHIJKLMNOP x"])
        self.assertEqual(over, 0)

    def test_limit_and_empty(self):
        items, bad, over = core.parse_candidates("\n".join(f"A{i}" for i in range(35)))
        self.assertEqual((len(items), over), (30, 5))
        self.assertEqual(core.parse_candidates(""), ([], [], 0))
        self.assertEqual(core.parse_candidates(None), ([], [], 0))
        self.assertEqual(core.parse_candidates("# テーマだけ")[0], [])

    def test_margin_in_card(self):
        st, card = core.screen_tenbagger("A", "a", {"marketCap": 1e9, "revenueGrowth": 0.3, "currency": "USD", "operatingMargins": -0.25})
        self.assertEqual(card["margin"], "-25.0%（赤字）")
        self.assertEqual(core.screen_tenbagger("A", "a", {"marketCap": 1e9, "currency": "USD"})[1]["margin"], "不明")


if __name__ == "__main__":
    unittest.main()
