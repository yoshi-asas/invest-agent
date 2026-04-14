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
        if st.session_state.get("password", "") == expected_password:
            st.session_state["password_correct"] = True
            st.session_state["password"] = ""
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
tab_single, tab_watch, tab_discover = st.tabs(["🔍 1. 個別分析", "📋 2. 注目銘柄＆スキャン", "🚀 3. テンバガー発掘エージェント"])

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
@st.cache_data(ttl=3600, show_spinner=False)
def get_yf_info(t_symbol):
    try:
        return yf.Ticker(t_symbol).info
    except Exception as e:
        raise e

@st.cache_data(ttl=3600, show_spinner=False)
def get_yf_dividends(t_symbol):
    try:
        return yf.Ticker(t_symbol).dividends
    except Exception:
        return pd.Series(dtype='float64')

@st.cache_data(ttl=3600, show_spinner=False)
def get_yf_history(t_symbol, period="6mo"):
    try:
        return yf.Ticker(t_symbol).history(period=period)
    except Exception:
        return pd.DataFrame()


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

                # ===== 株価チャートの表示 =====
                st.subheader(f"📊 {company_name} の株価チャート (過去6ヶ月)")
                hist_data = get_yf_history(ticker_symbol, period="6mo")
                if not hist_data.empty:
                    # tz-aware datetime index might cause issues in older streamlit versions, so we remove timezone if exists
                    if hist_data.index.tz is not None:
                        hist_data.index = hist_data.index.tz_localize(None)
                    st.line_chart(hist_data['Close'])
                else:
                    st.write("チャートデータが取得できませんでした。")

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
# タブ2: 注目銘柄リスト ＆ 自動スキャン
# ==========================================
with tab_watch:
    st.markdown("### 📋 注目銘柄リスト (Googleスプレッドシート連携)")
    st.write("気になる銘柄とその理由を登録・管理し、ボタン一つで異常（暴落・高配当化）がないか一括スキャンします。")
    
    try:
        from streamlit_gsheets import GSheetsConnection
        
        if "connections" in st.secrets and "gsheets" in st.secrets.connections:
            conn = st.connection("gsheets", type=GSheetsConnection)
            ws_name = "シート1"
            
            try:
                df = conn.read(worksheet="シート1", ttl=0)
            except:
                try:
                    df = conn.read(worksheet="Sheet1", ttl=0)
                    ws_name = "Sheet1"
                except Exception as inner_e:
                    raise Exception(f"シートが見つかりません。「シート1」が存在するか確認してください。({inner_e})")
            
            if df is not None:
                # 必要なカラムが存在しない場合は補完
                required_cols = ["銘柄コード", "企業名", "注目理由"]
                for col in required_cols:
                    if col not in df.columns:
                        df[col] = ""
                
                # 表示用にカラムを絞る
                display_df = df[required_cols]
                
                st.info("💡 行を選択して「Delete」キーを押すと削除できます。数値を直接クリックして書き換えることも可能です。")
                edited_df = st.data_editor(display_df, num_rows="dynamic", use_container_width=True, key="de_watch")
                
                if st.button("🔄 リストの変更を保存", type="primary", key="btn_save_watch"):
                    for col in edited_df.columns:
                        df[col] = edited_df[col]
                    conn.update(worksheet=ws_name, data=df)
                    st.success("データベースの変更を保存しました！")
                    st.rerun()
            else:
                st.info("データがありません。（1行目に「銘柄コード」「企業名」「注目理由」という見出しを作成してください）")
                df = pd.DataFrame(columns=["銘柄コード", "企業名", "注目理由"])
                
            st.divider()
            
            col_add, col_scan = st.columns(2)
            
            with col_add:
                st.markdown("#### ➕ 注目銘柄の追加")
                with st.form("add_watch_form"):
                    new_ticker = st.text_input("銘柄コード (例: 7203)")
                    new_reason = st.text_input("注目理由 (任意)")
                    
                    submitted = st.form_submit_button("📋 リリストに追加")
                    if submitted:
                        if new_ticker:
                            t_sym = new_ticker.strip().upper()
                            if t_sym.isdigit() and len(t_sym) == 4:
                                t_sym += ".T"
                                
                            with st.spinner("企業名を取得中..."):
                                try:
                                    info_data = get_yf_info(t_sym)
                                    raw_n = info_data.get('shortName', t_sym)
                                    company_name = raw_n
                                    try:
                                        company_name, _ = get_jp_company_info(t_sym, raw_n, '')
                                    except:
                                        pass
                                except Exception:
                                    company_name = "エラー(取得不可)"
                                    
                            new_data = pd.DataFrame([{
                                "銘柄コード": t_sym,
                                "企業名": company_name,
                                "注目理由": new_reason
                            }])
                            updated_df = pd.concat([df, new_data], ignore_index=True) if not df.empty else new_data
                            
                            conn.update(worksheet=ws_name, data=updated_df)
                            st.success(f"{t_sym} を登録しました！")
                            st.rerun()
                        else:
                            st.error("銘柄コードを入力してください。")
            
            with col_scan:
                st.markdown("#### 🚀 ウォッチリストを一括スキャン")
                st.markdown("スプレッドシートに登録された銘柄に異常がないか確認し、Discordへ通知します。")
                alert_drop_pct = st.number_input("⚠️【高値下落】52週高値から何%下落したら「暴落」？", value=15.0, step=1.0)
                alert_yield_pct = st.number_input("💰【高配当】配当利回りが何%以上になったら「高配当化」？", value=4.0, step=0.1)
                alert_target_pct = st.number_input("🎯【目標株価】アナリスト目標から何%割安なら通知する？", value=20.0, step=1.0)
                alert_days_before = st.number_input("📅【イベント接近】決算や配当権利日の何日前に入ったら通知する？", value=14, step=1)
                alert_rsi = st.number_input("📉【RSI大底】RSI(14日)がいくつ以下になったら大底判定とする？", value=30.0, step=1.0)
                alert_payout_pct = st.number_input("🚨【減配リスク】配当性向が何%を超えたら警告する？", value=80.0, step=1.0)
                if st.button("🚀 登録銘柄を一斉スキャン", type="primary"):
                    if not df.empty and "銘柄コード" in df.columns:
                        tickers_to_check = df["銘柄コード"].dropna().astype(str).tolist()
                        tickers_to_check = [t.strip() for t in tickers_to_check if t.strip() and t.strip() != "nan"]
                        
                        if not tickers_to_check:
                            st.warning("スキャン対象の銘柄がリストにありません。")
                        else:
                            progress_bar = st.progress(0)
                            status_text = st.empty()
                            st.divider()
                            
                            all_alerts = []
                            result_container = st.container()
                            
                            for i, t in enumerate(tickers_to_check):
                                status_text.text(f"スキャン中... ({i+1}/{len(tickers_to_check)}): {t}")
                                progress_bar.progress((i + 1) / len(tickers_to_check))
                                time.sleep(0.5)
                                
                                try:
                                    info = get_yf_info(t)
                                    current_price = info.get('currentPrice')
                                    high_52 = info.get('fiftyTwoWeekHigh')
                                    
                                    raw_currency = info.get('currency', '')
                                    currency_str = "円" if raw_currency == "JPY" else raw_currency
                                    
                                    raw_name = info.get('shortName', t)
                                    c_name = raw_name
                                    try:
                                        c_name, _ = get_jp_company_info(t, raw_name, "")
                                    except:
                                        pass
                                    
                                    drop_pct = 0
                                    if current_price and high_52 and high_52 > 0:
                                        drop_pct = (high_52 - current_price) / high_52
                                    
                                    div_rate = info.get('dividendRate')
                                    if div_rate and current_price and float(current_price) > 0:
                                        div_yield = div_rate / float(current_price)
                                    else:
                                        div_yield = info.get('dividendYield', 0)
                                        if div_yield and div_yield >= 1.0:
                                            div_yield = div_yield / 100
                                    div_yield = div_yield if div_yield else 0
                                    
                                    # --- テクニカル（移動平均線・RSI）判定 ---
                                    trend_str = "トレンド判定不能"
                                    sma_200 = None
                                    current_rsi = None
                                    hist_data = get_yf_history(t, period="1y")
                                    if not hist_data.empty and len(hist_data) > 50:
                                        hist_data['SMA50'] = hist_data['Close'].rolling(window=50).mean()
                                        hist_data['SMA200'] = hist_data['Close'].rolling(window=200).mean()
                                        
                                        # RSI(14)計算
                                        delta = hist_data['Close'].diff()
                                        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                                        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                                        rs = gain / loss
                                        hist_data['RSI'] = 100 - (100 / (1 + rs))
                                        
                                        sma_50 = hist_data['SMA50'].iloc[-1]
                                        sma_200 = hist_data['SMA200'].iloc[-1]
                                        current_rsi = hist_data['RSI'].iloc[-1]
                                        
                                        if pd.notna(sma_50) and pd.notna(sma_200) and current_price:
                                            if current_price > sma_200 and sma_50 > sma_200:
                                                trend_str = f"📈 上昇トレンド (200日線 {sma_200:,.1f} 円を上抜け)"
                                            elif current_price < sma_200:
                                                diff_pct = (current_price - sma_200) / sma_200 * 100
                                                trend_str = f"📉 下降・安値圏 (200日線 {sma_200:,.1f} 円より {diff_pct:.1f}%)"
                                            else:
                                                trend_str = f"📊 もみ合い (200日線 {sma_200:,.1f} 円付近)"
                                                
                                    import datetime
                                    now = datetime.datetime.now()
                                    
                                    # --- イベント日接近判定 ---
                                    event_alerts = []
                                    # 決算日
                                    e_timestamp = info.get('earningsTimestamp')
                                    if e_timestamp:
                                        e_date = datetime.datetime.fromtimestamp(e_timestamp)
                                        days_out = (e_date - now).days
                                        if 0 <= days_out <= alert_days_before:
                                            event_alerts.append(f"🗓️ **【決算目前】** {days_out}日後 ({e_date.strftime('%Y/%m/%d')}) に決算発表があります！")
                                    # 権利落ち日
                                    div_timestamp = info.get('exDividendDate')
                                    if div_timestamp:
                                        d_date = datetime.datetime.fromtimestamp(div_timestamp)
                                        days_out = (d_date - now).days
                                        if 0 <= days_out <= alert_days_before:
                                            event_alerts.append(f"🎁 **【権利日目前】** {days_out}日後 ({d_date.strftime('%Y/%m/%d')}) が配当落ち日です！")

                                    stock_alerts = []
                                    # 既存：高値下落＆高配当 化
                                    if drop_pct * 100 >= alert_drop_pct:
                                        stock_alerts.append(f"⚠️ **高値から急落!!** (52週高値 {high_52} → 現在 {current_price} : **{drop_pct*100:.1f}%下落**)")
                                    if div_yield * 100 >= alert_yield_pct:
                                        stock_alerts.append(f"💰 **お宝高配当化!!** (現在の配当利回り: **{div_yield*100:.2f}%**)")
                                        
                                    # 新規①：目標株価
                                    target_price = info.get('targetMeanPrice')
                                    if target_price and current_price and current_price > 0:
                                        target_diff = (target_price - current_price) / current_price * 100
                                        if target_diff >= alert_target_pct:
                                            stock_alerts.append(f"🎯 **超割安放置株!!** アナリスト平均目標株価({target_price})に対して現在値が約 **{target_diff:.1f}%も割安** です。")
                                            
                                    # 新規②：RSI大底検知
                                    if current_rsi and current_rsi <= alert_rsi:
                                        stock_alerts.append(f"📉 **【大底検知】** RSIが **{current_rsi:.1f}** まで低下しており、パニック的な売られすぎ水準にあります。")
                                        
                                    # 新規③：減配リスク警告
                                    payout_ratio = info.get('payoutRatio')
                                    if payout_ratio is not None and payout_ratio * 100 >= alert_payout_pct:
                                        stock_alerts.append(f"🚨 **【減配リスク警戒】** 配当性向が **{payout_ratio*100:.1f}%** と極めて高く、無理して配当を出しているため減配リスクがあります。")
                                        
                                    # イベント通知を追加
                                    stock_alerts.extend(event_alerts)
                                    
                                    if stock_alerts:
                                        alert_detail = f"### 🏢 {c_name} ({t})\n💰現在値: {current_price} {currency_str} | {trend_str}\n" + "\n".join([f"- {a}" for a in stock_alerts])
                                        all_alerts.append(alert_detail)
                                        with result_container:
                                            st.error(alert_detail)
                                    
                                    if stock_alerts:
                                        alert_detail = f"### 🏢 {c_name} ({t})\n💰現在値: {current_price} {currency_str} | {trend_str}\n" + "\n".join([f"- {a}" for a in stock_alerts])
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
                                st.success(f"🚨 スキャンの結果、**{len(all_alerts)}銘柄** でアラートが発動しました！")
                                import urllib.request, json
                                combined_message = "### 🔔 監視リスト 定期スキャン報告\n\n" + "\n\n".join(all_alerts)
                                
                                if webhook_url:
                                    payload = {"content": combined_message}
                                    req = urllib.request.Request(webhook_url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
                                    try:
                                        urllib.request.urlopen(req)
                                        st.info("✅ Discordに自動送信しました！")
                                    except Exception as e:
                                        st.error(f"❌ Discord自動通知に失敗しました: {e}")
                            else:
                                st.success("✅ スキャン完了。現在、アラート条件に合致する銘柄はありませんでした。")
                    else:
                        st.warning("スキャン対象の銘柄がリストにありません。")
                        
        else:
            st.warning("💡 現在、Googleスプレッドシートとの接続用のカギ（Secrets APIキー）が設定されていません。")
            
    except Exception as e:
        st.error(f"データベース機能でエラーが発生しました: {e}")

# ==========================================
# タブ3: テンバガー発掘エージェント
# ==========================================
with tab_discover:
    st.markdown("### 🚀 テンバガー発掘エージェント (自律型AI探索)")
    st.write("AI自身が「次に来る未来のメガトレンド」を予測し、関連する小型成長株をリストアップして財務（テンバガーの素質）を審査します。")
    
    if not api_key:
        st.warning("💡 この機能を使用するには、サイドバーからOpenAI APIキーを入力してください。")
    else:
        st.info("⚠️ 注意: 日本株と米国株（小型株中心）を対象に探索を行います。yfinanceのデータ取得状況により、一部の銘柄がスキップされる場合があります。")
        
        if st.button("🤖 AI発掘エージェントを起動 (探索開始)", type="primary"):
            progress_text = "AIが世界のマクロ経済・技術動向から「今後10年で市場規模が数十倍になるメガトレンド3つ」を考案中..."
            my_bar = st.progress(0, text=progress_text)
            
            try:
                client = openai.OpenAI(api_key=api_key)
                
                # STEP 1: AIテーマ＆銘柄生成
                ai_prompt = """
あなたは世界最高のベンチャーキャピタリスト・長期投資家です。
今後10年間で市場規模が爆発的（数十倍）に成長する可能性が高い「次世代メガトレンド」を3つ予測してください。
そして、その各メガトレンドに関連する「日米の上場企業で、まだ時価総額が比較的小さく（約2兆円=150億ドル以下が目安）、成長余地が大きいテンバガー候補銘柄」を3つずつ（計9銘柄）ピックアップしてください。日本株も必ず含めてください。

以下のJSON形式で必ず出力してください（他のテキストを含めないこと）。
{
  "themes": [
    {
      "theme_name": "テーマ名（例: 脳波インターフェース等の具体的な未来技術）",
      "reason": "なぜこのテーマが10年後に爆発的に成長するのか、簡潔で熱い解説（約100文字）",
      "tickers": [
        {
          "symbol": "ティッカーシンボル（米国株はそのまま、日本株は末尾に .T を付けること。例: 7203.T, PLTR）",
          "company_name": "企業名"
        }
      ]
    }
  ]
}
"""
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": ai_prompt}],
                    response_format={ "type": "json_object" },
                    max_tokens=1500
                )
                
                my_bar.progress(30, text="AIがテーマと候補リストの生成を完了。yfinanceを通じた財務スクリーニングを開始します...")
                
                import json
                ai_result = json.loads(response.choices[0].message.content)
                themes = ai_result.get("themes", [])
                
                st.divider()
                st.subheader("💡 AIが予測した次世代メガトレンドと発掘銘柄")
                
                total_tickers_to_check = sum([len(th.get("tickers", [])) for th in themes])
                checked_count = 0
                
                for theme in themes:
                    st.markdown(f"#### 🌐 トレンド: **{theme.get('theme_name')}**")
                    st.write(f"**【AIの選定理由】** {theme.get('reason')}")
                    
                    valid_candidates = []
                    
                    for raw_ticker in theme.get("tickers", []):
                        t_sym = raw_ticker.get("symbol", "").upper().strip()
                        c_name = raw_ticker.get("company_name", "")
                        
                        if t_sym:
                            checked_count += 1
                            my_bar.progress(30 + int(70 * (checked_count / total_tickers_to_check)), text=f"スクリーニング中: {t_sym} ({c_name})...")
                            
                            try:
                                info = get_yf_info(t_sym)
                                mcap = info.get('marketCap')
                                rev_growth = info.get('revenueGrowth')
                                current_price = info.get('currentPrice')
                                currency = info.get('currency', '')
                                
                                # yfinanceバグでデータが取れない場合もあるので緩和
                                if mcap is None:
                                    continue
                                
                                # 時価総額チェック (ドル換算で約15B以下をターゲット)
                                # 日本円の場合は150で割って概算ドルに
                                mcap_usd = mcap
                                if currency == "JPY":
                                    mcap_usd = mcap / 150
                                    mcap_str = f"{mcap / 100000000:,.0f}億円"
                                else:
                                    mcap_str = f"${mcap / 1000000000:,.1f} Billion"
                                
                                # 成長率チェック (直近がマイナスなら除外。nanの場合は不明として通す)
                                growth_str = "不明"
                                if rev_growth is not None:
                                    if rev_growth < 0:
                                        continue # 足切り（成長していない）
                                    growth_str = f"{rev_growth * 100:.1f}%"
                                    
                                # 時価総額 15 Billion USD 以上の大企業は除外 (足切り)
                                if mcap_usd > 15000000000:
                                    continue
                                
                                valid_candidates.append({
                                    "symbol": t_sym,
                                    "name": c_name,
                                    "price": current_price,
                                    "currency": currency,
                                    "mcap": mcap_str,
                                    "growth": growth_str
                                })
                                
                                time.sleep(0.5) # API制限回避
                            except Exception as e:
                                pass # エラーの銘柄はスキップ
                    
                    if valid_candidates:
                        cols = st.columns(len(valid_candidates))
                        for idx, cand in enumerate(valid_candidates):
                            with cols[idx]:
                                st.success(f"**{cand['name']}** ({cand['symbol']})")
                                cur_sym = "円" if cand['currency'] == "JPY" else cand['currency']
                                st.write(f"- 現在値: {cand['price']} {cur_sym}")
                                st.write(f"- 時価総額: {cand['mcap']}")
                                st.write(f"- 売上成長率: {cand['growth']}")
                    else:
                        st.warning("AIが推薦した銘柄はすべて「時価総額が大きすぎる」または「売上成長がマイナス」のため、yfinanceスクリーニングで足切りされました。真のテンバガー候補は見つかりませんでした。")
                    
                    st.markdown("---")
                
                my_bar.progress(100, text="発掘エージェントの処理が完了しました！")
                st.balloons()
                
            except Exception as e:
                st.error(f"AIエージェントの実行でエラーが発生しました。APIキーや通信状況をご確認ください。詳細: {e}")
