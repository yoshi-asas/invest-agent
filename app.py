import streamlit as st
import yfinance as yf
import pandas as pd
import openai
import urllib.request
import json
import time
import re

# ページの設定
st.set_page_config(page_title="AI投資エージェント", layout="wide", page_icon="📈")

# ==========================================
# 🔒 アプリ全体のパスワードロック認証
# ==========================================
def check_password():
    # クラウド(Secrets)からパスワードを取得。未設定時(ローカル)は '0000'
    expected_password = st.secrets.get("APP_PASSWORD", "0000")
    
    def password_entered():
        if st.session_state["password"] == expected_password:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.markdown("<h2 style='text-align: center; margin-top: 100px;'>🔒 秘密の投資エージェント</h2>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.info("このアプリは個人用にアクセス制限されています。パスワードを入力してください。")
            st.text_input("パスワード", type="password", on_change=password_entered, key="password")
        return False
    elif not st.session_state["password_correct"]:
        st.markdown("<h2 style='text-align: center; margin-top: 100px;'>🔒 秘密の投資エージェント</h2>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.text_input("パスワード", type="password", on_change=password_entered, key="password")
            st.error("😕 パスワードが間違っています。")
        return False
    else:
        return True

# パスワードが正しくない場合はここで処理を停止（以降の画面を描画しない）
if not check_password():
    st.stop()

# ==========================================
# ユーティリティ関数（企業名・セクターの日本語化）
# ==========================================
@st.cache_data(ttl=3600)
def get_jp_company_info(ticker_symbol, default_name, default_sector):
    name = default_name
    # 日本株の場合はYahooファイナンスから正確な日本語名を取得する
    if ticker_symbol.endswith('.T') or ticker_symbol.endswith('.t'):
        try:
            url = f"https://finance.yahoo.co.jp/quote/{ticker_symbol}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                html = response.read().decode('utf-8')
                match = re.search(r'<title>(.*?)【', html)
                if match:
                    name = match.group(1).replace('(株)', '').strip()
        except:
            pass
            
    # セクター（業種）の日本語変換
    SECTOR_MAP = {
        "Basic Materials": "素材",
        "Consumer Cyclical": "一般消費財",
        "Financial Services": "金融",
        "Real Estate": "不動産",
        "Consumer Defensive": "生活必需品",
        "Healthcare": "ヘルスケア",
        "Utilities": "公益事業",
        "Communication Services": "通信サービス",
        "Energy": "エネルギー",
        "Industrials": "資本財",
        "Technology": "情報技術"
    }
    sector_jp = SECTOR_MAP.get(default_sector, default_sector if default_sector else "不明")
    return name, sector_jp

st.title("📈 株式 長期保有アシスタント")
st.markdown("個別銘柄のAI深掘り分析と、**「監視リストの一括スキャン」**による自動アラート機能を備えたダッシュボードです。")

# タブで機能を分ける
tab_single, tab_batch, tab_portfolio = st.tabs(["🔍 1. 個別分析", "📋 2. 一括スキャン", "💼 3. ポートフォリオ"])

# ==========================================
# サイドバー（共通設定）
# ==========================================
st.sidebar.markdown("**🤖 AI連携設定 (個別分析用)**")
api_key = st.sidebar.text_input("OpenAI APIキー (お持ちの場合のみ入力)", type="password")

st.sidebar.divider()
st.sidebar.markdown("**🔔 通知設定 (Discord自動通知用)**")
webhook_url = st.sidebar.text_input("Discord Webhook URL", type="password")
if not webhook_url:
    st.sidebar.caption("👉 ここにWebhook URLを設定すると、シグナルや一括スキャンの結果をDiscordに送信できます。")

st.sidebar.divider()
st.sidebar.caption("※本アプリは投資判断の参考情報を提供するものであり、投資勧誘を目的とするものではありません。最終的な投資決定はご自身の判断でお願いいたします。")


# ==========================================
# yfinance キャッシュデータ取得関数（Rate Limit対策）
# ==========================================
import requests

yf_session = requests.Session()
yf_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
})

@st.cache_data(ttl=3600, show_spinner=False)
def get_yf_info(t_symbol):
    try:
        return yf.Ticker(t_symbol, session=yf_session).info
    except Exception as e:
        raise e

@st.cache_data(ttl=3600, show_spinner=False)
def get_yf_dividends(t_symbol):
    try:
        return yf.Ticker(t_symbol, session=yf_session).dividends
    except Exception:
        return pd.Series(dtype='float64')


# ==========================================
# タブ1: 個別銘柄の深掘り分析
# ==========================================
with tab_single:
    st.markdown("企業の基本情報や配当推移を取得し、**業界平均との比較**や**長期投資の視点**でAIが詳細な分析レポートを作成します。")
    ticker_symbol = st.text_input("🎯 深掘りする銘柄コード", value="2914")
    st.caption("例: 2914 (JT), AAPL (Apple), 7203 (トヨタ), 8058 (三菱商事)")

    if st.button("個別データを取得して分析", type="primary"):
        ticker_symbol = ticker_symbol.strip().upper()
        if ticker_symbol.isdigit() and len(ticker_symbol) == 4:
            ticker_symbol += ".T"
            
        with st.spinner(f"{ticker_symbol} のリアルタイムデータを取得中..."):
            try:
                info = get_yf_info(ticker_symbol)

                # 企業名・セクターを日本語化
                raw_name = info.get('shortName', ticker_symbol)
                raw_sector = info.get('sector', '')
                company_name, sector_jp = get_jp_company_info(ticker_symbol, raw_name, raw_sector)
                
                st.subheader(f"🏢 企業情報: {company_name} ({sector_jp})")

                # 主要な財務指標を4つのカラムで表示
                col1, col2, col3, col4 = st.columns(4)
                
                current_price = info.get('currentPrice', 0)
                raw_currency = info.get('currency', '')
                currency_str = "円" if raw_currency == "JPY" else raw_currency
                
                col1.metric("現在の株価", f"{current_price} {currency_str}" if current_price else "N/A")
                
                # --- 配当月と次回権利落ち日の取得 ---
                import datetime
                ex_div_timestamp = info.get('exDividendDate')
                ex_div_date = datetime.datetime.fromtimestamp(ex_div_timestamp).strftime('%m/%d') if ex_div_timestamp else ""
                
                dividends = get_yf_dividends(ticker_symbol)
                div_months_str = "N/A"
                if not dividends.empty:
                    try:
                        # 直近の配当実績（配当落ち日）から月を抽出して「配当月」とする
                        recent_months = sorted(list(set(dividends.tail(4).index.month)))
                        div_months_str = " ".join([f"{m}月" for m in recent_months])
                    except:
                        pass
                
                # 時価総額の代わりに配当月・権利確定日を表示
                if ex_div_date:
                    col2.metric("配当月 (次回権利落ち)", f"{div_months_str} ({ex_div_date})")
                else:
                    col2.metric("配当実績月", div_months_str)
                
                trailing_pe = info.get('trailingPE')
                col3.metric("PER (株価収益率)", f"{trailing_pe:.2f} 倍" if trailing_pe else "N/A")
                
                div_rate = info.get('dividendRate')
                if div_rate and current_price and current_price != 'N/A' and float(current_price) > 0:
                    div_yield = div_rate / float(current_price)
                else:
                    div_yield = info.get('dividendYield')
                    if div_yield and div_yield >= 1.0:
                        div_yield = div_yield / 100

                col4.metric("配当利回り", f"{div_yield * 100:.2f}%" if div_yield is not None else "N/A")

                st.markdown("##### 📊 安定性・収益性指標")
                col5, col6, col7, col8 = st.columns(4)
                
                payout_ratio = info.get('payoutRatio')
                col5.metric("配当性向", f"{payout_ratio * 100:.2f}%" if payout_ratio is not None else "N/A")
                
                roe = info.get('returnOnEquity')
                col6.metric("ROE (自己資本利益率)", f"{roe * 100:.2f}%" if roe is not None else "N/A")
                
                debt_to_equity = info.get('debtToEquity')
                col7.metric("負債比率 (D/Eレシオ)", f"{debt_to_equity:.2f}%" if debt_to_equity is not None else "N/A")
                
                beta = info.get('beta')
                col8.metric("ベータ値 (価格変動リスク)", f"{beta:.2f}" if beta is not None else "N/A")

                st.markdown("##### 📈 成長性指標 (YoY: 前年比)")
                col9, col10, col11, col12 = st.columns(4)
                
                total_revenue = info.get('totalRevenue', 0)
                if total_revenue > 0:
                    if raw_currency == 'JPY':
                        rev_str = f"{total_revenue / 100000000:,.0f} 億円"
                    else:
                        rev_str = f"{total_revenue / 1000000000:,.1f} Billion {currency_str}"
                else:
                    rev_str = "N/A"
                col9.metric("売上高", rev_str)
                
                operating_margin = info.get('operatingMargins')
                col10.metric("営業利益率", f"{operating_margin * 100:.2f}%" if operating_margin is not None else "N/A")
                
                revenue_growth = info.get('revenueGrowth')
                col11.metric("売上高成長率 (YoY)", f"{revenue_growth * 100:.2f}%" if revenue_growth is not None else "N/A")
                
                earnings_growth = info.get('earningsGrowth')
                col12.metric("純利益成長率 (YoY)", f"{earnings_growth * 100:.2f}%" if earnings_growth is not None else "N/A")

                # =================================================
                # 過去の配当推移と累進配当（連続減配なし）の計算
                # =================================================
                st.markdown("##### 💰 過去の配当実績（年間推移）")
                dividends = get_yf_dividends(ticker_symbol)
                
                div_trend_str = "データなし"
                progressive_years = 0
                
                if not dividends.empty:
                    if dividends.index.tz is not None:
                        dividends.index = dividends.index.tz_localize(None)
                    
                    annual_div = dividends.groupby(dividends.index.year).sum()
                    current_year = pd.Timestamp.now().year
                    
                    last_years = annual_div[annual_div.index <= current_year].tail(6)
                    
                    if not last_years.empty:
                        years = last_years.index.tolist()
                        v_list = last_years.values
                        
                        history_list = []
                        for y, v in zip(years, v_list):
                            mark = "（暫定）" if y == current_year else ""
                            history_list.append(f"{y}年: {v:.1f}{currency_str}{mark}")
                        
                        div_trend_str = " → ".join(history_list[-5:])
                        
                        check_list = []
                        for y, v in zip(years, v_list):
                            if y != current_year:
                                check_list.append(v)
                            else:
                                if v >= (check_list[-1] if check_list else 0):
                                    check_list.append(v)
                        
                        for i in range(len(check_list)-1, 0, -1):
                            if check_list[i] >= check_list[i-1] * 0.98:
                                progressive_years += 1
                            else:
                                break
                        
                        if progressive_years > 0:
                            st.info(f"📈 **配当推移:** {div_trend_str}\n\n🔥 **実績:** 直近 **{progressive_years} 年間**、実質的な減配がない「累進配当（または連続増配）」の傾向があります！")
                        else:
                            st.write(f"📈 **配当推移:** {div_trend_str}")
                else:
                    st.write("配当の履歴データが取得できませんでした（無配株などの可能性があります）。")
                    
                st.divider()

                # ===== ルールベース判定（シグナル通知） =====
                st.subheader("🚦 個別銘柄ルールの判定")
                
                signals = []
                if div_yield is not None and div_yield >= 0.04 and payout_ratio is not None and payout_ratio < 0.6:
                    signals.append("🟢 **【高配当シグナル】** 配当利回り4%以上 ＆ 配当性向60%未満の「お宝高配当銘柄」の条件を満たしました。")
                if trailing_pe is not None and trailing_pe < 15 and roe is not None and roe >= 0.15:
                    signals.append("🟢 **【割安優良シグナル】** PER15倍未満 ＆ ROE15%以上の「割安優良銘柄」の条件を満たしました。")
                if beta is not None and beta < 0.8:
                    signals.append("🔵 **【安定シグナル】** ベータ値0.8未満の「価格変動が穏やかな安定銘柄」です。")
                if progressive_years >= 3:
                    signals.append(f"🔥 **【累進配当シグナル】** 直近{progressive_years}年間、実質的な減配がない継続的な株主還元銘柄です。")

                if signals:
                    st.success(f"🎊 {ticker_symbol} において、以下のシグナルが点灯しました！")
                    for sig in signals:
                        st.markdown(f"- {sig}")
                    
                    if webhook_url:
                        div_str = f"{div_yield * 100:.2f}%" if div_yield is not None else "N/A"
                        message_content = f"### 🔔 シグナル検出: {company_name} ({ticker_symbol})\n" + "\n".join([f"- {s}" for s in signals]) + f"\n\n👉 現在値: {current_price} {currency_str} | PER: {trailing_pe:.1f}倍 | 配当利回り: {div_str}"
                        payload = {"content": message_content}
                        req = urllib.request.Request(
                            webhook_url, data=json.dumps(payload).encode("utf-8"), 
                            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                        )
                        try:
                            urllib.request.urlopen(req)
                            st.info("✅ 検出されたシグナルをDiscordに自動送信しました！")
                        except Exception as e:
                            st.error(f"❌ Discordへの通知に失敗しました: {e}")
                else:
                    st.info("💡 現在のところ、設定された強めのルール（高配当、割安、累進配当など）に合致するシグナルは発生していません。")

                st.divider()

                # ===== Google News RSSによる日本語ニュース ======
                st.subheader("📰 直近の関連ニュース (日本語のみ厳選)")
                news_text_for_ai = ""
                
                try:
                    import xml.etree.ElementTree as ET
                    import urllib.parse
                    
                    search_term = ticker_symbol.split('.')[0]
                    # 日本語の社名も含めて検索することで精度を高める
                    query = urllib.parse.quote(f"{search_term} {company_name}")
                    rss_url = f"https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
                    req_news = urllib.request.Request(rss_url, headers={'User-Agent': 'Mozilla/5.0'})
                    
                    with urllib.request.urlopen(req_news) as response_news:
                        xml_data = response_news.read()
                    
                    root = ET.fromstring(xml_data)
                    items = root.findall('.//channel/item')
                    
                    if items:
                        # 有料購読が必要なメディアをブラックリスト化
                        banned_sources = ["日経", "日本経済新聞", "ブルームバーグ", "Bloomberg", "ウォール", "WSJ", "ダイヤモンド", "四季報", "NewsPicks", "朝日新聞", "毎日新聞", "読売新聞", "産経新聞"]
                        
                        valid_news_count = 0
                        for item in items:
                            news_title = item.find('title').text if item.find('title') is not None else 'タイトル不明'
                            news_link = item.find('link').text if item.find('link') is not None else '#'
                            source_name = item.find('source').text if item.find('source') is not None else ''
                            
                            # 有料メディアはスキップ
                            if any(banned in news_title or banned in source_name for banned in banned_sources):
                                continue
                                
                            st.markdown(f"- [{news_title}]({news_link})")
                            news_text_for_ai += f"・{news_title}\n"
                            valid_news_count += 1
                            
                            if valid_news_count >= 3:
                                break
                                
                        if valid_news_count == 0:
                            st.write("無料で閲覧できる関連ニュースが見つかりませんでした。")
                    else:
                        st.write("関連する日本語ニュースが見つかりませんでした。")
                except Exception as e:
                    st.warning(f"ニュースの取得に失敗しました: {e}")
                    news_text_for_ai = "ニュース取得に失敗しました"

                st.divider()

                st.subheader("🤖 AI分析レポート (長期投資家視点)")
                if api_key:
                    with st.spinner("AIが業界平均を解析し、財務データとニュースを読み解いています..."):
                        client = openai.OpenAI(api_key=api_key)
                        prompt = f"""
                        あなたはウォーレン・バフェットのような優秀な長期投資家です。
                        以下の企業指標、ニュース、過去の配当推移をもとに、この銘柄を長期保有する視点で分析してください。

                        【企業基本情報】
                        企業名: {company_name}
                        セクター(業種): {sector_jp} ({raw_sector})
                        
                        【財務指標】
                        PER: {trailing_pe:.1f}倍
                        ROE: {f"{roe * 100:.2f}%" if roe is not None else "N/A"}
                        営業利益率: {f"{operating_margin * 100:.2f}%" if operating_margin is not None else "N/A"}
                        
                        【成長性・安全性】
                        売上高: {rev_str}
                        売上高成長率(前年比): {f"{revenue_growth * 100:.2f}%" if revenue_growth is not None else "N/A"}
                        純利益成長率(前年比): {f"{earnings_growth * 100:.2f}%" if earnings_growth is not None else "N/A"}
                        配当性向: {f"{payout_ratio * 100:.2f}%" if payout_ratio is not None else "N/A"}
                        負債比率 (D/E): {f"{debt_to_equity:.2f}%" if debt_to_equity is not None else "N/A"}
                        ベータ値: {f"{beta:.2f}" if beta is not None else "N/A"}
                        
                        【配当実績】
                        推移: {div_trend_str}
                        → 連続減配なし（累進配当実績）: {progressive_years}年間

                        【直近のニュース】
                        {news_text_for_ai}
                        
                        ---
                        以下の構成でマークダウン形式で簡潔にレポートを作成してください。
                        
                        ### 📊 1. 業界平均との比較評価（最重要）
                        この銘柄が属する「{sector_jp}業界」の一般的な平均PER・ROE水準（あなたの知識に基づく大体の数値）を明記し、本銘柄の数値がそれと比べて「割安か割高か」「収益性は優秀か劣後しているか」を必ず解説してください。
                        
                        ### ✅ 2. 長期保有における強み (Strengths)
                        ### ⚠️ 3. 留意すべきリスク (Risks)
                        ### 🎁 4. 株主優待情報 (Shareholder Benefits)
                        この企業（特に日本株の場合）の株主優待の有無や内容について、あなたの知識データベースから簡潔に紹介してください。すでに廃止されている有名な優待（JTやオリックスなど）はその旨を必ず警告してください。※米国株など優待文化がない場合は「なし」として過度な説明は省き、最後に「※優待内容は変更・廃止される可能性があるため最新の公式HPを必ずご確認ください」と免責を添えてください。
                        
                        ### 💡 5. 総合評価 (Conclusion)
                        """
                        response = client.chat.completions.create(
                            model="gpt-4o-mini",
                            messages=[
                                {"role": "system", "content": "あなたは優秀な株式アナリスト兼長期投資家です。業界ごとの基準の違い（銀行業はPERが低い、ITは高い等）を必ず考慮に入れ、業界水準と比較しながら分析してください。"},
                                {"role": "user", "content": prompt}
                            ],
                            max_tokens=800
                        )
                        st.write(response.choices[0].message.content)
                else:
                    st.info("💡 OpenAI APIキーが未入力のため、以下は「AIが出力するレポートのサンプル」を表示しています。")
                    st.markdown(f"**【APIキーを設定すると、ここに業界平均を考慮した本物の分析レポートが表示されます！】**")

            except Exception as e:
                st.error(f"データの取得に失敗しました。銘柄コードが正しいか確認してください。（エラー詳細: {e}）")

# ==========================================
# タブ2: 監視リスト一括スキャン
# ==========================================
with tab_batch:
    st.markdown("登録した複数銘柄の現在の状況を一括でスキャンし、**「設定した利回りを超えた」「高値から急落した」**などの条件に合致した優良銘柄やチャンス銘柄のみを抽出・Discord通知します。")
    
    watch_list_str = st.text_area("📝 監視するティッカーリスト（カンマ区切り、または改行で入力）", value="7203.T, 8058.T, 8316.T, 2914.T, AAPL, MSFT, NVDA", height=100)
    
    st.markdown("#### ⚙️ 発動するアラートの条件設定")
    colA, colB = st.columns(2)
    alert_drop_pct = colA.number_input("⚠️【高値下落アラート】52週高値から何%下落したら「暴落」と通知する？", value=15.0, step=1.0)
    alert_yield_pct = colB.number_input("💰【お宝利回りアラート】配当利回りが何%以上になったら「高配当化」と通知する？", value=4.0, step=0.1)
    
    if st.button("🚀 ウォッチリストを一括スキャン", type="primary"):
        # カンマか改行で区切られたティッカーをリスト化
        raw_tickers = watch_list_str.replace('\\n', ',').split(',')
        tickers_to_check = []
        for t in raw_tickers:
            t = t.strip().upper()
            if t:
                if t.isdigit() and len(t) == 4:
                    t += ".T"
                tickers_to_check.append(t)
        
        if not tickers_to_check:
            st.warning("監視する銘柄コードを入力してください。")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()
            st.divider()
            
            all_alerts = [] # 発動したアラートを全員分貯めるリスト
            result_container = st.container()
            
            for i, t in enumerate(tickers_to_check):
                status_text.text(f"スキャン中... ({i+1}/{len(tickers_to_check)}): {t}")
                progress_bar.progress((i + 1) / len(tickers_to_check))
                time.sleep(0.5) # API過負荷防止のウェイト
                
                try:
                    info = get_yf_info(t)
                    current_price = info.get('currentPrice')
                    high_52 = info.get('fiftyTwoWeekHigh')
                    
                    raw_currency = info.get('currency', '')
                    currency_str = "円" if raw_currency == "JPY" else raw_currency
                    
                    raw_name = info.get('shortName', t)
                    company_name, _ = get_jp_company_info(t, raw_name, "")
                    
                    # 1. 下落率の計算
                    drop_pct = 0
                    if current_price and high_52 and high_52 > 0:
                        drop_pct = (high_52 - current_price) / high_52
                    
                    # 2. 配当利回りの計算
                    div_rate = info.get('dividendRate')
                    if div_rate and current_price and float(current_price) > 0:
                        div_yield = div_rate / float(current_price)
                    else:
                        div_yield = info.get('dividendYield', 0)
                        if div_yield and div_yield >= 1.0:
                            div_yield = div_yield / 100
                    div_yield = div_yield if div_yield else 0
                    
                    # アラート判定
                    stock_alerts = []
                    if drop_pct * 100 >= alert_drop_pct:
                        stock_alerts.append(f"⚠️ **高値から急落!!** (52週高値 {high_52} → 現在 {current_price} : **{drop_pct*100:.1f}%下落**)")
                    
                    if div_yield * 100 >= alert_yield_pct:
                        stock_alerts.append(f"💰 **お宝高配当化!!** (現在の配当利回り: **{div_yield*100:.2f}%**)")
                    
                    # ルールに引っかかったらリストに追加し、画面にも表示
                    if stock_alerts:
                        alert_detail = f"### 🏢 {company_name} ({t})\n💰現在値: {current_price} {currency_str}\n" + "\n".join([f"- {a}" for a in stock_alerts])
                        all_alerts.append(alert_detail)
                        with result_container:
                            st.error(alert_detail)
                        
                except Exception as e:
                    all_alerts.append(f"### 🏢 {t}\n- データの取得に失敗しました ({e})")
                    with result_container:
                        st.warning(f"⚠️ {t} のデータ取得に失敗しました。")
            
            status_text.text(f"スキャン完了！ (対象: {len(tickers_to_check)}銘柄)")
            
            st.divider()
            if all_alerts:
                st.success(f"🚨 スキャンの結果、**{len(all_alerts)}銘柄** でアラート（下落買い時・高配当化）が発動しました！")
                combined_message = "### 🔔 監視リスト 定期スキャン報告\nお気に入り銘柄の中で、指定された条件に到達した銘柄があります！\n\n" + "\n\n".join(all_alerts)
                
                # Discord通知
                if webhook_url:
                    payload = {"content": combined_message}
                    req = urllib.request.Request(
                        webhook_url, 
                        data=json.dumps(payload).encode("utf-8"), 
                        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                    )
                    try:
                        with urllib.request.urlopen(req) as response:
                            st.info("✅ 上記のアラート内容をまとめてDiscordに自動送信しました！")
                    except Exception as e:
                        st.error(f"❌ Discord一括通知に失敗しました: {e}")
            else:
                st.success("✅ スキャン完了。現在、アラート条件に合致する「異常な値動き」を記録した銘柄はありませんでした。")

# ==========================================
# タブ3: マイ・ポートフォリオ管理 (DB連携)
# ==========================================
with tab_portfolio:
    st.markdown("### 💼 マイ・ポートフォリオ管理 (Googleスプレッドシート連携)")
    st.write("ご自身の保有銘柄をGoogleスプレッドシート（無料データベース）に安全に記録し、クラウド上で永続化する機能です。")
    
    try:
        from streamlit_gsheets import GSheetsConnection
        
        if "connections" in st.secrets and "gsheets" in st.secrets.connections:
            conn = st.connection("gsheets", type=GSheetsConnection)
            
            # --- 3つのサブタブを作成 ---
            port_tab1, port_tab2, port_tab3 = st.tabs(["💼 1. 実際の保有銘柄", "🔭 2. 長期保有の監視", "🎁 3. 株主優待の狙い目"])
            
            # ==================================
            # サブタブ1: 実際の保有銘柄
            # ==================================
            with port_tab1:
                ws_name = "シート1"
                try:
                    df = conn.read(worksheet="シート1", ttl=0)
                except:
                    try:
                        df = conn.read(worksheet="Sheet1", ttl=0)
                        ws_name = "Sheet1"
                    except Exception as inner_e:
                        raise Exception(f"シート名が見つかりません。「シート1」または「Sheet1」が存在するか確認してください。({inner_e})")
                
                if df is not None and not df.empty and len(df.columns) > 0:
                    st.info("💡 行を選択して「Delete」キーを押すと削除できます。数値を直接クリックして書き換えることも可能です。")
                    edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, key="de_held")
                    if st.button("🔄 表の変更をクラウドに保存", type="primary", key="btn_held"):
                        conn.update(worksheet=ws_name, data=edited_df)
                        st.success("データベースの変更を保存しました！")
                        st.rerun()
                else:
                    st.info("データが登録されていません。（1行目に「保有銘柄」「企業名」「保有株数」「取得単価」という見出しを作成してください）")
                    
                st.divider()
                st.markdown("#### ➕ 新規銘柄の追加 / 記録")
                with st.form("add_stock_form"):
                    col1, col2, col3 = st.columns(3)
                    with col1: new_ticker = st.text_input("銘柄コード (例: 7203)")
                    with col2: new_shares = st.number_input("保有株数", min_value=1, step=1)
                    with col3: new_cost = st.number_input("平均取得単価", min_value=0.0, step=10.0)
                    
                    submitted = st.form_submit_button("💼 データベースに登録する")
                    if submitted:
                        if new_ticker:
                            t_sym = new_ticker.strip().upper()
                            if t_sym.isdigit() and len(t_sym) == 4:
                                t_sym += ".T"
                                
                            with st.spinner("企業名を取得中..."):
                                try:
                                    info_data = get_yf_info(t_sym)
                                    raw_n = info_data.get('shortName', t_sym)
                                    company_name, _ = get_jp_company_info(t_sym, raw_n, '')
                                except Exception:
                                    company_name = "エラー(取得不可)"
                                    
                            new_data = pd.DataFrame([{
                                "保有銘柄": t_sym,
                                "企業名": company_name,
                                "保有株数": new_shares,
                                "取得単価": new_cost
                            }])
                            updated_df = pd.concat([df, new_data], ignore_index=True) if df is not None and not df.empty else new_data
                            
                            conn.update(worksheet=ws_name, data=updated_df)
                            st.success(f"{t_sym} を登録しました！")
                            st.rerun()
                        else:
                            st.error("銘柄コードを入力してください。")

            # ==================================
            # サブタブ2: 長期保有の監視
            # ==================================
            with port_tab2:
                ws_name_long = "長期保有"
                df_long = None
                try:
                    df_long = conn.read(worksheet=ws_name_long, ttl=0)
                except Exception:
                    st.warning("⚠️ Googleスプレッドシートに「長期保有」という名前のシートが見つかりません。シートを追加してください。")
                
                if df_long is not None:
                    if not df_long.empty and len(df_long.columns) > 0:
                        edited_df_long = st.data_editor(df_long, num_rows="dynamic", use_container_width=True, key="de_long")
                        if st.button("🔄 リストの変更を保存", type="primary", key="btn_long"):
                            conn.update(worksheet=ws_name_long, data=edited_df_long)
                            st.success("保存しました！")
                            st.rerun()
                    else:
                        st.info("データがありません。（1行目に「銘柄コード」「企業名」「現在の株価」「検討理由」「目標株価」と見出しを作成してください）")
                        
                    st.divider()
                    st.markdown("#### 🔭 監視リストへの追加")
                    with st.form("add_long_form"):
                        c1, c2, c3 = st.columns(3)
                        with c1: t_long = st.text_input("銘柄コード (例: 8058)")
                        with c2: reason = st.text_input("検討理由 (例: 業績改善等)")
                        with c3: price_target = st.number_input("目標購入株価", min_value=0.0)
                        
                        submitted_long = st.form_submit_button("🔭 監視リストに登録する")
                        if submitted_long:
                            if t_long:
                                t_sym = t_long.strip().upper()
                                if t_sym.isdigit() and len(t_sym) == 4:
                                    t_sym += ".T"
                                    
                                with st.spinner("情報取得中..."):
                                    try:
                                        info_data = get_yf_info(t_sym)
                                        raw_n = info_data.get('shortName', t_sym)
                                        company_name, _ = get_jp_company_info(t_sym, raw_n, '')
                                        current_price = info_data.get('currentPrice', 0)
                                    except Exception:
                                        company_name = "エラー"
                                        current_price = 0
                                        
                                new_data = pd.DataFrame([{
                                    "銘柄コード": t_sym,
                                    "企業名": company_name,
                                    "現在の株価": current_price,
                                    "検討理由": reason,
                                    "目標株価": price_target
                                }])
                                updated_df = pd.concat([df_long, new_data], ignore_index=True) if not df_long.empty else new_data
                                conn.update(worksheet=ws_name_long, data=updated_df)
                                st.success(f"{t_sym} を登録しました！")
                                st.rerun()
                            else:
                                st.error("入力してください。")

            # ==================================
            # サブタブ3: 株主優待の狙い目
            # ==================================
            with port_tab3:
                ws_name_perk = "株主優待"
                df_perk = None
                try:
                    df_perk = conn.read(worksheet=ws_name_perk, ttl=0)
                except Exception:
                    st.warning("⚠️ Googleスプレッドシートに「株主優待」という名前のシートが見つかりません。シートを追加してください。")
                
                if df_perk is not None:
                    if not df_perk.empty and len(df_perk.columns) > 0:
                        edited_df_perk = st.data_editor(df_perk, num_rows="dynamic", use_container_width=True, key="de_perk")
                        if st.button("🔄 リストの変更を保存", type="primary", key="btn_perk"):
                            conn.update(worksheet=ws_name_perk, data=edited_df_perk)
                            st.success("保存しました！")
                            st.rerun()
                    else:
                        st.info("データがありません。（1行目に「銘柄コード」「企業名」「優待内容」「権利確定月」「最低保有株数」と見出しを作成してください）")
                        
                    st.divider()
                    st.markdown("#### 🎁 優待リストへの追加")
                    with st.form("add_perk_form"):
                        c1, c2, c3, c4 = st.columns(4)
                        with c1: t_perk = st.text_input("銘柄コード")
                        with c2: perk_desc = st.text_input("優待内容 (例: お米2kg)")
                        with c3: month = st.text_input("権利確定月 (例: 3月/9月)")
                        with c4: min_shares = st.number_input("最低保有株数", min_value=1, value=100)
                        
                        submitted_perk = st.form_submit_button("🎁 優待リストに登録する")
                        if submitted_perk:
                            if t_perk:
                                t_sym = t_perk.strip().upper()
                                if t_sym.isdigit() and len(t_sym) == 4:
                                    t_sym += ".T"
                                    
                                with st.spinner("情報取得中..."):
                                    try:
                                        info_data = get_yf_info(t_sym)
                                        raw_n = info_data.get('shortName', t_sym)
                                        company_name, _ = get_jp_company_info(t_sym, raw_n, '')
                                    except Exception:
                                        company_name = "エラー"
                                        
                                new_data = pd.DataFrame([{
                                    "銘柄コード": t_sym,
                                    "企業名": company_name,
                                    "優待内容": perk_desc,
                                    "権利確定月": month,
                                    "最低保有株数": min_shares
                                }])
                                updated_df = pd.concat([df_perk, new_data], ignore_index=True) if not df_perk.empty else new_data
                                conn.update(worksheet=ws_name_perk, data=updated_df)
                                st.success(f"{t_sym} を登録しました！")
                                st.rerun()
                            else:
                                st.error("入力してください。")
        else:
            st.warning("💡 現在、Googleスプレッドシートとの接続用のカギ（Secrets APIキー）が設定されていません。\n\nAIからのガイドに沿って連携用キーを取得し、完了させましょう！")
            
    except Exception as e:
        st.error(f"データベース接続に失敗しました。ライブラリの不足、または接続設定を確認してください: {e}")
