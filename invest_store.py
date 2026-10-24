"""Googleスプレッドシートへの保存（ウォッチリスト・保有株・設定・通知済み・スキャン履歴・AIレポート）。

Webアプリ（app.py）と定期スキャン（auto_scan.py / GitHub Actions）の両方から同じコードで読み書きする。
保存先の実体は backend に差し替え可能（本番: GspreadBackend、テスト: MemoryBackend）。
"""
import datetime
import json
import math
import numbers
import os
import time

import pandas as pd

from invest_core import HISTORY_COLUMNS, PORTFOLIO_COLUMNS, SCAN_DEFAULTS, STATE_COLUMNS

SHEETS = {
    "watchlist": {"title": "シート1", "alt": ["Sheet1"], "columns": ["銘柄コード", "企業名", "注目理由"]},
    "portfolio": {"title": "保有株", "columns": PORTFOLIO_COLUMNS},
    "settings": {"title": "設定", "columns": ["項目", "値"]},
    "state": {"title": "通知済み", "columns": STATE_COLUMNS},
    "history": {"title": "スキャン履歴", "columns": HISTORY_COLUMNS},
    "reports": {"title": "AIレポート", "columns": ["日時", "銘柄コード", "企業名", "現在値", "レポート"]},
}


def cell(v):
    """シートに書き込める値（文字列・数値）に変換する。NaN/None は空文字"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple, dict, set)):
        return json.dumps(v, ensure_ascii=False, default=str)
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, numbers.Integral):
        return int(v)
    if isinstance(v, numbers.Real):
        return float(v) if math.isfinite(float(v)) else ""
    if isinstance(v, (datetime.datetime, pd.Timestamp)):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, datetime.date):
        return v.isoformat()
    return str(v)


def df_from_values(values, columns):
    """シートの値（2次元リスト）をDataFrameに変換する。空行は除き、スキーマの列を必ず持たせる"""
    if not values:
        return pd.DataFrame(columns=columns)
    header = [str(h).strip() for h in values[0]]
    keep = [i for i, h in enumerate(header) if h]
    rows = []
    for r in values[1:]:
        r = list(r) + [""] * (len(header) - len(r))
        row = [r[i] for i in keep]
        if any(str(x).strip() for x in row):
            rows.append(row)
    df = pd.DataFrame(rows, columns=[header[i] for i in keep])
    for c in columns:
        if c not in df.columns:
            df[c] = ""
    return df[columns + [c for c in df.columns if c not in columns]].reset_index(drop=True)


class MemoryBackend:
    """メモリ上の疑似スプレッドシート（テスト用）"""

    def __init__(self, sheets=None):
        self.sheets = {k: [list(r) for r in v] for k, v in (sheets or {}).items()}
        self.calls = []

    def titles(self):
        return list(self.sheets)

    def read_values(self, title):
        self.calls.append(("read", title))
        v = self.sheets.get(title)
        return None if v is None else [list(r) for r in v]

    def header(self, title):
        v = self.sheets.get(title)
        return None if not v else list(v[0])

    def write_values(self, title, values):
        self.calls.append(("write", title))
        self.sheets[title] = [list(r) for r in values]

    def append_values(self, title, rows):
        self.calls.append(("append", title))
        self.sheets.setdefault(title, []).extend([list(r) for r in rows])

    def delete_sheet(self, title):
        self.sheets.pop(title, None)


class GspreadBackend:
    """本物のGoogleスプレッドシート（gspread・サービスアカウント認証）"""

    def __init__(self, creds, spreadsheet_url):
        import gspread
        self._gspread = gspread
        self.sh = gspread.service_account_from_dict(creds).open_by_url(spreadsheet_url)
        self._ws = {}
        self._missing = {}      # 「存在しない」と分かったシート名 → 確認した時刻（毎回APIで確認し直さないため）

    def titles(self):
        return [w.title for w in self.sh.worksheets()]

    def _get(self, title):
        if title in self._ws:
            return self._ws[title]
        if time.time() - self._missing.get(title, -1e9) < 30:
            return None
        try:
            ws = self.sh.worksheet(title)
        except self._gspread.WorksheetNotFound:
            self._missing[title] = time.time()
            return None
        self._ws[title] = ws
        return ws

    def read_values(self, title):
        ws = self._get(title)
        return None if ws is None else ws.get_all_values()

    def header(self, title):
        ws = self._get(title)
        return None if ws is None else ws.row_values(1)

    def write_values(self, title, values):
        """全体を書き換える。新しい内容を先に書き込み、余った範囲だけを後で消す（途中で失敗しても元のデータが消えない）"""
        from gspread.utils import rowcol_to_a1
        n_rows = len(values)
        n_cols = max((len(r) for r in values), default=1)
        ws = self._get(title)
        if ws is None:
            ws = self.sh.add_worksheet(title=title, rows=max(n_rows + 50, 100), cols=max(n_cols, 5))
            self._ws[title] = ws
            self._missing.pop(title, None)
            ws.update(range_name="A1", values=values)
            return
        if ws.row_count < n_rows or ws.col_count < n_cols:
            ws.resize(rows=max(ws.row_count, n_rows + 50), cols=max(ws.col_count, n_cols))
        old_rows, old_cols = ws.row_count, ws.col_count
        ws.update(range_name="A1", values=values)
        leftovers = []
        if old_rows > n_rows:
            leftovers.append(f"{rowcol_to_a1(n_rows + 1, 1)}:{rowcol_to_a1(old_rows, old_cols)}")
        if old_cols > n_cols and n_rows > 0:
            leftovers.append(f"{rowcol_to_a1(1, n_cols + 1)}:{rowcol_to_a1(n_rows, old_cols)}")
        if leftovers:
            ws.batch_clear(leftovers)

    def append_values(self, title, rows):
        ws = self._get(title)
        ws.append_rows(rows, value_input_option="RAW")

    def delete_sheet(self, title):
        ws = self._get(title)
        if ws is not None:
            self.sh.del_worksheet(ws)
        self._ws.pop(title, None)
        self._missing.pop(title, None)

    def reset(self):
        """シートの参照を忘れる（シートを削除・作り直した場合に備える）"""
        self._ws.clear()
        self._missing.clear()


class SheetStore:
    """シートごとの読み書き。読み込みは短時間キャッシュし、書き込み時にそのシートのキャッシュを捨てる"""

    def __init__(self, backend, cache_seconds=30):
        self.backend = backend
        self.ttl = cache_seconds
        self._cache = {}
        self._watch_title = None

    def _title(self, key):
        d = SHEETS[key]
        if key == "watchlist":
            if self._watch_title:
                return self._watch_title
            titles = self.backend.titles()
            for t in [d["title"]] + d.get("alt", []):
                if t in titles:
                    self._watch_title = t
                    return t
        return d["title"]

    def clear_cache(self):
        """読み込みキャッシュを捨てる（スプレッドシートを直接編集した直後などに使う）"""
        self._cache.clear()
        self._watch_title = None
        getattr(self.backend, "reset", lambda: None)()

    def read_df(self, key, fresh=False):
        ent = self._cache.get(key)
        if ent and not fresh and time.time() - ent[0] < self.ttl:
            return ent[1].copy()
        values = self.backend.read_values(self._title(key))
        df = df_from_values(values or [], SHEETS[key]["columns"])
        self._cache[key] = (time.time(), df)
        return df.copy()

    def write_df(self, key, df):
        cols = list(df.columns)
        values = [cols] + [[cell(v) for v in row] for row in df.itertuples(index=False, name=None)]
        self.backend.write_values(self._title(key), values)
        self._cache.pop(key, None)

    def append_rows(self, key, rows):
        """末尾に行を追加する（シートが無ければ見出し付きで作成）"""
        if not rows:
            return
        title, cols = self._title(key), SHEETS[key]["columns"]
        header = self.backend.header(title)
        if not header:
            self.backend.write_values(title, [cols] + [[cell(r.get(c, "")) for c in cols] for r in rows])
        else:
            header = [h.strip() for h in header]
            self.backend.append_values(title, [[cell(r.get(c, "")) for c in header] for r in rows])
        self._cache.pop(key, None)

    # --- ウォッチリスト / 保有株
    def load_watchlist(self, fresh=False):
        return self.read_df("watchlist", fresh)

    def save_watchlist(self, df):
        self.write_df("watchlist", df)

    def load_portfolio(self, fresh=False):
        return self.read_df("portfolio", fresh)

    def save_portfolio(self, df):
        self.write_df("portfolio", df)

    # --- 設定（スキャン条件）
    def load_settings(self):
        s = dict(SCAN_DEFAULTS)
        for _, r in self.read_df("settings").iterrows():
            k = str(r["項目"]).strip()
            if k in s:
                try:
                    s[k] = type(SCAN_DEFAULTS[k])(float(str(r["値"]).replace(",", "")))
                except ValueError:
                    pass
        return s

    def save_settings(self, settings):
        self.write_df("settings", pd.DataFrame([{"項目": k, "値": v} for k, v in settings.items()]))

    # --- 通知済みアラート
    def load_state(self, fresh=False):
        return self.read_df("state", fresh)

    def save_state(self, df):
        self.write_df("state", df if len(df.columns) else pd.DataFrame(columns=STATE_COLUMNS))

    # --- スキャン履歴
    def append_history(self, rows):
        self.append_rows("history", rows)

    def load_history(self):
        df = self.read_df("history")
        return df.sort_values("日時", ascending=False).reset_index(drop=True) if len(df) else df

    # --- AIレポート
    def save_report(self, symbol, name, price, report, when):
        self.append_rows("reports", [{"日時": when.strftime("%Y-%m-%d %H:%M"), "銘柄コード": symbol, "企業名": name,
                                      "現在値": price if price is not None else "", "レポート": report}])

    def load_reports(self, symbol=None):
        df = self.read_df("reports")
        if len(df) and symbol:
            df = df[df["銘柄コード"] == symbol]
        return df.sort_values("日時", ascending=False).reset_index(drop=True) if len(df) else df


# ==========================================================
# 接続設定の読み込み
# ==========================================================
def _cfg_from_mapping(g):
    g = dict(g)
    url = g.get("spreadsheet")
    creds = {k: v for k, v in g.items() if k != "spreadsheet"}
    if not url or not creds.get("private_key"):
        return None
    return {"creds": creds, "url": url}


def _load_toml(path):
    try:
        import tomllib as toml_mod  # Python 3.11+
        with open(path, "rb") as f:
            return toml_mod.load(f)
    except ImportError:
        pass
    try:
        import tomli as toml_mod
        with open(path, "rb") as f:
            return toml_mod.load(f)
    except ImportError:
        import toml as toml_mod
        with open(path, encoding="utf-8") as f:
            return toml_mod.load(f)


def load_gsheets_config(secrets=None):
    """接続情報を探す。優先順: ①引数の secrets（Streamlitの st.secrets）②環境変数 ③手元の .streamlit/secrets.toml。無ければ None"""
    if secrets is not None:
        try:
            cfg = _cfg_from_mapping(secrets["connections"]["gsheets"])
            if cfg:
                return cfg
        except Exception:
            pass
    raw, url = os.environ.get("GCP_SERVICE_ACCOUNT_JSON"), os.environ.get("SPREADSHEET_URL")
    if raw and url:
        try:
            return {"creds": json.loads(raw), "url": url}
        except ValueError:
            return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
    if os.path.exists(path):
        try:
            return _cfg_from_mapping(_load_toml(path)["connections"]["gsheets"])
        except Exception:
            return None
    return None


def make_store(cfg):
    """接続設定からスプレッドシートのストアを作る"""
    return SheetStore(GspreadBackend(cfg["creds"], cfg["url"]))


def merge_watchlist_edit(df, edited_df):
    """表（data_editor）の編集結果（追加・削除・書き換え）を、保存用のDataFrameに変換する。
    元のシートにあった追加列は、残った行について保持する。銘柄コードは正規化し、空のコードの行は除く。"""
    from invest_core import normalize_ticker
    saved = df.reindex(edited_df.index)  # 残った行だけ残す（追加行はNaN、削除行は消える）
    for col in edited_df.columns:
        saved[col] = edited_df[col]
    saved["銘柄コード"] = saved["銘柄コード"].apply(normalize_ticker)
    saved = saved[saved["銘柄コード"] != ""]
    return saved.reset_index(drop=True)


def selftest(backend, title="_接続テスト", log=print):
    """一時シートを作り、書き込み・読み戻し・縮小して上書き・追記・削除ができるかを確かめる。
    既存のシートには触れない（title が既に存在する場合は中止する）。成功すれば True"""
    if title in backend.titles():
        log(f"❌ 「{title}」という名前のシートが既にあるため、動作確認を中止しました（既存のシートを壊さないため）。")
        return False
    store = SheetStore(backend, cache_seconds=0)
    ok = True

    def check(label, cond):
        nonlocal ok
        log(("✅ " if cond else "❌ ") + label)
        ok = ok and bool(cond)

    try:
        big = [["銘柄", "値"]] + [[f"T{i}", i] for i in range(120)]
        backend.write_values(title, big)
        check("新しいシートを作って書き込める（120行）", len(backend.read_values(title)) == 121)
        backend.write_values(title, [["銘柄", "値"], ["A", 1.5]])
        back = backend.read_values(title)
        check("少ないデータで上書きすると、余った行が消える", len(back) == 2 and back[1][0] == "A")
        backend.append_values(title, [["B", "=1+1"]])
        back = backend.read_values(title)
        check("末尾に追記できる（数式は文字として保存される）", len(back) == 3 and back[2] == ["B", "=1+1"])
        df = df_from_values(back, ["銘柄", "値"])
        check("DataFrameとして読み戻せる", list(df["銘柄"]) == ["A", "B"])
    except Exception as e:
        check(f"動作確認中にエラーが発生しました: {type(e).__name__}: {e}", False)
    finally:
        try:
            backend.delete_sheet(title)
            check("一時シートを削除できる", title not in backend.titles())
        except Exception as e:
            log(f"⚠️ 一時シート「{title}」を自動で削除できませんでした。スプレッドシートで手動で削除してください。（{type(e).__name__}）")
            ok = False
    return ok
