"""定期スキャン（auto_scan.py）のテスト。ローカル実行とGitHub Actions実行の両方の挙動を確認する"""
import datetime
import unittest
from contextlib import ExitStack
from unittest import mock

import pandas as pd

import auto_scan
import invest_core as core
import invest_store
from tests.fakes import FakeDiscord, FakeMarket, WEBHOOK, make_hist, make_info

NOW = datetime.datetime(2026, 10, 24, 16, 30, tzinfo=core.JST)
ACTIONS_ENV = {"GITHUB_ACTIONS": "true", "DISCORD_WEBHOOK_URL": WEBHOOK}
LOCAL_ENV = {"DISCORD_WEBHOOK_URL": WEBHOOK}


class ScanBase(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.market = FakeMarket()
        self.market.add("7203.T", info=make_info(shortName="TOYOTA", dividendRate=100.0, currentPrice=3105.0, fiftyTwoWeekHigh=3200.0))
        self.market.add("AAPL", info=make_info(shortName="Apple", currency="USD", dividendRate=0.5, currentPrice=347.0, fiftyTwoWeekHigh=350.0))
        self.market.add("FAKE1")
        self.market.add("ERR", raises=RuntimeError("secret-internal-detail"))
        stack.enter_context(self.market.install())
        self.discord = FakeDiscord()
        stack.enter_context(self.discord.install())
        stack.enter_context(mock.patch.object(core, "get_jp_company_info", lambda s, n, sec: (f"JP:{n}", sec)))
        stack.enter_context(mock.patch.object(core, "time", mock.Mock(sleep=lambda s: None)))
        self.store = invest_store.SheetStore(invest_store.MemoryBackend(), cache_seconds=0)
        self.cfg = {"creds": {}, "url": "x"}
        stack.enter_context(mock.patch.object(invest_store, "load_gsheets_config", lambda s: self.cfg))
        stack.enter_context(mock.patch.object(invest_store, "make_store", lambda c: self.store))
        self.lines = []

    def run_scan(self, env, **kw):
        return auto_scan.run_auto_scan(env=env, now=NOW, out=self.lines.append, pause=0, **kw)

    def seed(self, codes, names=None):
        self.store.save_watchlist(pd.DataFrame({"銘柄コード": codes, "企業名": names or [""] * len(codes), "注目理由": [""] * len(codes)}))
        self.store.save_settings({**core.SCAN_DEFAULTS, "yield_pct": 0.1})      # ほぼ全銘柄がアラートになる条件

    def text(self):
        return "\n".join(self.lines)


class TestWithSheet(ScanBase):
    def test_new_alerts_notified_once_and_recorded(self):
        self.seed(["7203", "AAPL", "FAKE1", "ERR"], ["トヨタ", "", "", ""])
        self.assertEqual(self.run_scan(LOCAL_ENV), 0)
        self.assertEqual(len(self.discord.sent), 1)
        msg = self.discord.sent[0]
        self.assertEqual(msg.count("### 🏢"), 3)
        self.assertIn("トヨタ (7203.T)", msg)             # シートの企業名を優先
        self.assertIn("JP:Apple (AAPL)", msg)             # 無ければ取得した名前
        self.assertIn("データ取得に失敗: ERR", msg)
        hist = self.store.load_history()
        self.assertEqual(len(hist), 3)
        self.assertEqual(set(hist["実行元"]), {"local"})
        self.assertEqual(len(self.store.load_state()), 3)
        # 2回目: 通知済みなので送らない・履歴も増えない
        self.discord.sent.clear()
        self.run_scan(LOCAL_ENV)
        self.assertEqual(self.discord.sent, [])
        self.assertEqual(len(self.store.load_history()), 3)
        self.assertIn("通知済みのため、送信しません", self.text())

    def test_condition_cleared_then_fires_again(self):
        self.seed(["FAKE1"])
        self.run_scan(LOCAL_ENV)
        self.assertEqual(len(self.discord.sent), 1)
        self.store.save_settings({**core.SCAN_DEFAULTS, "yield_pct": 50.0})        # 条件を厳しくして解消
        self.run_scan(LOCAL_ENV)
        self.assertEqual(len(self.store.load_state()), 0)
        self.store.save_settings({**core.SCAN_DEFAULTS, "yield_pct": 0.1})         # 再び発生
        self.discord.sent.clear()
        self.run_scan(LOCAL_ENV)
        self.assertEqual(len(self.discord.sent), 1)

    def test_settings_come_from_the_sheet(self):
        self.seed(["FAKE1"])
        self.store.save_settings({**core.SCAN_DEFAULTS, "yield_pct": 99.0})
        self.run_scan(LOCAL_ENV)
        self.assertEqual(self.discord.sent, [])

    def test_failed_send_is_not_recorded(self):
        self.seed(["FAKE1"])
        with mock.patch.object(core, "send_discord", side_effect=RuntimeError("down")):
            self.assertEqual(self.run_scan(LOCAL_ENV), 1)
        self.assertEqual(len(self.store.load_state()), 0)
        self.assertEqual(len(self.store.load_history()), 0)

    def test_without_webhook_prints_and_does_not_mark_notified(self):
        self.seed(["FAKE1"])
        self.assertEqual(self.run_scan({}), 0)
        self.assertEqual(self.discord.sent, [])
        self.assertIn("送信される予定だったメッセージ", self.text())
        self.assertEqual(len(self.store.load_state()), 0)

    def test_all_failed_is_an_error(self):
        self.seed(["ERR"])
        self.assertEqual(self.run_scan(LOCAL_ENV), 1)
        self.assertIn("すべての銘柄でデータ取得に失敗", self.text())


class TestInActions(ScanBase):
    def test_log_does_not_reveal_watchlist(self):
        """公開リポジトリのログに、ウォッチリストの銘柄・内容が出ないこと"""
        self.seed(["7203", "AAPL", "FAKE1", "ERR"], ["トヨタ", "", "", ""])
        self.assertEqual(self.run_scan(ACTIONS_ENV), 0)
        log = self.text()
        for secret in ("7203", "AAPL", "FAKE1", "ERR", "トヨタ", "TOYOTA", "secret-internal-detail", WEBHOOK):
            self.assertNotIn(secret, log, f"ログに「{secret}」が出ている")
        self.assertIn("対象 4銘柄", log)
        self.assertEqual(set(self.store.load_history()["実行元"]), {"actions"})

    def test_missing_webhook_fails_loudly(self):
        self.seed(["FAKE1"])
        self.assertEqual(self.run_scan({"GITHUB_ACTIONS": "true"}), 1)
        self.assertIn("DISCORD_WEBHOOK_URL が設定されていません", self.text())

    def test_missing_sheet_config_fails_instead_of_scanning_sample_list(self):
        self.cfg = None
        self.assertEqual(self.run_scan(ACTIONS_ENV), 1)
        self.assertEqual(self.discord.sent, [])
        self.assertIn("接続情報", self.text())


class TestLocalFallback(ScanBase):
    def test_uses_builtin_watchlist_without_sheet(self):
        self.cfg = None
        with mock.patch.object(auto_scan, "WATCHLIST", ["7203.T", "FAKE1"]):
            self.assertEqual(self.run_scan(LOCAL_ENV), 0)
        self.assertIn("WATCHLIST で実行します", self.text())
        self.assertIn("7203.T", self.text())               # 手元実行ではログに銘柄が出てよい
        self.assertEqual(len(self.discord.sent), 1)         # 既定の利回り条件(4%)でFAKE1(5%)・7203(3.2%はNG)
        self.assertEqual(self.discord.sent[0].count("### 🏢"), 1)

    def test_connection_error_falls_back_locally(self):
        def boom(cfg):
            raise ConnectionError("no network")
        with mock.patch.object(invest_store, "make_store", boom), mock.patch.object(auto_scan, "WATCHLIST", ["FAKE1"]):
            self.assertEqual(self.run_scan(LOCAL_ENV), 0)
        self.assertIn("接続できませんでした", self.text())
        self.assertEqual(len(self.discord.sent), 1)


if __name__ == "__main__":
    unittest.main()
