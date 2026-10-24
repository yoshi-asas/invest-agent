import json
import os

SECRETS_PATH = ".streamlit/secrets.toml"
SECTION = "[connections.gsheets]"


def keep_other_settings(path):
    """既存の secrets.toml から、[connections.gsheets] 以外の設定（APP_PASSWORD など）を取り出す"""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    kept, skipping = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):  # セクションの開始
            skipping = stripped == SECTION or stripped.startswith(SECTION[:-1] + ".")
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


print("==========================================")
print("📊 データベース連携セットアップツール 📊")
print("==========================================\n")

print("【ステップ1】スプレッドシートのURL")
print("先ほど共有設定を行ったスプレッドシートを開き、ブラウザ上部のURLをすべてコピーして貼り付けてください。")
url = input("URL > ").strip()

print("\n【ステップ2】ダウンロードしたJSONファイルのパス")
print("ダウンロードフォルダにあるJSONファイルを右クリックして『パスのコピー（またはパスとしてコピー）』を選び、そのまま貼り付けてください。")
print("※「\"C:\\Users\\...\\xxx.json\"」のような形式になります。")
json_path = input("JSONファイルのパス > ").strip()
json_path = json_path.strip('"').strip("'")  # パスのコピーで付くクォーテーションを自動で外す

print("\n設定ファイルを作成中です...")
try:
    with open(json_path, 'r', encoding='utf-8') as f:
        creds = json.load(f)

    # Streamlit用の隠しフォルダを作成
    os.makedirs(".streamlit", exist_ok=True)

    # 既存の設定（APP_PASSWORD など）は残し、スプレッドシート接続部分だけを書き換える
    other_settings = keep_other_settings(SECRETS_PATH)

    # TOML形式に変換して鍵を保存（JSONの文字列表記はTOMLの基本文字列としてそのまま使える）
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        if other_settings:
            f.write(other_settings + "\n\n")
        f.write(SECTION + "\n")
        f.write(f"spreadsheet = {json.dumps(url)}\n")
        for k, v in creds.items():
            f.write(f"{k} = {json.dumps(v if isinstance(v, str) else str(v))}\n")

    print("✅ 成功！魔法の金庫ファイル（.streamlit/secrets.toml）が作成されました！")
    print("これであなたのアプリとスプレッドシートが完全に繋がりました。")
    print("まず  python check_sheet.py  を実行して、書き込みができることを確認してください（既存のシートには触れません）。")
    print("そのあと、アプリの「タブ2: 注目銘柄＆スキャン」画面を開き、試しに何か銘柄を登録してみてください！")

    if "APP_PASSWORD" not in other_settings:
        print("\n⚠️ secrets.toml に APP_PASSWORD がありません。このままだとアプリのパスワードは「0000」になります。")
        print('   ファイルの先頭（[connections.gsheets] より上）に  APP_PASSWORD = "好きなパスワード"  を追記してください。')

except FileNotFoundError:
    print("❌ エラー: 指定されたJSONファイルが見つかりません。貼り付けたパスが正しいか確認してください。")
except Exception as e:
    print(f"❌ 予期せぬエラーが発生しました: {e}")
