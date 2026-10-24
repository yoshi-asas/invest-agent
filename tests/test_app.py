"""画面（app.py）を Streamlit の AppTest でヘッドレス実行して確認する。
外部通信はすべて偽物に差し替え（yfinance・OpenAI・Discord・Googleスプレッドシート）。"""
import os
import types
import unittest
from contextlib import ExitStack
from unittest import mock

import openai
import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

import invest_core as core
import invest_store
from tests.fakes import FakeDiscord, FakeMarket, FakeOpenAI, WEBHOOK, make_dividends, make_hist, make_info

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


class AppBase(unittest.TestCase):
    use_store = True

    def setUp(self):
        st.cache_data.clear()
        st.cache_resource.clear()
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.market = FakeMarket()
        self.market.add("FAKE1")
        self.market.add("7203.T", info=make_info(shortName="TOYOTA", currentPrice=3105.0, dividendRate=100.0, fiftyTwoWeekHigh=3200.0,
                                                  trailingPE=8.8), hist=make_hist(start=2500, end=3105))
        self.market.add("AAPL", info=make_info(shortName="Apple Inc.", currentPrice=347.0, currency="USD", dividendRate=1.08,
                                                payoutRatio=0.15, fiftyTwoWeekHigh=350.0, trailingPE=30.0, beta=1.2),
                        hist=make_hist(start=200, end=347))
        self.market.add("FAKEERR", raises=RuntimeError("boom (simulated fetch failure)"))
        self.market.add("JPY=X", info={"regularMarketPrice": 150.0, "shortName": "USD/JPY"}, hist=make_hist(n=10, start=148, end=150))
        stack.enter_context(self.market.install())
        self.discord = FakeDiscord()
        stack.enter_context(self.discord.install())
        stack.enter_context(mock.patch.object(core, "get_jp_company_info", lambda s, n, sec: (n, core.SECTOR_MAP.get(sec, sec or "不明"))))
        stack.enter_context(mock.patch.object(core, "fetch_news", lambda s, n, limit=3: ([{"title": "テストニュース", "link": "https://example.com/1"}], None)))
        FakeOpenAI.calls.clear()
        stack.enter_context(mock.patch.object(openai, "OpenAI", FakeOpenAI))
        # core 内の待機だけを無効にする（time.sleep を全体で潰すと、AppTest 自身の待機ループが空回りして遅くなる）
        stack.enter_context(mock.patch.object(core, "time", types.SimpleNamespace(sleep=lambda s: None)))
        self.backend = invest_store.MemoryBackend()
        self.store = invest_store.SheetStore(self.backend, cache_seconds=0)
        cfg = {"creds": {}, "url": "x"} if self.use_store else None
        stack.enter_context(mock.patch.object(invest_store, "load_gsheets_config", lambda s: cfg))
        stack.enter_context(mock.patch.object(invest_store, "make_store", lambda c: self.store))

    # ---- helpers
    def new_app(self, login=True, api_key=False, webhook=False):
        at = AppTest.from_file(APP, default_timeout=180)
        at.secrets["APP_PASSWORD"] = "pw"
        at.secrets["OPENAI_API_KEY"] = ""          # 手元の secrets.toml に書かれていても、テストでは未設定として扱う
        at.secrets["DISCORD_WEBHOOK_URL"] = ""
        if login:
            at.session_state["password_correct"] = True
        at.run()
        if login:
            if api_key:
                at.sidebar.text_input[0].set_value("sk-fake")
            if webhook:
                at.sidebar.text_input[1].set_value(WEBHOOK)
            if api_key or webhook:
                at.run()
        return at

    def button(self, at, text):
        found = [b for b in at.button if text in b.label]
        self.assertTrue(found, f"ボタンが見つからない: {text}（ある: {[b.label for b in at.button]}）")
        return found[0]

    def number(self, at, text):
        return [n for n in at.number_input if text in n.label][0]

    def analyze(self, at, code):
        [t for t in at.text_input if "深掘り" in t.label][0].set_value(code)
        self.button(at, "個別データを取得して分析").click()
        at.run()
        return at

    def seed_watchlist(self, codes):
        self.store.save_watchlist(pd.DataFrame({"銘柄コード": codes, "企業名": [""] * len(codes), "注目理由": [""] * len(codes)}))


class TestLogin(AppBase):
    def test_refuses_to_start_without_password_setting(self):
        at = AppTest.from_file(APP, default_timeout=60)
        at.secrets["APP_PASSWORD"] = ""        # 手元の secrets.toml に APP_PASSWORD があっても「未設定」を再現する
        at.run()
        self.assertTrue(any("APP_PASSWORD が設定されていない" in e.value for e in at.error))
        self.assertEqual(len(at.tabs), 0)

    def test_default_password_only_when_explicitly_allowed(self):
        with mock.patch.dict(os.environ, {"APP_PASSWORD_ALLOW_DEFAULT": "1"}):
            at = AppTest.from_file(APP, default_timeout=60)
            at.secrets["APP_PASSWORD"] = ""
            at.run()
            self.assertTrue(any("既定のパスワード(0000)" in w.value for w in at.warning))
            at.text_input[0].set_value("0000")
            self.button(at, "ログイン").click()
            at.run()
            self.assertEqual(len(at.tabs), 5)

    def test_correct_password_logs_in(self):
        at = self.new_app(login=False)
        at.text_input[0].set_value("pw")
        self.button(at, "ログイン").click()
        at.run()
        self.assertEqual(len(at.tabs), 5)

    def test_lockout_after_repeated_failures(self):
        at = self.new_app(login=False)
        for i in range(4):
            at.text_input[0].set_value("bad")
            self.button(at, "ログイン").click()
            at.run()
            self.assertTrue(any(f"あと{4 - i}回でロック" in e.value for e in at.error), [e.value for e in at.error])
        at.text_input[0].set_value("bad")
        self.button(at, "ログイン").click()
        at.run()
        self.assertTrue(any("60秒間ロックします" in e.value for e in at.error))
        at.text_input[0].set_value("pw")                      # ロック中は正しいパスワードでも入れない
        self.button(at, "ログイン").click()
        at.run()
        self.assertTrue(any("試行回数が多すぎます" in e.value for e in at.error))
        self.assertEqual(len(at.tabs), 0)


class TestTab1(AppBase):
    def test_full_analysis_signals_discord_and_ai(self):
        at = self.new_app(api_key=True, webhook=True)
        self.analyze(at, "FAKE1")
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertFalse(at.error, [e.value for e in at.error])
        self.assertTrue(any("企業情報: FAKE CO" in s.value for s in at.subheader))
        self.assertTrue(any("シグナルが点灯" in s.value for s in at.success))
        self.assertEqual(len(self.discord.sent), 1)
        self.assertIn("シグナル検出", self.discord.sent[0])
        self.assertTrue(any("(fake) AIレポート本文" in m.value for m in at.markdown))
        labels = [m.label for m in at.metric]
        self.assertIn("配当利回り", labels)
        self.assertIn("RSI (14日)", labels)
        self.assertEqual({m.label: m.value for m in at.metric}["配当利回り"], "5.00%")

    def test_result_survives_other_interactions(self):
        at = self.new_app(api_key=True)
        self.analyze(at, "FAKE1")
        at.sidebar.text_input[1].set_value("https://example.invalid/x")      # サイドバーを触ると再実行される
        at.run()
        self.assertTrue(any("企業情報: FAKE CO" in s.value for s in at.subheader), "結果が消えている")
        self.assertEqual(len(FakeOpenAI.calls), 1, "再実行でAIが再度呼ばれている（課金の無駄）")
        self.assertEqual(len(self.discord.sent), 0)

    def test_unknown_ticker(self):
        at = self.new_app(api_key=True, webhook=True)
        self.analyze(at, "ZZZZ9999")
        self.assertTrue(any("見つかりませんでした" in e.value for e in at.error))
        self.assertEqual((len(FakeOpenAI.calls), len(self.discord.sent)), (0, 0))

    def test_missing_values_do_not_crash(self):
        self.market.add("LOSS", info=make_info(trailingPE=None, totalRevenue=None, dividendRate=None, payoutRatio=None,
                                               returnOnEquity=None, beta=None))
        at = self.new_app(api_key=True, webhook=True)
        self.analyze(at, "LOSS")
        self.assertFalse(at.exception)
        self.assertFalse(at.error, [e.value for e in at.error])
        self.assertTrue(any("取得できなかった項目" in w.value for w in at.warning))
        self.assertIn("PER: N/A", FakeOpenAI.calls[-1]["messages"][-1]["content"])

    def test_rate_limited_note_and_no_api_key_hint(self):
        self.market.add("RL", raises=Exception("429 Too Many Requests"))
        at = self.new_app()
        self.analyze(at, "RL")
        self.assertTrue(any("アクセス制限" in w.value for w in at.warning))
        self.assertTrue(any("APIキーが未入力" in i.value for i in at.info))

    def test_color_labels_and_help(self):
        at = self.new_app()
        self.analyze(at, "FAKE1")
        md = "\n".join(m.value for m in at.markdown)
        self.assertIn(":green[● 良好]", md)
        pe = [m for m in at.metric if m.label.startswith("PER")][0]
        self.assertIn("15倍未満", pe.help)

    def test_compact_view_renders_table(self):
        at = self.new_app()
        at.sidebar.toggle[0].set_value(True)
        at.run()
        self.analyze(at, "FAKE1")
        self.assertFalse(at.exception)
        md = "\n".join(m.value for m in at.markdown)
        self.assertIn("| 指標 | 値 | 目安 |", md)
        self.assertEqual([m for m in at.metric], [])

    def test_save_report_to_sheet(self):
        at = self.new_app(api_key=True)
        self.analyze(at, "FAKE1")
        self.button(at, "このレポートを保存").click()
        at.run()
        reps = self.store.load_reports()
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps.loc[0, "銘柄コード"], "FAKE1")
        self.assertIn("fake", reps.loc[0, "レポート"])
        self.assertTrue(any("保存済み" in s.value for s in at.success))
        self.assertFalse([b for b in at.button if "このレポートを保存" in b.label])

    def test_chart_period_switch_and_5y(self):
        at = self.new_app()
        self.analyze(at, "FAKE1")
        for label in ("3ヶ月", "1年", "5年"):
            at.radio(key="chart_period").set_value(label)
            at.run()
            self.assertFalse(at.exception, f"{label}: {[e.value for e in at.exception]}")


class TestChartSpec(unittest.TestCase):
    """candlestick_chart が有効なVega-Liteの仕様を作れるか（ブラウザなしで検証）"""

    def load(self):
        import ast
        import altair as alt
        with open(APP, encoding="utf-8") as f:
            src = f.read()
        ns = {"alt": alt, "pd": pd}
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and "UP_COLOR" in {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}:
                exec(compile(ast.Module([node], []), "app.py", "exec"), ns)
            if isinstance(node, ast.Assign) and "FIT" in {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}:
                exec(compile(ast.Module([node], []), "app.py", "exec"), ns)
            if isinstance(node, ast.FunctionDef) and node.name == "candlestick_charts":
                exec(compile(ast.Module([node], []), "app.py", "exec"), ns)
        return ns["candlestick_charts"]

    def test_specs(self):
        chart_fn = self.load()
        for n, tail in ((260, 126), (260, None), (60, None), (10, None)):
            charts = chart_fn(make_hist(n=n), tail=tail)
            self.assertEqual(len(charts), 2)                              # 価格 + 出来高
            specs = [c.to_dict() for c in charts]                         # 仕様が不正なら例外になる
            self.assertIn("layer", specs[0])
            for sp in specs:
                self.assertEqual(sp["width"], "container")                # 枠の幅に合わせる
                self.assertEqual(sp["autosize"]["type"], "fit")           # 軸を含めて枠に収める（はみ出し防止）
        self.assertEqual(len(chart_fn(make_hist(n=100, volume=0))), 1)    # 出来高が無ければ価格のみ
        h = make_hist(n=100)
        h.index.name = "Date"                                              # 実データ(yfinance)は index 名が Date
        self.assertEqual(len(chart_fn(h)), 2)


class TestTab2(AppBase):
    CODES = ["7203", "AAPL", "FAKE1", "FAKEERR", "7203.T"]      # 7203 と 7203.T は同じ銘柄

    def scan(self, at, dedupe=True, yield_pct=0.1):
        self.number(at, "高配当").set_value(yield_pct)
        at.checkbox(key="chk_dedupe").set_value(dedupe)
        self.button(at, "登録銘柄を一斉スキャン").click()
        at.run()
        return at

    def test_scan_alerts_dedupe_history_and_state(self):
        self.seed_watchlist(self.CODES)
        at = self.new_app(webhook=True)
        self.scan(at)
        self.assertFalse(at.exception, [e.value for e in at.exception])
        alerts = [e.value for e in at.error if "### 🏢" in e.value]
        heads = [a.splitlines()[0] for a in alerts]
        self.assertEqual(len(alerts), len(set(heads)))
        self.assertEqual(sorted(h.split("(")[-1] for h in heads), ["7203.T)", "AAPL)", "FAKE1)"])
        self.assertTrue(any("**3銘柄**" in s.value for s in at.success))
        self.assertTrue(any("FAKEERR" in w.value and "1銘柄" in w.value for w in at.warning))
        aapl = [a for a in alerts if "(AAPL)" in a][0]
        self.assertIn("USD", aapl)
        self.assertNotIn("円", aapl)
        self.assertEqual(len(self.discord.sent), 1)
        self.assertIn("FAKEERR", self.discord.sent[0])
        hist = self.store.load_history()
        self.assertEqual(len(hist), 3)
        self.assertEqual(set(hist["銘柄コード"]), {"7203.T", "AAPL", "FAKE1"})
        self.assertEqual(set(hist["実行元"]), {"app"})
        state = self.store.load_state()
        self.assertEqual(len(state), 3)

        # 2回目: 同じアラートは通知しない・履歴も増えない
        self.discord.sent.clear()
        at2 = self.new_app(webhook=True)
        self.scan(at2)
        self.assertEqual(self.discord.sent, [])
        self.assertTrue(any("新しく発生したアラートはありません" in i.value for i in at2.info))
        self.assertEqual(len(self.store.load_history()), 3)

        # 再通知オフにすると全件送る
        at3 = self.new_app(webhook=True)
        self.scan(at3, dedupe=False)
        self.assertEqual(len(self.discord.sent), 1)
        self.assertEqual(self.discord.sent[0].count("### 🏢"), 3)

    def test_failed_discord_does_not_mark_as_notified(self):
        self.seed_watchlist(["FAKE1"])
        at = self.new_app()
        at.sidebar.text_input[1].set_value("https://fake-webhook.invalid/x")
        at.run()
        with mock.patch.object(core, "send_discord", side_effect=RuntimeError("network down")):
            self.scan(at)
        self.assertTrue(any("Discord自動通知に失敗" in e.value for e in at.error))
        self.assertEqual(len(self.store.load_state()), 0, "通知に失敗したのに通知済みとして記録された")

    def test_many_alerts_are_split(self):
        for i in range(40):
            self.market.add(f"FAKE{i + 100}")
        self.seed_watchlist([f"FAKE{i + 100}" for i in range(40)])
        at = self.new_app(webhook=True)
        self.scan(at)
        self.assertFalse(at.exception)
        self.assertTrue(len(self.discord.sent) >= 2 and all(len(s) <= 2000 for s in self.discord.sent))
        self.assertEqual("\n\n".join(self.discord.sent).count("### 🏢"), 40)

    def test_add_form(self):
        self.seed_watchlist(["7203.T"])
        at = self.new_app()
        [t for t in at.text_input if "銘柄コード (例" in t.label][0].set_value("7203")
        self.button(at, "リストに追加").click()
        at.run()
        self.assertTrue(any("7203.T は既に登録されています" in w.value for w in at.warning))
        self.assertEqual(len(self.store.load_watchlist()), 1)
        at = self.new_app()
        [t for t in at.text_input if "銘柄コード (例" in t.label][0].set_value("fake1")
        self.button(at, "リストに追加").click()
        at.run()
        wl = self.store.load_watchlist()
        self.assertEqual(list(wl["銘柄コード"]), ["7203.T", "FAKE1"])
        self.assertEqual(wl.loc[1, "企業名"], "FAKE CO")
        self.assertTrue(any("FAKE1 を登録しました" in s.value for s in at.success))     # 再実行後も案内が見える

    def test_settings_are_saved_and_loaded(self):
        self.seed_watchlist(["FAKE1"])
        at = self.new_app()
        self.number(at, "高値下落").set_value(12.0)
        self.number(at, "再通知").set_value(3)
        self.button(at, "この条件を保存").click()
        at.run()
        s = self.store.load_settings()
        self.assertEqual((s["drop_pct"], s["renotify_days"]), (12.0, 3))
        at = self.new_app()
        self.assertEqual(self.number(at, "高値下落").value, 12.0)       # 次回起動時にも反映される

    def test_overview_filter_and_compare(self):
        self.seed_watchlist(["7203", "AAPL", "FAKE1", "FAKEERR"])
        at = self.new_app()
        self.button(at, "最新の状況を取得して一覧表示").click()
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        rows = at.session_state["overview"]
        self.assertEqual([r["銘柄コード"] for r in rows], ["7203.T", "AAPL", "FAKE1", "FAKEERR"])
        self.assertIn("取得失敗", rows[3]["企業名"])
        self.number(at, "配当利回り(%)が以上").set_value(4.0)
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any("1 / 4 銘柄を表示" in c.value for c in at.caption), [c.value for c in at.caption])
        self.assertTrue(len(at.multiselect(key="cmp_pick").value) >= 2)


class TestNoStore(AppBase):
    use_store = False

    def test_messages(self):
        at = self.new_app()
        warns = [w.value for w in at.warning]
        self.assertEqual(sum("Secrets APIキー" in w for w in warns), 3)       # タブ2・3・5（スプレッドシートが必要な画面）
        self.analyze(at, "FAKE1")                                              # 分析自体は使える
        self.assertFalse(at.exception)
        self.assertTrue(any("企業情報" in s.value for s in at.subheader))


class TestPortfolio(AppBase):
    def test_evaluate(self):
        self.store.save_portfolio(pd.DataFrame([
            {"銘柄コード": "7203", "企業名": "トヨタ", "株数": 100, "取得単価": 2500, "メモ": ""},
            {"銘柄コード": "AAPL", "企業名": "Apple", "株数": 10, "取得単価": 150, "メモ": ""}]))
        at = self.new_app()
        self.button(at, "最新の株価で評価を更新").click()
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        m = {x.label: x.value for x in at.metric}
        # 3105*100 + 347*10*150 = 310,500 + 520,500
        self.assertEqual(m["評価額（円換算）"], "831,000 円")
        self.assertTrue(m["損益（円換算）"].startswith("+"))
        self.assertTrue(any("1ドル=150.00円" in c.value for c in at.caption))

    def test_fx_missing_warns(self):
        self.market.data.pop("JPY=X")
        self.store.save_portfolio(pd.DataFrame([{"銘柄コード": "AAPL", "企業名": "Apple", "株数": 10, "取得単価": 150, "メモ": ""}]))
        at = self.new_app()
        self.button(at, "最新の株価で評価を更新").click()
        at.run()
        self.assertTrue(any("為替レートを取得できなかった" in w.value for w in at.warning))
        self.assertFalse(at.exception)


class TestTenbagger(AppBase):
    def test_screening(self):
        self.market.add("SMALL", info=make_info(marketCap=2e9, currency="USD", revenueGrowth=0.4, shortName="Small"))
        self.market.add("BIG", info=make_info(marketCap=5e13, currency="JPY", revenueGrowth=0.1, shortName="Big"))
        self.market.add("NEG", info=make_info(marketCap=1e9, currency="USD", revenueGrowth=-0.2, shortName="Neg"))
        at = self.new_app(api_key=True)
        self.button(at, "AI発掘エージェントを起動").click()
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        ok = [s.value for s in at.success if "小型成長" in s.value]
        self.assertTrue(ok)
        rejected = [e.value for e in at.error]                       # 除外されたカードは赤枠で表示される
        self.assertEqual(sorted(rejected), ["**大企業** (BIG)", "**減収** (NEG)"])
        texts = "\n".join(c.value for c in at.caption)
        self.assertIn("マイナス成長", texts)
        self.assertIn("時価総額が大きすぎる", texts)
        self.assertNotIn("存在しない", "\n".join(s.value for s in at.success) + texts)


class TestTenbaggerPaste(AppBase):
    TEXT = "# 成長テーマ\nSMALL 小型成長\nBIG 大企業\nNEG 減収\nNOPE 存在しない\n# 別テーマ\nFAKE1\n読めない行\n"

    def setUp(self):
        super().setUp()
        self.market.add("SMALL", info=make_info(marketCap=2e9, currency="USD", revenueGrowth=0.4, shortName="Small", operatingMargins=-0.1))
        self.market.add("BIG", info=make_info(marketCap=5e13, currency="JPY", revenueGrowth=0.1, shortName="Big"))
        self.market.add("NEG", info=make_info(marketCap=1e9, currency="USD", revenueGrowth=-0.2, shortName="Neg"))

    def run_paste(self, text, api_key=False):
        at = self.new_app(api_key=api_key)
        at.text_area(key="tb_paste").set_value(text)
        self.button(at, "貼り付けた候補を審査する").click()
        at.run()
        return at

    def test_works_without_api_key(self):
        at = self.run_paste(self.TEXT)
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(FakeOpenAI.calls, [])                              # OpenAIは呼ばれない
        self.assertTrue(any("審査完了: 5銘柄中 2銘柄" in s.value for s in at.success), [s.value for s in at.success])
        ok = sorted(s.value for s in at.success if "(" in s.value and "審査完了" not in s.value)
        self.assertEqual(ok, ["**FAKE CO** (FAKE1)", "**小型成長** (SMALL)"])
        self.assertEqual(sorted(e.value for e in at.error), ["**大企業** (BIG)", "**減収** (NEG)"])
        md = "\n".join(m.value for m in at.markdown)
        self.assertIn("成長テーマ", md)
        self.assertIn("別テーマ", md)
        self.assertIn("営業利益率: -10.0%（赤字）", md)
        caps = "\n".join(c.value for c in at.caption)
        self.assertIn("NOPE（該当する銘柄が見つかりません）", caps)
        self.assertIn("マイナス成長", caps)
        self.assertTrue(any("読み取れなかった行: 読めない行" in w.value for w in at.warning))

    def test_result_survives_rerun(self):
        at = self.run_paste(self.TEXT)
        at.sidebar.text_input[1].set_value("https://example.invalid/x")
        at.run()
        self.assertTrue(any("審査完了" in s.value for s in at.success))

    def test_empty_input(self):
        at = self.run_paste("   \n")
        self.assertTrue(any("読み取れる銘柄がありません" in w.value for w in at.warning))

    def test_over_limit_and_fetch_error(self):
        for i in range(33):
            self.market.add(f"FAKE{i + 100}")
        at = self.run_paste("\n".join(f"FAKE{i + 100}" for i in range(33)) + "\nFAKEERR")
        self.assertTrue(any("上限（30銘柄）を超えた 4 銘柄" in w.value for w in at.warning), [w.value for w in at.warning])
        at = self.run_paste("FAKEERR")
        self.assertTrue(any("FAKEERR（取得エラー: RuntimeError）" in c.value for c in at.caption))


class TestHistoryTab(AppBase):
    def test_history_evaluation_and_report_diff(self):
        rows = [{"日時": "2026-08-03 16:00", "銘柄コード": "FAKE1", "企業名": "FAKE CO", "アラート種別": "drop", "内容": "x", "現在値": 90.0, "実行元": "app"},
                {"日時": "2026-10-01 16:00", "銘柄コード": "7203.T", "企業名": "トヨタ", "アラート種別": "yield", "内容": "y", "現在値": 3000.0, "実行元": "actions"}]
        self.store.append_history(rows)
        import datetime
        self.store.save_report("FAKE1", "FAKE CO", 90.0, "# 評価\n強み: A\nリスク: B", datetime.datetime(2026, 9, 1, 10, 0))
        self.store.save_report("FAKE1", "FAKE CO", 100.0, "# 評価\n強み: A\nリスク: C", datetime.datetime(2026, 10, 1, 10, 0))
        at = self.new_app()
        self.assertFalse(at.exception)
        self.button(at, "アラート後の値動きを評価する").click()
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        ev, summ = at.session_state["hist_eval"]
        self.assertEqual(set(summ["種別"]), {"高値から急落", "高配当化"})
        drop = summ[summ["種別"] == "高値から急落"].iloc[0]
        self.assertEqual(drop["20日後 件数"], 1)
        code = [c for c in at.get("code") if "リスク" in c.value]
        self.assertTrue(code, "レポートの差分が表示されていない")
        self.assertIn("-リスク: B", code[0].value)
        self.assertIn("+リスク: C", code[0].value)


if __name__ == "__main__":
    unittest.main()
