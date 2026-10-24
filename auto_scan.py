"""定期スキャン。手元のPC（タスクスケジューラ）でも、GitHub Actions でも、同じコードで動く。

- Googleスプレッドシートに接続できる場合:
    ウォッチリスト・スキャン条件・通知済みの状態をシートから読み、新しく発生したアラートだけをDiscordに通知し、履歴をシートに記録する
    （Webアプリの「2. 注目銘柄＆スキャン」と同じ判定・同じ条件）
- 接続できない場合（手元PCのみ）:
    下の WATCHLIST と LOCAL_SETTINGS で動く（通知済み管理・履歴の記録はしない）

必要な設定:
    DISCORD_WEBHOOK_URL ... 通知先（.env または環境変数 / GitHub Actions の Secrets）
    スプレッドシート接続 ... .streamlit/secrets.toml、または環境変数 GCP_SERVICE_ACCOUNT_JSON と SPREADSHEET_URL
"""
import os
import sys

import invest_core as core
import invest_store

# ==========================================
# ⚙️ 設定エリア（スプレッドシートに接続できないときだけ使われる）
# ==========================================
WATCHLIST = [
    "7203.T",  # トヨタ自動車
    "8058.T",  # 三菱商事
    "8316.T",  # 三井住友FG
    "9432.T",  # NTT
    "AAPL",    # Apple
    "MSFT",    # Microsoft
    "NVDA",    # NVIDIA
]
LOCAL_SETTINGS = {"drop_pct": 15.0, "yield_pct": 4.0}   # 他の条件は invest_core.SCAN_DEFAULTS


def run_auto_scan(env=None, now=None, out=print, pause=1.0):
    """スキャンを1回実行する。終了コード（0=正常 / 1=異常）を返す"""
    env = os.environ if env is None else env
    in_actions = env.get("GITHUB_ACTIONS") == "true"
    quiet = in_actions            # 公開リポジトリではログも公開されるため、銘柄名などは出力しない
    webhook = env.get("DISCORD_WEBHOOK_URL") or env.get("WEBHOOK_URL")
    now = now or core.now_jst()
    out(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] ウォッチリストの一括自動スキャンを開始します...")

    if in_actions and not webhook:
        out("❌ DISCORD_WEBHOOK_URL が設定されていません（GitHub の Settings → Secrets and variables → Actions に登録してください）。")
        return 1

    # ---- 接続とウォッチリスト・条件の読み込み
    store, names = None, {}
    cfg = invest_store.load_gsheets_config(None)
    if cfg:
        try:
            store = invest_store.make_store(cfg)
        except Exception as e:
            out(f"⚠️ スプレッドシートに接続できませんでした（{type(e).__name__}）。")
    if store is None and in_actions:
        out("❌ スプレッドシートの接続情報（GCP_SERVICE_ACCOUNT_JSON / SPREADSHEET_URL）が設定されていないか、接続できません。")
        return 1
    if store is not None:
        try:
            wl = store.load_watchlist(fresh=True)
            settings = store.load_settings()
        except Exception as e:
            out(f"❌ スプレッドシートの読み込みに失敗しました（{type(e).__name__}）。")
            return 1
        tickers = list(dict.fromkeys(t for t in (core.normalize_ticker(c) for c in wl["銘柄コード"].tolist()) if t))
        names = {core.normalize_ticker(c): str(n) for c, n in zip(wl["銘柄コード"], wl["企業名"])
                 if str(n).strip() and "エラー" not in str(n)}
    else:
        out("ℹ️ スプレッドシートに接続せず、このファイルの WATCHLIST で実行します（通知済み管理・履歴の記録はありません）。")
        tickers, settings = list(WATCHLIST), {**core.SCAN_DEFAULTS, **LOCAL_SETTINGS}
    if not tickers:
        out("スキャン対象の銘柄がありません。")
        return 0

    # ---- スキャン
    def name_of(sym, info):
        return names.get(sym) or core.get_jp_company_info(sym, info.get("shortName", sym), "")[0]

    def progress(i, n, res):
        out(f"スキャン中 ({i + 1}/{n})" if quiet else f"スキャン中 ({i + 1}/{n}): {res['symbol']}"
            + (f"  [エラー] {res['error']}" if res["error"] and not quiet else ""))

    results = core.scan_tickers(tickers, settings, lambda t: core.fetch_info(t, retries=2), core.fetch_history,
                                name_fn=name_of, progress=progress, pause=pause, now=now)
    failed = [r for r in results if r["error"]]
    alerted = [r for r in results if r["alerts"]]
    if len(failed) == len(results):
        out("❌ すべての銘柄でデータ取得に失敗しました（Yahoo Finance のアクセス制限などの可能性があります）。")
        return 1

    # ---- 新規アラートの判定
    if store is not None:
        try:
            state_df = store.load_state(fresh=True)
        except Exception:
            state_df = None
        new_items, new_state = core.filter_new_alerts(results, state_df, now.date(), settings["renotify_days"])
        only = new_items
    else:
        new_items = {(r["symbol"], a["key"]) for r in alerted for a in r["alerts"]}
        new_state, only = None, None

    out("-" * 40)
    out(f"スキャン完了！ 対象 {len(results)}銘柄 / アラート {len(alerted)}銘柄 / 新規アラート {len(new_items)}件 / 取得失敗 {len(failed)}銘柄")

    # ---- 通知
    title = (f"### 🔔 【自動実行】監視リスト 定期チェック報告 ({now.strftime('%Y-%m-%d %H:%M:%S')})\n"
             "お気に入り銘柄の中で、指定された条件に到達した「異常値・買い時チャンス（？）」があります！")
    message = core.format_scan_message(results, title, only=only)
    if message is None:
        out("✅ 通知するアラートはありません。" if not alerted else "✅ アラートはすべて通知済みのため、送信しません。")
    elif webhook:
        try:
            n = core.send_discord(webhook, message)
            out(f"✅ Discordへアラートを自動送信しました！（{n}通）")
        except Exception as e:
            out(f"❌ Discord一括通知に失敗しました（{type(e).__name__}）。通知済みとして記録しません。")
            return 1
    else:
        out("⚠️ Discordへの送信は行われませんでした（DISCORD_WEBHOOK_URL が設定されていません）。")
        out("送信される予定だったメッセージ内容:")
        out("\n" + message)

    # ---- 状態・履歴の記録（通知に成功した場合、または通知するものが無かった場合だけ。送信していないのに「通知済み」にしない）
    if store is not None and (message is None or webhook):
        source = "actions" if in_actions else "local"
        try:
            store.save_state(new_state)
            rows = core.history_rows(results, new_items, source, now)
            if rows:
                store.append_history(rows)
                out(f"📜 履歴に{len(rows)}件記録しました。")
        except Exception as e:
            out(f"⚠️ 状態・履歴の記録に失敗しました（{type(e).__name__}）。次回、同じアラートが再通知される可能性があります。")
    return 0


def main():
    try:   # 手元PCでは .env から DISCORD_WEBHOOK_URL を読む（GitHub Actions では環境変数で渡される）
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass
    sys.exit(run_auto_scan())


if __name__ == "__main__":
    main()
