"""Googleスプレッドシートへの接続と書き込みの動作確認ツール（最初に1回だけ実行してください）。

    python check_sheet.py

一時的なシート「_接続テスト」を作成して、書き込み・読み戻し・上書き・追記・削除を確かめます。
すでにあるシート（シート1 など）には一切触れません。最後に一時シートは自動で削除されます。
"""
import sys

import invest_store


def main():
    cfg = invest_store.load_gsheets_config(None)
    if not cfg:
        print("❌ スプレッドシートの接続情報が見つかりません。.streamlit/secrets.toml の [connections.gsheets] を確認してください。")
        return 1
    try:
        backend = invest_store.GspreadBackend(cfg["creds"], cfg["url"])
    except Exception as e:
        print(f"❌ スプレッドシートに接続できませんでした（{type(e).__name__}）。")
        print("   ・サービスアカウントのメールアドレスを、スプレッドシートの「共有」で『編集者』として追加していますか？")
        print("   ・Google Sheets API / Google Drive API は有効ですか？")
        return 1
    print("✅ スプレッドシートに接続できました。既存のシート:", "、".join(backend.titles()))
    ok = invest_store.selftest(backend)
    print("\n🎉 すべて問題ありません。アプリを使い始められます。" if ok else "\n⚠️ 問題があります。上の ❌ の項目を確認してください。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
