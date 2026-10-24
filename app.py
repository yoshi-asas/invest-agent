import difflib
import hmac
import json
import os
import time

import altair as alt
import openai
import pandas as pd
import streamlit as st

import invest_core as core
import invest_store

st.set_page_config(page_title="AI投資エージェント", layout="wide", page_icon="📈")

# ==========================================
# 🔒 アプリ全体のパスワードロック認証
# ==========================================
MAX_FAILS = 5           # 連続でこの回数まちがえたらロック
BASE_LOCK_SECONDS = 60  # ロック時間（以降は2倍ずつ、最大15分）


@st.cache_resource
def _login_guard():
    """全セッション共通の失敗カウンタ（総当たり対策）"""
    return {"fails": 0, "locked_until": 0.0}


def _secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def _expected_password():
    """(パスワード, 既定値の0000か) を返す。未設定なら (None, False)"""
    pw = _secret("APP_PASSWORD")
    if pw:
        return str(pw), False
    if os.environ.get("APP_PASSWORD_ALLOW_DEFAULT") == "1":   # ローカル開発用の明示的な許可
        return "0000", True
    return None, False


def check_password():
    if st.session_state.get("password_correct"):
        return True
    st.markdown("<h2 style='text-align: center; margin-top: 80px;'>🔒 秘密の投資エージェント</h2>", unsafe_allow_html=True)
    expected, is_default = _expected_password()
    if expected is None:
        st.error("APP_PASSWORD が設定されていないため、起動できません。"
                 "`.streamlit/secrets.toml`（公開環境では Streamlit の Secrets）の先頭に `APP_PASSWORD = \"好きなパスワード\"` を追加してください。")
        return False
    guard = _login_guard()
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.info("このアプリは個人用にアクセス制限されています。パスワードを入力してください。")
        if is_default:
            st.warning("⚠️ 既定のパスワード(0000)で動作中です。公開環境では使わないでください。")
        remaining = guard["locked_until"] - time.time()
        with st.form("login_form"):
            pw = st.text_input("パスワード", type="password")
            submitted = st.form_submit_button("ログイン")
        if submitted:
            if remaining > 0:
                st.error(f"試行回数が多すぎます。あと{int(remaining) + 1}秒待ってからもう一度お試しください。")
            elif hmac.compare_digest(pw.encode("utf-8"), expected.encode("utf-8")):
                guard["fails"] = 0
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                guard["fails"] += 1
                if guard["fails"] >= MAX_FAILS:
                    lock = min(BASE_LOCK_SECONDS * 2 ** (guard["fails"] - MAX_FAILS), 900)
                    guard["locked_until"] = time.time() + lock
                    st.error(f"😕 パスワードが間違っています。{lock}秒間ロックします。")
                else:
                    st.error(f"😕 パスワードが間違っています。（あと{MAX_FAILS - guard['fails']}回でロック）")
        elif remaining > 0:
            st.warning(f"試行回数が多すぎたため、あと{int(remaining) + 1}秒ロック中です。")
    return False


if not check_password():
    st.stop()


# ==========================================
# データ取得（キャッシュ付き）・保存先
# ==========================================
@st.cache_data(ttl=3600, show_spinner=False)
def get_yf_info(sym):
    return core.fetch_info(sym)


@st.cache_data(ttl=3600, show_spinner=False)
def get_yf_dividends(sym):
    return core.fetch_dividends(sym)


@st.cache_data(ttl=3600, show_spinner=False)
def get_yf_history(sym, period="1y"):
    return core.fetch_history(sym, period)


@st.cache_data(ttl=3600, show_spinner=False)
def get_jp_info(sym, name, sector):
    return core.get_jp_company_info(sym, name, sector)


@st.cache_data(ttl=900, show_spinner=False)
def get_news(sym, name):
    return core.fetch_news(sym, name)


FETCH = core.Fetchers(info=get_yf_info, dividends=get_yf_dividends, history=get_yf_history,
                      jp_info=get_jp_info, news=get_news)


@st.cache_resource(show_spinner=False)
def _connect_store():
    cfg = invest_store.load_gsheets_config(st.secrets)
    return invest_store.make_store(cfg) if cfg else None     # 接続に失敗した場合は例外（キャッシュされない）


def get_store():
    try:
        return _connect_store(), None
    except Exception as e:
        return None, str(e)


STORE, STORE_ERROR = get_store()
FAILED = object()      # safe_store が失敗したときの目印


def safe_store(action, *args, **kwargs):
    """ストアの操作を実行し、失敗したら画面にエラーを出して FAILED を返す"""
    try:
        return action(*args, **kwargs)
    except Exception as e:
        st.error(f"スプレッドシートの操作に失敗しました: {e}")
        return FAILED


def no_store_notice():
    if STORE_ERROR:
        st.error(f"Googleスプレッドシートに接続できませんでした: {STORE_ERROR}")
    else:
        st.warning("💡 現在、Googleスプレッドシートとの接続用のカギ（Secrets APIキー）が設定されていません。")


def lookup_company_name(sym):
    """銘柄コードから企業名を取得する（日本株は日本語名）"""
    try:
        info = FETCH.info(sym)
        if core.is_unknown_ticker(info):
            return "エラー(取得不可)"
        return FETCH.jp_info(sym, info.get("shortName", sym), "")[0]
    except Exception:
        return "エラー(取得不可)"


def flash(kind, msg):
    st.session_state.setdefault("flash", []).append((kind, msg))


# ==========================================
# 画面部品
# ==========================================
LEVEL_MD = {"good": ":green[● 良好]", "neutral": ":gray[● 普通]", "caution": ":orange[● 注意]", "na": ":gray[● ―]", None: ""}


def render_metrics(specs, compact, per_row=4):
    """指標を表示する。compact=True はスマホ向けに1つの表にまとめる"""
    if compact:
        lines = ["| 指標 | 値 | 目安 |", "|---|---|---|"]
        lines += [f"| {s['label']} | **{s['value']}** | {LEVEL_MD[s['level']]} |" for s in specs]
        st.markdown("\n".join(lines))
        return
    for i in range(0, len(specs), per_row):
        cols = st.columns(per_row)
        for col, s in zip(cols, specs[i:i + per_row]):
            col.metric(s["label"], s["value"], help=s.get("help"))
            if s["level"]:
                col.markdown(LEVEL_MD[s["level"]])


def compact_months(months_str):
    """「6月 12月」→「6・12月」（指標カードで途中で切れないように短くする）"""
    parts = [p.rstrip("月") for p in str(months_str).split()]
    return "・".join(parts) + "月" if parts and all(p.isdigit() for p in parts) else months_str


TREND_SHORT = {"上昇": "📈 上昇", "下降": "📉 下降", "もみ合い": "📊 もみ合い"}


def metric_groups(a):
    """タブ1に表示する指標のグループ。各指標は {label, value, level, help}"""
    H = core.METRIC_HELP
    pe, tech = a["pe"], a["tech"]
    rsi = tech["rsi14"] if tech else None
    price_txt = f"{a['price']} {a['cur']}" if a["price"] else "N/A"
    ex = a["ex_div"].strftime("%m/%d") if a["ex_div"] else ""
    months = compact_months(a["div"]["months_str"])
    div_month = f"{months} ({ex})" if ex else months

    def m(label, value, key, raw, rated=True):
        return {"label": label, "value": value, "level": core.rate_metric(key, raw) if rated else None, "help": H.get(key)}

    de = a["de"]
    trend = core.trend_label(a["price"], tech["sma50"] if tech else None, tech["sma200"] if tech else None, a["cur"]).split(" (")[0]
    trend_level = "good" if "上昇" in trend else "caution" if "下降" in trend else "neutral" if "もみ合い" in trend else "na"
    trend_short = next((v for k, v in TREND_SHORT.items() if k in trend), "判定不能")
    return [
        ("", [
            m("現在の株価", price_txt, "price", None, rated=False),
            {"label": "配当月 (次回権利落ち)" if ex else "配当実績月", "value": div_month, "level": None, "help": H["div_month"]},
            m("PER (株価収益率)", f"{pe:.2f} 倍" if core.isnum(pe) else "N/A", "pe", pe),
            m("配当利回り", core.fmt_pct(a["div_yield"]), "div_yield", a["div_yield"]),
        ]),
        ("##### 📊 安定性・収益性指標", [
            m("配当性向", core.fmt_pct(a["payout"]), "payout", a["payout"]),
            m("ROE (自己資本利益率)", core.fmt_pct(a["roe"]), "roe", a["roe"]),
            m("負債比率 (D/Eレシオ)", f"{de:.2f}%" if core.isnum(de) else "N/A", "de", de),
            m("ベータ値 (価格変動リスク)", f"{a['beta']:.2f}" if core.isnum(a["beta"]) else "N/A", "beta", a["beta"]),
        ]),
        ("##### 📈 成長性指標 (YoY: 前年比)", [
            m("売上高", a["rev_str"], "revenue", None, rated=False),
            m("営業利益率", core.fmt_pct(a["op_margin"]), "op_margin", a["op_margin"]),
            m("売上高成長率 (YoY)", core.fmt_pct(a["rev_growth"]), "rev_growth", a["rev_growth"]),
            m("純利益成長率 (YoY)", core.fmt_pct(a["eps_growth"]), "eps_growth", a["eps_growth"]),
        ]),
        ("##### 🕯️ テクニカル指標", [
            m("RSI (14日)", f"{rsi:.1f}" if rsi is not None else "N/A", "rsi", rsi),
            m("直近1ヶ月の値動き", f"{tech['mo1_perf']:+.1f}%" if tech else "N/A", "mo1", None, rated=False),
            {"label": "トレンド", "value": trend_short, "level": trend_level, "help": H["trend"] + f"（現在の判定: {trend}）"},
        ]),
    ]


def show_chart(chart):
    try:
        st.altair_chart(chart, width="stretch")
    except TypeError:      # 古いStreamlit
        st.altair_chart(chart, use_container_width=True)


UP_COLOR, DOWN_COLOR = "#d9534f", "#2b7bb9"      # 日本式: 上昇=赤、下落=青


FIT = alt.AutoSizeParams(type="fit", contains="padding")     # 軸の目盛りも含めて、表示枠の幅に収める


def candlestick_charts(hist, tail=None):
    """ローソク足＋移動平均線のチャートと、出来高のチャートを返す（[価格, 出来高] または [価格]）。
    移動平均は全期間で計算してから表示範囲を切り出す。連結(vconcat)チャートは枠に収まらないため別々に描画する"""
    df = hist.copy()
    for n in (20, 50, 200):
        if len(df) >= n:
            df[f"SMA{n}"] = df["Close"].rolling(n).mean()
    if tail:
        df = df.tail(tail)
    idx_name = df.index.name or "index"
    df = df.reset_index().rename(columns={idx_name: "Date"})
    ma_cols = [c for c in ("SMA20", "SMA50", "SMA200") if c in df and df[c].notna().any()]
    bar_w = max(1.5, min(9.0, 700.0 / max(len(df), 1)))
    color = alt.condition("datum.Close >= datum.Open", alt.value(UP_COLOR), alt.value(DOWN_COLOR))
    tip = [alt.Tooltip("Date:T", title="日付"), alt.Tooltip("Open:Q", title="始値", format=",.1f"),
           alt.Tooltip("High:Q", title="高値", format=",.1f"), alt.Tooltip("Low:Q", title="安値", format=",.1f"),
           alt.Tooltip("Close:Q", title="終値", format=",.1f"), alt.Tooltip("Volume:Q", title="出来高", format=",.0f")]
    x = alt.X("Date:T", axis=alt.Axis(title=None))
    no_zero = alt.Scale(zero=False)
    base = alt.Chart(df).encode(x=x)
    layers = [
        base.mark_rule().encode(y=alt.Y("Low:Q", scale=no_zero, title=None, axis=alt.Axis(minExtent=55)), y2="High:Q", color=color),
        base.mark_bar(size=bar_w).encode(y=alt.Y("Open:Q", scale=no_zero, title=None), y2="Close:Q", color=color, tooltip=tip),
    ]
    if ma_cols:
        palette = {"SMA20": "#f0ad4e", "SMA50": "#8e44ad", "SMA200": "#555555"}
        layers.append(alt.Chart(df).transform_fold(ma_cols, as_=["移動平均", "value"]).mark_line(strokeWidth=1.5).encode(
            x=x, y=alt.Y("value:Q", scale=no_zero, title=None),
            color=alt.Color("移動平均:N", scale=alt.Scale(domain=ma_cols, range=[palette[c] for c in ma_cols]),
                            legend=alt.Legend(title=None, orient="top"))))
    price = alt.layer(*layers).properties(height=340, width="container", autosize=FIT)
    charts = [price]
    if "Volume" in df and (df["Volume"].fillna(0) > 0).any():
        vol = alt.Chart(df).mark_bar(size=bar_w).encode(x=x, y=alt.Y("Volume:Q", title="出来高", axis=alt.Axis(minExtent=55)),
                                                           color=color).properties(height=110, width="container", autosize=FIT)
        charts.append(vol)
    return charts


def fmt_fetched(iso):
    try:
        return pd.Timestamp(iso).strftime("%Y-%m-%d %H:%M") + " (JST)"
    except Exception:
        return "不明"


def style_pct_change(styler, cols):
    """前日比などの色づけ（日本式: 上昇=赤、下落=青）"""
    def fn(v):
        if isinstance(v, (int, float)) and v > 0:
            return "color: #d9534f"
        if isinstance(v, (int, float)) and v < 0:
            return "color: #2b7bb9"
        return ""
    apply = getattr(styler, "map", None) or styler.applymap
    return apply(fn, subset=[c for c in cols if c in styler.data.columns])


def show_table(data, **kwargs):
    try:
        st.dataframe(data, width="stretch", hide_index=True, **kwargs)
    except TypeError:
        st.dataframe(data, use_container_width=True, hide_index=True, **kwargs)


# ==========================================
# ヘッダー・サイドバー
# ==========================================
st.title("📈 株式 長期保有アシスタント")
st.markdown("個別銘柄のAI深掘り分析と、**「監視リストの一括スキャン」**による自動アラート、保有株の損益管理ができるダッシュボードです。")

for _kind, _msg in st.session_state.pop("flash", []):
    getattr(st, _kind)(_msg)

st.sidebar.markdown("**🤖 AI連携設定**")
_typed_key = st.sidebar.text_input("OpenAI APIキー (お持ちの場合のみ入力)", type="password")
_secret_key = _secret("OPENAI_API_KEY")
api_key = _typed_key or _secret_key
if _secret_key and not _typed_key:
    st.sidebar.caption("🔑 Secretsに登録されたキーを使用中（入力すると、そちらを優先します）")

st.sidebar.divider()
st.sidebar.markdown("**🔔 通知設定 (Discord)**")
_typed_hook = st.sidebar.text_input("Discord Webhook URL", type="password")
_secret_hook = _secret("DISCORD_WEBHOOK_URL")
webhook_url = _typed_hook or _secret_hook
if _secret_hook and not _typed_hook:
    st.sidebar.caption("🔑 Secretsに登録されたURLを使用中")
elif not webhook_url:
    st.sidebar.caption("👉 Webhook URLを設定すると、シグナルや一括スキャンの結果をDiscordに送信できます。")

st.sidebar.divider()
compact = st.sidebar.toggle("📱 コンパクト表示（スマホ向け）", key="compact_view",
                            help="指標を1つの表にまとめて表示します。画面が狭いときにおすすめです。")
if st.sidebar.button("🔄 データを再取得（キャッシュをクリア）", help="株価などは1時間キャッシュされます。最新の値を取り直したいときに押します。"):
    st.cache_data.clear()
    if STORE:
        STORE.clear_cache()
    flash("success", "キャッシュをクリアしました。次の操作から最新のデータを取得します。")
    st.rerun()
st.sidebar.divider()
st.sidebar.caption("※本アプリは投資判断の参考情報を提供するものであり、投資勧誘を目的とするものではありません。最終的な投資決定はご自身の判断でお願いいたします。")

tab_single, tab_watch, tab_pf, tab_discover, tab_hist = st.tabs(
    ["🔍 1. 個別分析", "📋 2. 注目銘柄＆スキャン", "💼 3. 保有株", "🚀 4. テンバガー発掘", "📜 5. 履歴・レポート"])


# ==========================================
# タブ1: 個別銘柄の深掘り分析
# ==========================================
def run_single_analysis(raw_code):
    """分析を実行して結果を返す（AIレポート・シグナル通知を含む）。失敗したら例外"""
    sym = core.normalize_ticker(raw_code)
    if not sym:
        raise ValueError("銘柄コードを入力してください")
    a = core.collect_analysis(sym, FETCH)
    signals = core.judge_signals(a["div_yield"], a["payout"], a["pe"], a["roe"], a["beta"], a["div"]["progressive_years"])
    res = {"a": a, "signals": signals, "ai": None, "notices": [], "saved": False, "analyzed_at": core.now_jst()}

    if signals and webhook_url:
        pe_str = f"{a['pe']:.1f}倍" if core.isnum(a["pe"]) else "N/A"
        msg = (f"### 🔔 シグナル検出: {a['company_name']} ({sym})\n" + "\n".join(f"- {s}" for s in signals)
               + f"\n\n👉 現在値: {a['price']} {a['cur']} | PER: {pe_str} | 配当利回り: {core.fmt_pct(a['div_yield'])}")
        try:
            core.send_discord(webhook_url, msg)
            res["notices"].append(("info", "✅ 検出されたシグナルをDiscordに自動送信しました！"))
        except Exception as e:
            res["notices"].append(("error", f"❌ Discordへの通知に失敗しました: {e}"))

    if api_key:
        try:
            res["ai"] = core.generate_report(openai.OpenAI(api_key=api_key), a)
        except Exception as e:
            res["notices"].append(("error", f"AIレポートの作成に失敗しました。APIキーや残高をご確認ください。（{e}）"))
    return res


def render_single(res):
    a = res["a"]
    sym = a["symbol"]
    st.subheader(f"🏢 企業情報: {a['company_name']} ({a['sector_jp']})")
    st.caption(f"📡 データ取得: {fmt_fetched(a['fetched_at'])} ／ 出典: Yahoo Finance (yfinance)。株価は最大20分程度遅れることがあり、"
               "財務指標は決算発表後に更新されます。")
    for note in a["notes"]:
        st.warning(note)

    for title, specs in metric_groups(a):
        if title:
            st.markdown(title)
        render_metrics(specs, compact)
    if not compact:
        st.caption("● の色は一般的な目安による評価です（緑=良好 / 灰=普通 / 橙=注意）。各指標の ⓘ にマウスを合わせると基準を確認できます。業種によって適正な水準は異なります。")

    st.markdown("##### 💰 過去の配当実績（年間推移）")
    d = a["div"]
    if d["trend_str"] == "データなし":
        st.write("配当の履歴データが取得できませんでした（無配株などの可能性があります）。")
    elif d["progressive_years"] > 0:
        st.info(f"📈 **配当推移:** {d['trend_str']}\n\n🔥 **実績:** 直近 **{d['progressive_years']} 年間**、実質的な減配がない「累進配当（または連続増配）」の傾向があります！")
    else:
        st.write(f"📈 **配当推移:** {d['trend_str']}")
    st.divider()

    st.subheader("🚦 個別銘柄ルールの判定")
    if res["signals"]:
        st.success(f"🎊 {sym} において、以下のシグナルが点灯しました！")
        for s in res["signals"]:
            st.markdown(f"- {s}")
    else:
        st.info("💡 現在のところ、設定された強めのルール（高配当、割安、累進配当など）に合致するシグナルは発生していません。")
    for kind, msg in res["notices"]:
        getattr(st, kind)(msg)
    st.divider()

    st.subheader(f"📊 {a['company_name']} の株価チャート")
    periods = {"3ヶ月": 63, "6ヶ月": 126, "1年": 252, "5年": None}
    label = st.radio("チャート期間", list(periods), index=1, horizontal=True, key="chart_period")
    hist = get_yf_history(sym, "5y") if label == "5年" else a["hist"]
    if hist is None or hist.empty:
        st.write("チャートデータが取得できませんでした。")
    else:
        for chart in candlestick_charts(hist, tail=periods[label]):
            show_chart(chart)
        st.caption("ローソク足：赤=上昇、青=下落／線：橙=20日、紫=50日、灰=200日移動平均")
    st.divider()

    st.subheader("📰 直近の関連ニュース (日本語のみ厳選)")
    if a["news"]:
        for n in a["news"]:
            st.markdown(f"- [{n['title']}]({n['link']})")
    elif a["news_error"]:
        st.warning(f"ニュースの取得に失敗しました: {a['news_error']}")
    else:
        st.write("無料で閲覧できる関連ニュースが見つかりませんでした。")
    st.divider()

    st.subheader("🤖 AI分析レポート (長期投資家視点)")
    if res["ai"]:
        st.write(res["ai"])
        if STORE is not None:
            if res["saved"]:
                st.success("✅ このレポートは保存済みです（「5. 履歴・レポート」タブで過去のレポートと比較できます）。")
            elif st.button("💾 このレポートを保存", key="save_report_btn"):
                if safe_store(STORE.save_report, sym, a["company_name"], a["price"], res["ai"], res["analyzed_at"]) is not FAILED:
                    res["saved"] = True
                    st.rerun()
    elif not api_key:
        st.info("💡 OpenAI APIキーが未入力のため、AIレポートは作成していません。サイドバーにAPIキーを入力して、もう一度「個別データを取得して分析」を押してください。")


with tab_single:
    st.markdown("企業の基本情報や配当推移を取得し、**業界平均との比較**や**長期投資の視点**でAIが詳細な分析レポートを作成します。")
    ticker_input = st.text_input("🎯 深掘りする銘柄コード", value="2914")
    st.caption("例: 2914 (JT), AAPL (Apple), 7203 (トヨタ), 8058 (三菱商事)")
    if st.button("個別データを取得して分析", type="primary"):
        try:
            with st.spinner(f"{core.normalize_ticker(ticker_input) or ticker_input} のリアルタイムデータを取得中..."):
                st.session_state["single"] = run_single_analysis(ticker_input)
            st.session_state.pop("single_error", None)
        except Exception as e:
            st.session_state["single"] = None
            st.session_state["single_error"] = str(e)
    if st.session_state.get("single_error"):
        st.error(f"データの取得に失敗しました。銘柄コードが正しいか確認してください。（エラー詳細: {st.session_state['single_error']}）")
    elif st.session_state.get("single"):
        render_single(st.session_state["single"])


# ==========================================
# タブ2: 注目銘柄リスト ＆ 自動スキャン
# ==========================================
SETTING_FIELDS = [  # (key, ラベル, step, 型)
    ("drop_pct", "⚠️【高値下落】52週高値から何%下落したら「暴落」？", 1.0, float),
    ("yield_pct", "💰【高配当】配当利回りが何%以上になったら「高配当化」？", 0.1, float),
    ("target_pct", "🎯【目標株価】アナリスト目標から何%割安なら通知する？", 1.0, float),
    ("days_before", "📅【イベント接近】決算や配当権利日の何日前に入ったら通知する？", 1, int),
    ("rsi", "📉【RSI大底】RSI(14日)がいくつ以下になったら大底判定とする？", 1.0, float),
    ("payout_pct", "🚨【減配リスク】配当性向が何%を超えたら警告する？", 1.0, float),
    ("renotify_days", "🔕【再通知】同じアラートを何日ごとに再通知する？（新規のみ通知する場合）", 1, int),
]


def render_overview(df, sort_col, ascending):
    if sort_col in df:
        df = df.sort_values(sort_col, ascending=ascending, na_position="last")
    fmt = {"現在値": "{:,.1f}", "前日比(%)": "{:+.2f}", "配当利回り(%)": "{:.2f}", "PER": "{:.1f}", "ROE(%)": "{:.1f}",
           "配当性向(%)": "{:.1f}", "高値から(%)": "{:.1f}", "RSI": "{:.1f}", "非減配(年)": "{:.0f}"}
    styler = df.style.format({k: v for k, v in fmt.items() if k in df.columns}, na_rep="―")
    show_table(style_pct_change(styler, ["前日比(%)", "高値から(%)"]))


with tab_watch:
    st.markdown("### 📋 注目銘柄リスト (Googleスプレッドシート連携)")
    st.write("気になる銘柄とその理由を登録・管理し、一覧での比較やボタン一つでの一括スキャン（暴落・高配当化など）ができます。")
    if STORE is None:
        no_store_notice()
    else:
        wl = safe_store(STORE.load_watchlist)
        if wl is FAILED:
            wl = pd.DataFrame(columns=["銘柄コード", "企業名", "注目理由"])
        st.info("💡 表の下の「＋」で行を追加、行を選択して「Delete」キーで削除、セルをクリックして書き換えができます。変更後は必ず「リストの変更を保存」を押してください。")
        edited_df = st.data_editor(wl[["銘柄コード", "企業名", "注目理由"]], num_rows="dynamic", width="stretch", key="de_watch")
        if st.button("🔄 リストの変更を保存", type="primary", key="btn_save_watch"):
            saved_df = invest_store.merge_watchlist_edit(wl, edited_df)
            if saved_df.empty:
                st.warning("登録銘柄が0件になるため保存しませんでした。全件を削除する場合はスプレッドシートを直接編集してください。")
            else:
                blank = saved_df["企業名"].isna() | (saved_df["企業名"].astype(str).str.strip() == "")
                if blank.any():
                    with st.spinner("企業名を取得中..."):
                        for i in saved_df.index[blank]:
                            saved_df.at[i, "企業名"] = lookup_company_name(saved_df.at[i, "銘柄コード"])
                if safe_store(STORE.save_watchlist, saved_df) is not FAILED:
                    flash("success", "データベースの変更を保存しました！")
                    st.rerun()

        st.divider()
        st.markdown("#### ➕ 注目銘柄の追加")
        with st.form("add_watch_form"):
            new_ticker = st.text_input("銘柄コード (例: 7203)")
            new_reason = st.text_input("注目理由 (任意)")
            if st.form_submit_button("📋 リストに追加"):
                if new_ticker:
                    t_sym = core.normalize_ticker(new_ticker)
                    registered = wl["銘柄コード"].map(core.normalize_ticker).tolist() if not wl.empty else []
                    if t_sym in registered:
                        st.warning(f"{t_sym} は既に登録されています。")
                    else:
                        with st.spinner("企業名を取得中..."):
                            company_name = lookup_company_name(t_sym)
                        new_row = pd.DataFrame([{"銘柄コード": t_sym, "企業名": company_name, "注目理由": new_reason}])
                        updated = pd.concat([wl, new_row], ignore_index=True) if not wl.empty else new_row
                        if safe_store(STORE.save_watchlist, updated) is not FAILED:
                            flash("success", f"{t_sym} を登録しました！")
                            st.rerun()
                else:
                    st.error("銘柄コードを入力してください。")

        watch_codes = list(dict.fromkeys(t for t in (core.normalize_ticker(c) for c in wl["銘柄コード"].dropna().tolist()) if t))

        # ---------- 一覧・絞り込み・比較 ----------
        st.divider()
        with st.expander("📊 一覧・絞り込み・比較（登録銘柄の最新状況をまとめて見る）", expanded=False):
            with_div = st.checkbox("配当の履歴も取得して「非減配(年)」を計算する（銘柄数が多いと時間がかかります）", value=False, key="ov_div")
            if st.button("📥 最新の状況を取得して一覧表示", key="btn_overview"):
                if not watch_codes:
                    st.warning("登録銘柄がありません。")
                else:
                    ov_rows, bar = [], st.progress(0)
                    for i, t in enumerate(watch_codes):
                        bar.progress((i + 1) / len(watch_codes), text=f"取得中... ({i + 1}/{len(watch_codes)}): {t}")
                        try:
                            info = FETCH.info(t)
                            name = FETCH.jp_info(t, info.get("shortName", t), "")[0]
                            ov_rows.append(core.overview_row(t, name, info, FETCH.history(t, "1y"), FETCH.dividends(t) if with_div else None))
                        except Exception as e:
                            ov_rows.append({"銘柄コード": t, "企業名": f"取得失敗 ({e})"})
                    bar.empty()
                    st.session_state["overview"] = ov_rows
            ov_rows = st.session_state.get("overview")
            if ov_rows:
                full = pd.DataFrame(ov_rows)
                st.markdown("**絞り込み条件**（0 のままの項目は条件に使いません。値が取得できない銘柄は、条件を指定した項目では除外されます）")
                c1, c2, c3 = st.columns(3)
                min_yield = c1.number_input("配当利回り(%)が以上", min_value=0.0, value=0.0, step=0.5, key="f_yield")
                max_pe = c2.number_input("PER(倍)が以下", min_value=0.0, value=0.0, step=1.0, key="f_pe")
                min_roe = c3.number_input("ROE(%)が以上", min_value=0.0, value=0.0, step=1.0, key="f_roe")
                c4, c5, c6 = st.columns(3)
                max_payout = c4.number_input("配当性向(%)が以下", min_value=0.0, value=0.0, step=5.0, key="f_payout")
                min_prog = c5.number_input("非減配の年数が以上", min_value=0, value=0, step=1, key="f_prog")
                max_rsi = c6.number_input("RSIが以下", min_value=0.0, value=0.0, step=5.0, key="f_rsi")
                filtered = core.filter_overview(
                    full, min_yield=min_yield or None, max_pe=max_pe or None, min_roe=min_roe or None,
                    max_payout=max_payout or None, min_prog=min_prog or None, max_rsi=max_rsi or None)
                sc1, sc2 = st.columns([2, 1])
                sort_col = sc1.selectbox("並べ替え", [c for c in full.columns if c not in ("銘柄コード", "企業名", "通貨")], key="ov_sort")
                asc = sc2.radio("順序", ["降順", "昇順"], horizontal=True, key="ov_order") == "昇順"
                st.caption(f"{len(filtered)} / {len(full)} 銘柄を表示（前日比・高値からの変化は 赤=上昇 / 青=下落）")
                render_overview(filtered, sort_col, asc)

                st.markdown("**📈 銘柄の比較**")
                options = full["銘柄コード"].tolist()
                picked = st.multiselect("比較する銘柄（2〜6銘柄）", options, default=options[:3], max_selections=6, key="cmp_pick")
                period_label = st.radio("期間", ["6ヶ月", "1年", "5年"], index=1, horizontal=True, key="cmp_period")
                if len(picked) >= 2:
                    period = {"6ヶ月": "6mo", "1年": "1y", "5年": "5y"}[period_label]
                    norm = core.normalize_prices({t: FETCH.history(t, period) for t in picked})
                    if norm.empty:
                        st.write("比較できる株価データがありませんでした。")
                    else:
                        long = norm.reset_index().rename(columns={norm.index.name or "index": "Date"}).melt(
                            "Date", var_name="銘柄", value_name="指数").dropna()
                        show_chart(alt.Chart(long).mark_line().encode(
                            x=alt.X("Date:T", axis=alt.Axis(title=None)),
                            y=alt.Y("指数:Q", scale=alt.Scale(zero=False), title="期間初日=100"),
                            color=alt.Color("銘柄:N", legend=alt.Legend(title=None, orient="top")),
                            tooltip=["Date:T", "銘柄:N", alt.Tooltip("指数:Q", format=".1f")]).properties(height=320, width="container", autosize=FIT))
                    cmp_df = full[full["銘柄コード"].isin(picked)].set_index("銘柄コード").T
                    cmp_df = cmp_df.apply(lambda col: col.map(lambda v: "―" if pd.isna(v) else (f"{v:,.2f}" if isinstance(v, float) else str(v))))
                    try:
                        st.dataframe(cmp_df, width="stretch")
                    except TypeError:
                        st.dataframe(cmp_df, use_container_width=True)
                else:
                    st.caption("2銘柄以上を選ぶと、株価の推移（期間初日=100）と指標を並べて比較できます。")

        # ---------- スキャン ----------
        st.divider()
        st.markdown("#### 🚀 ウォッチリストを一括スキャン")
        st.markdown("登録された銘柄に異常がないか確認し、Discordへ通知します。条件は保存でき、定期スキャン（GitHub Actions / auto_scan.py）でも同じ条件が使われます。")
        saved_settings = safe_store(STORE.load_settings)
        if saved_settings is FAILED:
            saved_settings = dict(core.SCAN_DEFAULTS)
        cur_settings = {}
        sc_cols = st.columns(2)
        for i, (key, label, step, typ) in enumerate(SETTING_FIELDS):
            with sc_cols[i % 2]:
                cur_settings[key] = st.number_input(label, value=typ(saved_settings[key]), step=typ(step), key=f"set_{key}")
        b1, b2 = st.columns([1, 1])
        if b1.button("💾 この条件を保存", key="btn_save_settings"):
            if safe_store(STORE.save_settings, {k: cur_settings[k] for k, *_ in SETTING_FIELDS}) is not FAILED:
                flash("success", "スキャン条件を保存しました（定期スキャンにも反映されます）。")
                st.rerun()
        dedupe = st.checkbox("🔕 同じアラートは再通知しない（新しく発生したものだけDiscordに通知）", value=True, key="chk_dedupe")

        if b2.button("🚀 登録銘柄を一斉スキャン", type="primary", key="btn_scan"):
            if not watch_codes:
                st.warning("スキャン対象の銘柄がリストにありません。")
            else:
                progress_bar, status_text = st.progress(0), st.empty()
                st.divider()
                result_container = st.container()

                def on_progress(i, n, res):
                    status_text.text(f"スキャン中... ({i + 1}/{n}): {res['symbol']}")
                    progress_bar.progress((i + 1) / n)
                    with result_container:
                        if res["error"]:
                            st.warning(f"⚠️ {res['symbol']} のデータ取得に失敗しました。")
                        elif res["alerts"]:
                            st.error(core.format_alert_block(res))

                results = core.scan_tickers(
                    watch_codes, cur_settings, FETCH.info, FETCH.history,
                    name_fn=lambda t, info: FETCH.jp_info(t, info.get("shortName", t), "")[0], progress=on_progress, pause=0.5)
                status_text.text(f"スキャン完了！ (対象: {len(watch_codes)}銘柄)")
                st.divider()

                alerted = [r for r in results if r["alerts"]]
                failed = [r for r in results if r["error"]]
                now = core.now_jst()
                state_df = safe_store(STORE.load_state, True)
                new_items, new_state = core.filter_new_alerts(
                    results, None if state_df is FAILED else state_df, now.date(), cur_settings["renotify_days"])

                if alerted:
                    st.success(f"🚨 スキャンの結果、**{len(alerted)}銘柄** でアラートが発動しました！")
                else:
                    st.success("✅ スキャン完了。現在、アラート条件に合致する銘柄はありませんでした。")

                notified_ok = True
                if webhook_url and alerted:
                    message = core.format_scan_message(results, "### 🔔 監視リスト 定期スキャン報告", only=new_items if dedupe else None)
                    if message is None:
                        st.info("ℹ️ 新しく発生したアラートはありませんでした（通知済みのアラートは再送しません）。")
                    else:
                        try:
                            n = core.send_discord(webhook_url, message)
                            st.info(f"✅ Discordに自動送信しました！（{n}通）")
                        except Exception as e:
                            notified_ok = False
                            st.error(f"❌ Discord自動通知に失敗しました: {e}")
                if failed:
                    st.warning(f"⚠️ {len(failed)}銘柄はデータを取得できませんでした（アラート件数には含めていません）: "
                               + "、".join(f"{r['symbol']} ({r['error']})" for r in failed))
                if notified_ok:      # 通知に失敗したときは「通知済み」にしない（次回また通知される）
                    safe_store(STORE.save_state, new_state)
                hist_rows = core.history_rows(results, new_items, "app", now)
                if hist_rows and safe_store(STORE.append_history, hist_rows) is not FAILED:
                    st.caption(f"📜 新しく発生した{len(hist_rows)}件のアラートを履歴に記録しました（「5. 履歴・レポート」タブで確認できます）。")


# ==========================================
# タブ3: 保有株（ポートフォリオ）
# ==========================================
with tab_pf:
    st.markdown("### 💼 保有株の損益・配当管理 (Googleスプレッドシート連携)")
    st.write("保有している銘柄の株数と取得単価を登録すると、評価額・損益・予想配当を自動計算します。")
    if STORE is None:
        no_store_notice()
    else:
        pf = safe_store(STORE.load_portfolio)
        if pf is FAILED:
            pf = pd.DataFrame(columns=core.PORTFOLIO_COLUMNS)
        pf_view = pf[core.PORTFOLIO_COLUMNS].copy()
        for c in ("株数", "取得単価"):
            pf_view[c] = pd.to_numeric(pf_view[c], errors="coerce")
        st.info("💡 表の下の「＋」で行を追加します。取得単価は1株あたりの購入価格（日本株は円、米国株はドル）です。変更後は「保有株を保存」を押してください。")
        pf_edit = st.data_editor(
            pf_view, num_rows="dynamic", width="stretch", key="de_pf",
            column_config={"株数": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
                           "取得単価": st.column_config.NumberColumn(min_value=0, format="%.2f")})
        if st.button("💾 保有株を保存", type="primary", key="btn_save_pf"):
            to_save = pf_edit.copy()
            to_save["銘柄コード"] = to_save["銘柄コード"].apply(core.normalize_ticker)
            to_save = to_save[to_save["銘柄コード"] != ""].reset_index(drop=True)
            bad = to_save[pd.to_numeric(to_save["株数"], errors="coerce").fillna(0) <= 0]
            if len(bad):
                st.warning("株数が入力されていない行があります: " + "、".join(bad["銘柄コード"]))
            else:
                blank = to_save["企業名"].isna() | (to_save["企業名"].astype(str).str.strip() == "")
                if blank.any():
                    with st.spinner("企業名を取得中..."):
                        for i in to_save.index[blank]:
                            to_save.at[i, "企業名"] = lookup_company_name(to_save.at[i, "銘柄コード"])
                if safe_store(STORE.save_portfolio, to_save) is not FAILED:
                    st.session_state.pop("pf_result", None)
                    flash("success", "保有株を保存しました。")
                    st.rerun()

        st.divider()
        if st.button("📊 最新の株価で評価を更新", key="btn_pf_eval"):
            held = pf_view[pd.to_numeric(pf_view["株数"], errors="coerce").fillna(0) > 0]
            if held.empty:
                st.warning("保有株が登録されていません。")
            else:
                infos, bar = {}, st.progress(0)
                syms = list(dict.fromkeys(core.normalize_ticker(c) for c in held["銘柄コード"] if core.normalize_ticker(c)))
                for i, s in enumerate(syms):
                    bar.progress((i + 1) / len(syms), text=f"株価を取得中... ({i + 1}/{len(syms)}): {s}")
                    try:
                        infos[s] = FETCH.info(s)
                    except Exception:
                        infos[s] = {}
                bar.empty()
                fx = None
                if any(i.get("currency") == "USD" for i in infos.values()):
                    fxh = FETCH.history("JPY=X", "5d")
                    fx = float(fxh["Close"].iloc[-1]) if fxh is not None and len(fxh) else None
                detail, totals = core.calc_portfolio(held, infos, fx)
                st.session_state["pf_result"] = {"detail": detail, "totals": totals, "fx": fx, "at": core.now_jst()}
        r = st.session_state.get("pf_result")
        if r is not None and not r["detail"].empty:
            d, t, fx = r["detail"], r["totals"], r["fx"]
            st.caption(f"📡 評価時点: {r['at'].strftime('%Y-%m-%d %H:%M')} (JST) ／ 出典: Yahoo Finance"
                       + (f" ／ 米ドル換算: 1ドル={fx:,.2f}円" if fx else ""))
            if (d["通貨"] == "USD").any() and not fx:
                st.warning("為替レートを取得できなかったため、米国株は円換算の合計に含めていません。")
            if (~d["通貨"].isin(["JPY", "USD"])).any():
                st.warning("日本円・米ドル以外の通貨の銘柄は、円換算の合計に含めていません。")
            jpy_cost, jpy_val = d["取得額(円換算)"].sum(min_count=1), d["評価額(円換算)"].sum(min_count=1)
            jpy_pnl, jpy_div = d["損益(円換算)"].sum(min_count=1), d["予想年間配当(円換算)"].sum(min_count=1)
            k = st.columns(4)
            k[0].metric("評価額（円換算）", f"{jpy_val:,.0f} 円" if pd.notna(jpy_val) else "N/A")
            k[1].metric("損益（円換算）", f"{jpy_pnl:+,.0f} 円" if pd.notna(jpy_pnl) else "N/A",
                        delta=f"{jpy_pnl / jpy_cost * 100:+.1f}%" if pd.notna(jpy_pnl) and pd.notna(jpy_cost) and jpy_cost else None)
            k[2].metric("予想年間配当（円換算）", f"{jpy_div:,.0f} 円" if pd.notna(jpy_div) else "N/A")
            k[3].metric("配当利回り（評価額ベース）", f"{jpy_div / jpy_val * 100:.2f}%" if pd.notna(jpy_div) and pd.notna(jpy_val) and jpy_val else "N/A",
                        help="予想年間配当 ÷ 評価額。配当は会社予想・過去実績に基づく目安です。")
            st.markdown("**通貨別の合計**")
            show_table(t.style.format({**{c: "{:,.0f}" for c in ("取得額", "評価額", "損益", "予想年間配当")},
                                       "損益率(%)": "{:+.1f}", "配当利回り(評価額ベース,%)": "{:.2f}"}, na_rep="―"))
            st.markdown("**銘柄ごとの明細**")
            show = d.drop(columns=["取得額(円換算)", "損益(円換算)", "予想年間配当(円換算)"])
            sty = show.style.format({"株数": "{:,.0f}", "取得単価": "{:,.2f}", "現在値": "{:,.2f}", "取得額": "{:,.0f}", "評価額": "{:,.0f}",
                                     "損益": "{:+,.0f}", "損益率(%)": "{:+.1f}", "予想年間配当": "{:,.0f}", "取得ベース利回り(%)": "{:.2f}",
                                     "現在利回り(%)": "{:.2f}", "評価額(円換算)": "{:,.0f}"}, na_rep="―")
            show_table(style_pct_change(sty, ["損益", "損益率(%)"]))
            if d["評価額(円換算)"].notna().any():
                g1, g2 = st.columns(2)
                pie = d.dropna(subset=["評価額(円換算)"])
                with g1:
                    st.markdown("**評価額の構成**")
                    show_chart(alt.Chart(pie).mark_arc(innerRadius=50).encode(
                        theta=alt.Theta("評価額(円換算):Q"), color=alt.Color("企業名:N", legend=alt.Legend(title=None, orient="bottom")),
                        tooltip=["企業名:N", alt.Tooltip("評価額(円換算):Q", format=",.0f")]).properties(height=300))
                with g2:
                    st.markdown("**予想年間配当（円換算）**")
                    divs = d[d["予想年間配当(円換算)"].fillna(0) > 0]
                    if divs.empty:
                        st.write("配当が見込める銘柄がありません。")
                    else:
                        show_chart(alt.Chart(divs).mark_bar().encode(
                            x=alt.X("予想年間配当(円換算):Q", title=None), y=alt.Y("企業名:N", sort="-x", title=None),
                            tooltip=["企業名:N", alt.Tooltip("予想年間配当(円換算):Q", format=",.0f")]).properties(height=300))


# ==========================================
# タブ4: テンバガー発掘エージェント
# ==========================================
PASTE_SAMPLE = """# 宇宙
9348 ispace
RKLB Rocket Lab
# 量子コンピューター
IONQ IonQ"""


def render_candidates(theme_name, reason, valid, rejected, unavailable=()):
    """1つのテーマの審査結果を表示する"""
    st.markdown(f"#### 🌐 トレンド: **{theme_name}**")
    if reason:
        st.write(f"**【選定理由】** {reason}")
    if valid:
        st.markdown("##### 🌟 真のテンバガー候補 (フィルタ通過)")
        for col, cand in zip(st.columns(len(valid)), valid):
            with col:
                st.success(f"**{cand['name']}** ({cand['symbol']})")
                st.write(f"- 現在値: {cand['price']} {core.currency_label(cand['currency'])}")
                st.write(f"- 時価総額: {cand['mcap']}")
                st.write(f"- 売上成長率: {cand['growth']}")
                st.write(f"- 営業利益率: {cand['margin']}")
    else:
        st.warning("このテーマでは、yfinanceスクリーニングを通過したテンバガー候補は見つかりませんでした。")
    if rejected:
        st.markdown("##### ❌ 除外された推薦銘柄 (足切り)")
        cols = st.columns(min(len(rejected), 4))
        for i, cand in enumerate(rejected):
            with cols[i % len(cols)]:
                st.error(f"**{cand['name']}** ({cand['symbol']})")
                st.caption(f"除外理由: {cand['reason']}")
                st.caption(f"時価総額: {cand['mcap']}")
                st.caption(f"売上成長率: {cand['growth']}")
    if unavailable:
        st.caption("⚠️ 情報を取得できず、審査できなかった銘柄: " + "、".join(unavailable))
    st.markdown("---")


def screen_symbols(items, on_progress=None, pause=0.5):
    """[(コード, 企業名)] を審査して (通過, 除外, 審査できなかった銘柄の説明) を返す"""
    valid, rejected, unavailable = [], [], []
    for i, (sym, name) in enumerate(items):
        if on_progress:
            on_progress(i, len(items), sym, name)
        info = {}
        try:
            info = FETCH.info(sym)
            status, card = core.screen_tenbagger(sym, name or info.get("shortName", sym), info)
        except Exception as e:
            unavailable.append(f"{sym}（取得エラー: {type(e).__name__}）")
            continue
        if status == "valid":
            valid.append(card)
        elif status == "rejected":
            rejected.append(card)
        else:
            unavailable.append(f"{sym}（該当する銘柄が見つかりません）" if core.is_unknown_ticker(info) else f"{sym}（時価総額が不明）")
        if pause:
            time.sleep(pause)
    return valid, rejected, unavailable


with tab_discover:
    st.markdown("### 🚀 テンバガー発掘エージェント")
    st.write("将来大きく伸びそうな小型成長株の候補を、**時価総額（150億ドル以下）と売上成長率（プラス）**で審査します。"
             "候補は、AIに探させる（下段）か、他のAIや調べもので見つけたものを貼り付けて（上段）審査できます。")

    # ---------- ① 貼り付けて審査（APIキー不要） ----------
    st.markdown("#### 📋 候補を貼り付けて審査（OpenAI APIキー不要）")
    st.caption("Claude など好きなAIや、ご自身の調べもので見つけた候補を貼り付けます。1行に1銘柄（コード と 企業名）。"
               "「# テーマ名」の行を入れると、テーマごとにまとめて表示します。最大30銘柄（コードは `7203` でも `7203.T` でも、米国株は `PLTR` でも可）。")
    pasted = st.text_area("候補リスト", key="tb_paste", height=200, placeholder=PASTE_SAMPLE)
    if st.button("🔎 貼り付けた候補を審査する", type="primary", key="btn_tb_paste"):
        items, bad_lines, over = core.parse_candidates(pasted)
        if not items:
            st.session_state["tb_manual"] = None
            st.warning("読み取れる銘柄がありませんでした。1行に「コード 企業名」の形で入力してください。")
        else:
            bar = st.progress(0)
            valid_all, rejected_all, unavail_all = screen_symbols(
                [(it["symbol"], it["name"]) for it in items],
                on_progress=lambda i, n, sym, name: bar.progress((i + 1) / n, text=f"審査中... ({i + 1}/{n}): {sym}"))
            bar.empty()
            by_symbol = {c["symbol"]: c for c in valid_all + rejected_all}
            groups = {}
            for it in items:                      # テーマごとにまとめ直す（入力順）
                g = groups.setdefault(it["theme"], {"valid": [], "rejected": [], "unavailable": []})
                if it["symbol"] in {c["symbol"] for c in valid_all}:
                    g["valid"].append(by_symbol[it["symbol"]])
                elif it["symbol"] in {c["symbol"] for c in rejected_all}:
                    g["rejected"].append(by_symbol[it["symbol"]])
                else:
                    g["unavailable"].append(next(u for u in unavail_all if u.startswith(it["symbol"] + "（")))
            st.session_state["tb_manual"] = {"groups": groups, "bad": bad_lines, "over": over, "total": len(items),
                                              "at": core.now_jst()}
    manual = st.session_state.get("tb_manual")
    if manual:
        n_ok = sum(len(g["valid"]) for g in manual["groups"].values())
        st.success(f"審査完了: {manual['total']}銘柄中 {n_ok}銘柄が条件を通過しました（{manual['at'].strftime('%Y-%m-%d %H:%M')} 時点のデータ）。")
        if manual["bad"]:
            st.warning("読み取れなかった行: " + " ／ ".join(manual["bad"]))
        if manual["over"]:
            st.warning(f"上限（{core.MAX_CANDIDATES}銘柄）を超えた {manual['over']} 銘柄は審査していません。")
        for theme_name, g in manual["groups"].items():
            render_candidates(theme_name, "", g["valid"], g["rejected"], g["unavailable"])
        st.caption("※ 通過は「時価総額が大きすぎず、売上が伸びている」だけを意味します。赤字（営業利益率がマイナス）の企業も含まれるため、"
                   "財務の健全性・事業内容・株価の割高感は、必ずご自身で確認してください。")

    # ---------- ② AIに探させる（OpenAI APIキーが必要） ----------
    st.divider()
    st.markdown("#### 🤖 AIに候補を探させる（自律型AI探索・OpenAI APIキーが必要）")
    st.write("AI自身が「次に来る未来のメガトレンド」を予測し、関連する小型成長株をリストアップして審査します。")

    if not api_key:
        st.warning("💡 この機能を使用するには、サイドバーからOpenAI APIキーを入力してください。（上の「貼り付けて審査」はキー不要です）")
    else:
        st.info("⚠️ 注意: 日本株と米国株（小型株中心）を対象に探索を行います。yfinanceのデータ取得状況により、一部の銘柄がスキップされる場合があります。")

        if st.button("🤖 AI発掘エージェントを起動 (探索開始)", type="primary"):
            my_bar = st.progress(0, text="AIが世界のマクロ経済・技術動向から「今後10年で市場規模が数十倍になるメガトレンド3つ」を考案中...")
            try:
                client = openai.OpenAI(api_key=api_key)
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
                    model="gpt-4o-mini", messages=[{"role": "user", "content": ai_prompt}],
                    response_format={"type": "json_object"}, max_tokens=1500)
                my_bar.progress(30, text="AIがテーマと候補リストの生成を完了。yfinanceを通じた財務スクリーニングを開始します...")

                themes = json.loads(response.choices[0].message.content).get("themes", [])
                st.divider()
                st.subheader("💡 AIが予測した次世代メガトレンドと発掘銘柄")
                total = max(1, sum(len(th.get("tickers", [])) for th in themes))
                done = [0]

                def on_progress(i, n, sym, name):
                    done[0] += 1
                    my_bar.progress(min(100, 30 + int(70 * done[0] / total)), text=f"スクリーニング中: {sym} ({name})...")

                for theme in themes:
                    pairs = [(t.get("symbol", "").upper().strip(), t.get("company_name", "")) for t in theme.get("tickers", [])]
                    valid, rejected, _ = screen_symbols([p for p in pairs if p[0]], on_progress=on_progress)
                    render_candidates(theme.get("theme_name"), theme.get("reason"), valid, rejected)    # 取得不可の銘柄は表示しない
                my_bar.progress(100, text="発掘エージェントの処理が完了しました！")
                st.balloons()
            except Exception as e:
                st.error(f"AIエージェントの実行でエラーが発生しました。APIキーや通信状況をご確認ください。詳細: {e}")


# ==========================================
# タブ5: 履歴・レポート
# ==========================================
with tab_hist:
    st.markdown("### 📜 スキャン履歴・AIレポート")
    if STORE is None:
        no_store_notice()
    else:
        st.markdown("#### 🔔 アラートの履歴")
        hist_df = safe_store(STORE.load_history)
        if hist_df is FAILED or hist_df.empty:
            st.write("まだ履歴がありません。「2. 注目銘柄＆スキャン」で一括スキャンを実行すると、新しく発生したアラートが記録されます。")
        else:
            f1, f2 = st.columns(2)
            kind_name = lambda k: core.ALERT_NAMES.get(core.alert_type(str(k)), str(k))
            syms = f1.multiselect("銘柄で絞り込み", sorted(hist_df["銘柄コード"].unique()), key="hf_sym")
            types = f2.multiselect("種別で絞り込み", sorted({kind_name(k) for k in hist_df["アラート種別"]}), key="hf_type")
            view = hist_df.copy()
            view["種別"] = view["アラート種別"].map(kind_name)
            if syms:
                view = view[view["銘柄コード"].isin(syms)]
            if types:
                view = view[view["種別"].isin(types)]
            show_table(view[["日時", "銘柄コード", "企業名", "種別", "内容", "現在値", "実行元"]])
            st.caption(f"{len(view)} / {len(hist_df)} 件")

            st.markdown("##### 📈 アラート後の値動き（的中率の確認）")
            st.caption("アラートが出た時点の株価を基準に、5営業日後・20営業日後の株価がどう動いたかを集計します。まだ日数が経っていないアラートは集計に含まれません。")
            if st.button("📈 アラート後の値動きを評価する", key="btn_eval_hist"):
                with st.spinner("株価を取得して評価中..."):
                    ev = core.evaluate_alert_history(view, FETCH.history)
                    st.session_state["hist_eval"] = (ev, core.summarize_alert_history(ev))
            if st.session_state.get("hist_eval"):
                ev, summ = st.session_state["hist_eval"]
                if summ.empty:
                    st.write("評価できる履歴がありません。")
                else:
                    pct = {**{c: "{:+.1%}" for c in summ.columns if c.endswith("平均")}, **{c: "{:.0%}" for c in summ.columns if c.endswith("上昇率")}}
                    show_table(summ.style.format(pct, na_rep="―"))
                    st.caption("「上昇率」= アラート時より株価が上がっていた割合。件数が少ないうちは参考程度にしてください。")

        st.divider()
        st.markdown("#### 🧠 保存したAIレポート")
        reports = safe_store(STORE.load_reports)
        if reports is FAILED or reports.empty:
            st.write("まだ保存したレポートがありません。「1. 個別分析」でAIレポートを作成し、「💾 このレポートを保存」を押すと、ここで過去のレポートと比較できます。")
        else:
            sym_pick = st.selectbox("銘柄", sorted(reports["銘柄コード"].unique()), key="rep_sym")
            sub = reports[reports["銘柄コード"] == sym_pick].reset_index(drop=True)
            labels = [f"{r['日時']}（株価 {r['現在値']}）" for _, r in sub.iterrows()]
            if len(sub) == 1:
                st.caption("保存されているレポートは1件です。")
                st.markdown(sub.loc[0, "レポート"])
            else:
                c1, c2 = st.columns(2)
                new_i = c1.selectbox("新しいレポート", range(len(sub)), format_func=lambda i: labels[i], index=0, key="rep_new")
                old_i = c2.selectbox("比べるレポート", range(len(sub)), format_func=lambda i: labels[i], index=1, key="rep_old")
                left, right = st.columns(2)
                left.markdown(f"**{labels[old_i]}**")
                left.markdown(sub.loc[old_i, "レポート"])
                right.markdown(f"**{labels[new_i]}**")
                right.markdown(sub.loc[new_i, "レポート"])
                with st.expander("🔍 変更点だけを見る（- 前回 / + 今回）"):
                    diff = "\n".join(line for line in difflib.unified_diff(
                        str(sub.loc[old_i, "レポート"]).splitlines(), str(sub.loc[new_i, "レポート"]).splitlines(), lineterm="", n=0)
                        if not line.startswith(("---", "+++", "@@")))
                    st.code(diff or "変更はありません。", language="diff")
