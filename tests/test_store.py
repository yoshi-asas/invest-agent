import datetime
import json
import os
import unittest
from unittest import mock

import gspread
import pandas as pd
from gspread.utils import a1_to_rowcol

import invest_store as st


# ------------------------------------------------------------------
# gspread の偽物（セルをdictで持ち、本物の gspread.utils でA1表記を解釈する）
# ------------------------------------------------------------------
class FakeWorksheet:
    def __init__(self, title, rows, cols, log):
        self.title, self.row_count, self.col_count, self.log = title, rows, cols, log
        self.cells = {}
        self.fail_update = False

    def get_all_values(self):
        if not self.cells:
            return []
        r = max(k[0] for k in self.cells)
        c = max(k[1] for k in self.cells)
        return [[str(self.cells.get((i, j), "")) for j in range(1, c + 1)] for i in range(1, r + 1)]

    def row_values(self, i):
        vals = self.get_all_values()
        return vals[i - 1] if len(vals) >= i else []

    def update(self, range_name, values=None, **kw):
        self.log.append(("update", self.title, range_name))
        if self.fail_update:
            raise RuntimeError("API error")
        r0, c0 = a1_to_rowcol(range_name)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                if v != "":
                    self.cells[(r0 + i, c0 + j)] = v
                else:
                    self.cells.pop((r0 + i, c0 + j), None)
        assert r0 + len(values) - 1 <= self.row_count, "グリッドの行数を超えて書き込もうとした"
        assert c0 + max(len(r) for r in values) - 1 <= self.col_count, "グリッドの列数を超えて書き込もうとした"

    def batch_clear(self, ranges):
        self.log.append(("batch_clear", self.title, tuple(ranges)))
        for rng in ranges:
            a, b = rng.split(":")
            (r1, c1), (r2, c2) = a1_to_rowcol(a), a1_to_rowcol(b)
            for k in [k for k in self.cells if r1 <= k[0] <= r2 and c1 <= k[1] <= c2]:
                del self.cells[k]

    def resize(self, rows=None, cols=None):
        self.log.append(("resize", self.title, rows, cols))
        self.row_count, self.col_count = rows or self.row_count, cols or self.col_count

    def append_rows(self, values, value_input_option="RAW", **kw):
        self.log.append(("append_rows", self.title, value_input_option))
        r = max([k[0] for k in self.cells], default=0)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self.cells[(r + 1 + i, 1 + j)] = v
        self.row_count = max(self.row_count, r + len(values))


class FakeSpreadsheet:
    def __init__(self):
        self.ws, self.log = {}, []

    def worksheets(self):
        return list(self.ws.values())

    def worksheet(self, title):
        if title not in self.ws:
            raise gspread.WorksheetNotFound(title)
        return self.ws[title]

    def add_worksheet(self, title, rows, cols, index=None):
        self.log.append(("add_worksheet", title, rows, cols))
        self.ws[title] = FakeWorksheet(title, rows, cols, self.log)
        return self.ws[title]

    def del_worksheet(self, ws):
        self.log.append(("del_worksheet", ws.title))
        del self.ws[ws.title]


def make_backend(sh=None):
    sh = sh or FakeSpreadsheet()
    client = mock.Mock()
    client.open_by_url.return_value = sh
    with mock.patch.object(gspread, "service_account_from_dict", return_value=client):
        be = st.GspreadBackend({"private_key": "k"}, "https://example.invalid/sheet")
    return be, sh


class TestHelpers(unittest.TestCase):
    def test_cell(self):
        import numpy as np
        self.assertEqual(st.cell(None), "")
        self.assertEqual(st.cell(float("nan")), "")
        self.assertEqual(st.cell(pd.NaT), "")
        self.assertEqual(st.cell(np.int64(5)), 5)
        self.assertIsInstance(st.cell(np.int64(5)), int)
        self.assertEqual(st.cell(np.float64(1.5)), 1.5)
        self.assertEqual(st.cell(float("inf")), "")
        self.assertEqual(st.cell(True), "True")
        self.assertEqual(st.cell(pd.Timestamp("2026-10-24 12:30")), "2026-10-24 12:30:00")
        self.assertEqual(st.cell(datetime.date(2026, 10, 24)), "2026-10-24")
        self.assertEqual(st.cell("=1+1"), "=1+1")
        self.assertEqual(st.cell([1, "あ"]), '[1, "あ"]')

    def test_df_from_values(self):
        cols = ["a", "b"]
        self.assertEqual(list(st.df_from_values([], cols).columns), cols)
        df = st.df_from_values([["a", "", "c"], ["1", "x", "3"], ["", "", ""], ["2"]], cols)
        self.assertEqual(list(df.columns), ["a", "b", "c"])        # 空見出しの列は捨て、スキーマの列は補う
        self.assertEqual(len(df), 2)                               # 空行は捨てる
        self.assertEqual(df.loc[1, "c"], "")                       # 足りないセルは空文字


class TestGspreadBackend(unittest.TestCase):
    def test_creates_missing_sheet(self):
        be, sh = make_backend()
        self.assertIsNone(be.read_values("保有株"))
        be.write_values("保有株", [["a", "b"], [1, 2]])
        self.assertIn(("add_worksheet", "保有株", 100, 5), sh.log)
        self.assertEqual(be.read_values("保有株"), [["a", "b"], ["1", "2"]])
        self.assertEqual(be.header("保有株"), ["a", "b"])

    def test_overwrite_with_smaller_data_leaves_no_leftovers_and_writes_first(self):
        be, sh = make_backend()
        be.write_values("S", [["h1", "h2", "h3"]] + [[i, i, i] for i in range(10)])
        sh.log.clear()
        be.write_values("S", [["x", "y"], [1, 2]])
        self.assertEqual(be.read_values("S"), [["x", "y"], ["1", "2"]])
        ops = [e[0] for e in sh.log]
        self.assertLess(ops.index("update"), ops.index("batch_clear"))     # 先に書いてから余りを消す

    def test_grows_grid_when_needed(self):
        be, sh = make_backend()
        be.write_values("S", [["a"]])
        big = [["a", "b", "c", "d", "e", "f", "g"]] + [[i] * 7 for i in range(300)]
        be.write_values("S", big)
        self.assertEqual(len(be.read_values("S")), 301)
        self.assertTrue(any(e[0] == "resize" for e in sh.log))

    def test_failed_update_keeps_old_data(self):
        be, sh = make_backend()
        be.write_values("S", [["a", "b"], [1, 2]])
        sh.ws["S"].fail_update = True
        with self.assertRaises(RuntimeError):
            be.write_values("S", [["x"], [9]])
        self.assertEqual(be.read_values("S"), [["a", "b"], ["1", "2"]])
        self.assertFalse(any(e[0] == "batch_clear" for e in sh.log))

    def test_append_is_raw(self):
        be, sh = make_backend()
        be.write_values("S", [["a"], [1]])
        be.append_values("S", [["=SUM(1)"]])
        self.assertIn(("append_rows", "S", "RAW"), sh.log)
        self.assertEqual(be.read_values("S")[-1], ["=SUM(1)"])


class TestBackendExtras(unittest.TestCase):
    def test_missing_sheet_lookup_is_cached_until_created(self):
        be, sh = make_backend()
        calls = []
        orig = sh.worksheet
        sh.worksheet = lambda t: (calls.append(t), orig(t))[1]
        self.assertIsNone(be.read_values("設定"))
        self.assertIsNone(be.read_values("設定"))
        self.assertEqual(calls, ["設定"])                         # 2回目はAPIに問い合わせない
        be.write_values("設定", [["項目", "値"], ["a", 1]])
        self.assertEqual(be.read_values("設定")[1], ["a", "1"])      # 作成後は読める
        be.reset()
        self.assertEqual(be.read_values("設定")[1], ["a", "1"])

    def test_delete_sheet(self):
        be, sh = make_backend()
        be.write_values("X", [["a"]])
        be.delete_sheet("X")
        self.assertNotIn("X", sh.ws)
        be.delete_sheet("X")                                      # 無いシートの削除は何もしない


class TestSelfTest(unittest.TestCase):
    def test_passes_on_memory_backend(self):
        lines = []
        be = st.MemoryBackend({"シート1": [["銘柄コード"], ["7203.T"]]})
        self.assertTrue(st.selftest(be, log=lines.append))
        self.assertEqual(be.titles(), ["シート1"])                  # 一時シートは消え、既存シートはそのまま
        self.assertEqual(be.sheets["シート1"], [["銘柄コード"], ["7203.T"]])

    def test_passes_on_gspread_backend_and_cleans_up(self):
        be, sh = make_backend()
        sh.add_worksheet("シート1", 100, 5)
        sh.ws["シート1"].cells[(1, 1)] = "銘柄コード"
        lines = []
        self.assertTrue(st.selftest(be, log=lines.append), lines)
        self.assertEqual(list(sh.ws), ["シート1"])
        self.assertEqual(sh.ws["シート1"].cells, {(1, 1): "銘柄コード"})   # 既存シートは無傷
        self.assertFalse([l for l in lines if l.startswith("❌")])

    def test_refuses_when_title_already_exists(self):
        be = st.MemoryBackend({"_接続テスト": [["大事なデータ"]]})
        lines = []
        self.assertFalse(st.selftest(be, log=lines.append))
        self.assertEqual(be.sheets["_接続テスト"], [["大事なデータ"]])      # 触っていない
        self.assertIn("中止", "".join(lines))

    def test_reports_failure_and_still_cleans_up(self):
        be = st.MemoryBackend()
        orig = be.append_values
        be.append_values = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("API error"))
        lines = []
        self.assertFalse(st.selftest(be, log=lines.append))
        self.assertEqual(be.titles(), [])
        self.assertTrue(any("エラーが発生しました" in l for l in lines))


class TestSheetStore(unittest.TestCase):
    def setUp(self):
        self.be = st.MemoryBackend()
        self.store = st.SheetStore(self.be, cache_seconds=60)

    def test_missing_sheet_gives_empty_schema_frame(self):
        for key in st.SHEETS:
            df = self.store.read_df(key)
            self.assertEqual(list(df.columns), st.SHEETS[key]["columns"])
            self.assertTrue(df.empty)

    def test_roundtrip_keeps_extra_columns(self):
        df = pd.DataFrame({"銘柄コード": ["7203.T", "AAPL"], "企業名": ["トヨタ", "Apple"], "注目理由": ["", "x"], "メモ": ["m1", "m2"]})
        self.store.save_watchlist(df)
        back = self.store.load_watchlist(fresh=True)
        self.assertEqual(list(back.columns), ["銘柄コード", "企業名", "注目理由", "メモ"])
        self.assertEqual(list(back["メモ"]), ["m1", "m2"])

    def test_watchlist_uses_existing_alt_title(self):
        be = st.MemoryBackend({"Sheet1": [["銘柄コード", "企業名", "注目理由"], ["7203.T", "トヨタ", ""]]})
        store = st.SheetStore(be)
        self.assertEqual(len(store.load_watchlist()), 1)
        store.save_watchlist(pd.DataFrame({"銘柄コード": ["AAPL"], "企業名": ["Apple"], "注目理由": [""]}))
        self.assertEqual(be.sheets["Sheet1"][1][0], "AAPL")
        self.assertNotIn("シート1", be.sheets)

    def test_cache_and_invalidation(self):
        self.store.save_watchlist(pd.DataFrame({"銘柄コード": ["A"], "企業名": ["a"], "注目理由": [""]}))
        self.be.calls.clear()
        self.store.load_watchlist()
        self.store.load_watchlist()
        self.assertEqual(len([c for c in self.be.calls if c[0] == "read"]), 1)         # 2回目はキャッシュ
        self.store.save_watchlist(pd.DataFrame({"銘柄コード": ["B"], "企業名": ["b"], "注目理由": [""]}))
        self.assertEqual(list(self.store.load_watchlist()["銘柄コード"]), ["B"])      # 書き込み後は最新
        self.be.calls.clear()
        self.store.load_watchlist(fresh=True)
        self.assertEqual(len([c for c in self.be.calls if c[0] == "read"]), 1)

    def test_returned_frames_are_copies(self):
        self.store.save_portfolio(pd.DataFrame({"銘柄コード": ["A"], "企業名": ["a"], "株数": [1], "取得単価": [1], "メモ": [""]}))
        d = self.store.load_portfolio()
        d.loc[0, "株数"] = 999
        self.assertNotEqual(self.store.load_portfolio().loc[0, "株数"], 999)

    def test_append_creates_header_then_appends_in_header_order(self):
        self.store.append_history([{"日時": "2026-10-24 12:00", "銘柄コード": "A", "企業名": "a", "アラート種別": "drop",
                                    "内容": "x", "現在値": 100.0, "実行元": "app"}])
        self.assertEqual(self.be.sheets["スキャン履歴"][0], ["日時", "銘柄コード", "企業名", "アラート種別", "内容", "現在値", "実行元"])
        # 見出しの順序を入れ替えたシートにも、列名で合わせて追記できる
        self.be.sheets["スキャン履歴"][0] = ["銘柄コード", "日時", "企業名", "アラート種別", "内容", "現在値", "実行元"]
        self.store.append_history([{"日時": "2026-10-25 12:00", "銘柄コード": "B", "企業名": "b", "アラート種別": "yield",
                                    "内容": "y", "現在値": 50, "実行元": "actions"}])
        self.assertEqual(self.be.sheets["スキャン履歴"][-1][:2], ["B", "2026-10-25 12:00"])
        self.store.append_history([])                                   # 空は何もしない

    def test_history_sorted_desc_and_reports_filter(self):
        for d, s in (("2026-10-01 10:00", "A"), ("2026-10-03 10:00", "B"), ("2026-10-02 10:00", "A")):
            self.store.append_history([{"日時": d, "銘柄コード": s, "企業名": s, "アラート種別": "drop", "内容": "", "現在値": 1, "実行元": "app"}])
        self.assertEqual(list(self.store.load_history()["日時"]), ["2026-10-03 10:00", "2026-10-02 10:00", "2026-10-01 10:00"])
        w = datetime.datetime(2026, 10, 24, 12, 0)
        self.store.save_report("A", "a", 100.0, "レポート1\n改行あり", w)
        self.store.save_report("B", "b", None, "レポートB", w)
        self.store.save_report("A", "a", 110.0, "レポート2", w + datetime.timedelta(days=1))
        reps = self.store.load_reports("A")
        self.assertEqual(list(reps["レポート"]), ["レポート2", "レポート1\n改行あり"])
        self.assertEqual(len(self.store.load_reports()), 3)

    def test_settings_roundtrip_types_and_defaults(self):
        s = self.store.load_settings()
        self.assertEqual(s, st.SCAN_DEFAULTS)
        s["drop_pct"], s["days_before"] = 12.5, 30
        self.store.save_settings(s)
        back = self.store.load_settings()
        self.assertEqual(back["drop_pct"], 12.5)
        self.assertEqual(back["days_before"], 30)
        self.assertIsInstance(back["days_before"], int)
        self.be.sheets["設定"].append(["rsi", "abc"])             # 壊れた値は既定値のまま
        self.be.sheets["設定"].append(["unknown", "1"])           # 未知の項目は無視
        self.store._cache.clear()
        back = self.store.load_settings()
        self.assertEqual(back["rsi"], st.SCAN_DEFAULTS["rsi"])
        self.assertNotIn("unknown", back)

    def test_state_roundtrip_including_empty(self):
        df = pd.DataFrame([{"銘柄コード": "A", "アラート種別": "drop", "初回通知日": "2026-10-01", "最終確認日": "2026-10-02"}])
        self.store.save_state(df)
        self.assertEqual(len(self.store.load_state(fresh=True)), 1)
        self.store.save_state(pd.DataFrame(columns=["銘柄コード", "アラート種別", "初回通知日", "最終確認日"]))
        self.assertTrue(self.store.load_state(fresh=True).empty)


class TestWatchlistMerge(unittest.TestCase):
    def test_add_delete_edit(self):
        df = pd.DataFrame({"銘柄コード": ["7203.T", "8058.T", "9432.T"], "企業名": ["A", "B", "C"], "注目理由": ["", "", ""], "メモ": ["m0", "m1", "m2"]})
        edited = df[["銘柄コード", "企業名", "注目理由"]].drop(index=1).copy()    # 行1を削除
        edited.loc[3] = ["2914", "", "高配当"]                                    # 追加（.Tなし・企業名なし）
        edited.loc[0, "注目理由"] = "EV"                                          # 既存行を編集
        edited.loc[4] = [None, None, None]                                        # 空行（コード未入力）
        out = st.merge_watchlist_edit(df, edited)
        self.assertEqual(list(out["銘柄コード"]), ["7203.T", "9432.T", "2914.T"])
        self.assertEqual(out.loc[0, "注目理由"], "EV")
        self.assertEqual(list(out["メモ"])[:2], ["m0", "m2"])                      # 画面に出していない列は保持
        self.assertEqual(out.index.tolist(), [0, 1, 2])


class TestClearCache(unittest.TestCase):
    def test_clear_cache_drops_everything(self):
        be, sh = make_backend()
        store = st.SheetStore(be, cache_seconds=600)
        store.save_portfolio(pd.DataFrame({"銘柄コード": ["A"], "企業名": ["a"], "株数": [1], "取得単価": [1], "メモ": [""]}))
        store.load_portfolio()
        sh.ws["保有株"].cells[(2, 1)] = "B"                      # スプレッドシートを直接編集した想定
        self.assertEqual(store.load_portfolio().loc[0, "銘柄コード"], "A")      # キャッシュのまま
        store.clear_cache()
        self.assertEqual(store.load_portfolio().loc[0, "銘柄コード"], "B")


class TestConfig(unittest.TestCase):
    CREDS = {"type": "service_account", "private_key": "-----BEGIN-----", "client_email": "a@b"}

    def test_from_secrets_mapping(self):
        secrets = {"connections": {"gsheets": {"spreadsheet": "https://x", **self.CREDS}}}
        cfg = st.load_gsheets_config(secrets)
        self.assertEqual(cfg["url"], "https://x")
        self.assertNotIn("spreadsheet", cfg["creds"])
        self.assertEqual(cfg["creds"]["client_email"], "a@b")

    def test_from_env(self):
        env = {"GCP_SERVICE_ACCOUNT_JSON": json.dumps(self.CREDS), "SPREADSHEET_URL": "https://y"}
        with mock.patch.dict(os.environ, env), mock.patch("os.path.exists", return_value=False):
            cfg = st.load_gsheets_config({})
        self.assertEqual(cfg["url"], "https://y")
        with mock.patch.dict(os.environ, {**env, "GCP_SERVICE_ACCOUNT_JSON": "{broken"}):
            self.assertIsNone(st.load_gsheets_config(None))

    def test_none_when_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("os.path.exists", return_value=False):
            self.assertIsNone(st.load_gsheets_config({"connections": {}}))
            self.assertIsNone(st.load_gsheets_config(None))

    def test_incomplete_mapping_is_ignored(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("os.path.exists", return_value=False):
            self.assertIsNone(st.load_gsheets_config({"connections": {"gsheets": {"spreadsheet": "https://x"}}}))


if __name__ == "__main__":
    unittest.main()
