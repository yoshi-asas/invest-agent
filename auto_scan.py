import yfinance as yf
import urllib.request
import json
import time
from datetime import datetime

# ==========================================
# ⚙️ 設定エリア（ここをご自身の環境に合わせて書き換えてください）
# ==========================================

# 1. 自動チェックしたいティッカーシンボルのリスト
WATCHLIST = [
    "7203.T", # トヨタ自動車
    "8058.T", # 三菱商事
    "8316.T", # 三井住友FG
    "9432.T", # NTT
    "AAPL",   # Apple
    "MSFT",   # Microsoft
    "NVDA"    # NVIDIA
]

# 2. アラート発動の条件（%）
ALERT_DROP_PCT = 15.0  # 52週高値から何%下落したら「買い時急落アラート」とするか
ALERT_YIELD_PCT = 4.0  # 配当利回りが何%以上になったら「お宝高配当化アラート」とするか

# 3. DiscordのWebhook URL
# ※ここに先ほど取得したWebhook URLを貼り付けてください
WEBHOOK_URL = "https://discordapp.com/api/webhooks/1488526354210361518/FygJfxM_wU5uvSfl9GpicegbCEvtr8_dfzhN3FcWZLHG20cFQtr5zb-JYfBi11EAoM6a"

# ==========================================
# 🚀 監視・実行プログラム 本体
# ==========================================
def run_auto_scan():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now_str}] ウォッチリストの一括自動スキャンを開始します...")
    
    all_alerts = []
    
    for i, ticker in enumerate(WATCHLIST):
        print(f"スキャン中 ({i+1}/{len(WATCHLIST)}): {ticker}")
        time.sleep(1) # API制限エラーを避けるための1秒待機
        
        try:
            info = yf.Ticker(ticker).info
            current_price = info.get('currentPrice')
            high_52 = info.get('fiftyTwoWeekHigh')
            currency = info.get('currency', '')
            company_name = info.get('shortName', ticker)
            
            # --- 1. 下落率の計算 ---
            drop_pct = 0
            if current_price and high_52 and high_52 > 0:
                drop_pct = (high_52 - current_price) / high_52
            
            # --- 2. 配当利回りの計算 ---
            div_rate = info.get('dividendRate')
            if div_rate and current_price and current_price != 'N/A' and float(current_price) > 0:
                div_yield = div_rate / float(current_price)
            else:
                div_yield = info.get('dividendYield', 0)
                if div_yield and div_yield >= 1.0:
                    div_yield = div_yield / 100
            div_yield = div_yield if div_yield else 0
            
            # --- 3. アラート判定 ---
            stock_alerts = []
            if drop_pct * 100 >= ALERT_DROP_PCT:
                stock_alerts.append(f"⚠️ **高値から急落!!** (52週高値 {high_52} → 現在 {current_price} : **{drop_pct*100:.1f}%下落**)")
            
            if div_yield * 100 >= ALERT_YIELD_PCT:
                stock_alerts.append(f"💰 **お宝高配当化!!** (現在の配当利回り: **{div_yield*100:.2f}%**)")
            
            # 条件に合致していればアラート集計リストへ追加
            if stock_alerts:
                alert_detail = f"### 🏢 {company_name} ({ticker})\n💰現在値: {current_price} {currency}\n" + "\n".join([f"- {a}" for a in stock_alerts])
                all_alerts.append(alert_detail)
                
        except Exception as e:
            print(f"[エラー] {ticker} のデータ取得に失敗しました: {e}")
    
    print("-" * 40)
    print("スキャン完了！")
    
    # スキャン結果の総括とDiscord通知
    if all_alerts:
        print(f"🚨 スキャンの結果、{len(all_alerts)}銘柄でアラート条件が発動しました。")
        combined_message = f"### 🔔 【自動実行】監視リスト 定期チェック報告 ({now_str})\nお気に入り銘柄の中で、指定された条件に到達した「異常値・買い時チャンス（？）」があります！\n\n" + "\n\n".join(all_alerts)
        
        # Webhook URLが設定されている場合のみ送信
        if WEBHOOK_URL and not WEBHOOK_URL.startswith("ここにDiscordのWebhook URL"):
            payload = {"content": combined_message}
            req = urllib.request.Request(
                WEBHOOK_URL, 
                data=json.dumps(payload).encode("utf-8"), 
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            try:
                with urllib.request.urlopen(req) as response:
                    print("✅ Discordへアラートを自動送信しました！")
            except Exception as e:
                print(f"❌ Discord一括通知に失敗しました: {e}")
        else:
            print("⚠️ Discordへの送信は行われませんでした（Webhook URLが設定されていません）。")
            print("送信される予定だったメッセージ内容:")
            print("\n", combined_message)
    else:
        print("✅ アラート条件に合致する「異常値」を記録した銘柄はありません。（すべて通常運行です）")

if __name__ == "__main__":
    run_auto_scan()
