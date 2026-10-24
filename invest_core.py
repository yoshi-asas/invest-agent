"""投資エージェントの共通ロジック（Streamlit に依存しない）。

app.py（Webアプリ）・discord_bot.py（Bot）・auto_scan.py（定期スキャン）・テストから共通で使う。
データ取得・指標計算・アラート判定・Discord送信・AIプロンプトなどをここに集約している。
"""
import datetime
import json
import math
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

import pandas as pd
import yfinance as yf

JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst():
    return datetime.datetime.now(JST)


# ==========================================================
# 共通ユーティリティ
# ==========================================================
def isnum(v):
    """数値として使える値か（None・NaN・文字列を除く）"""
    if v is None or isinstance(v, (str, bool)):
        return False
    try:
        return not math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def fmt_pct(v, nd=2):
    """比率（0.04）を「4.00%」に。取得できなければ N/A"""
    return f"{v * 100:.{nd}f}%" if isnum(v) else "N/A"


def normalize_ticker(code):
    """銘柄コードを正規化する。東証コード（4桁数字 / 新コード 例:130A）は末尾に .T を付ける。空・NaNは空文字を返す"""
    if code is None:
        return ""
    code = str(code).strip().upper()
    if code in ("", "NAN", "NONE"):
        return ""
    if re.fullmatch(r"\d{4}|\d{3}[A-Z]", code):
        code += ".T"
    return code


def is_unknown_ticker(info):
    """yfinanceが銘柄情報を返さなかった（存在しない銘柄コード）場合にTrue"""
    return not info or (all(info.get(k) is None for k in ("currentPrice", "regularMarketPrice", "marketCap"))
                        and not info.get("shortName") and not info.get("longName"))


def is_rate_limit_error(e):
    s = str(e)
    return "Too Many Requests" in s or "429" in s or "Rate limited" in s


def calc_div_yield(info, current_price):
    """配当利回りを比率で返す（0.04 = 4%）。取得できなければ None"""
    div_rate = info.get("dividendRate")
    try:
        if div_rate and current_price and float(current_price) > 0:
            return div_rate / float(current_price)
    except (TypeError, ValueError):
        pass
    y = info.get("dividendYield")
    return None if y is None else y / 100  # yfinance 1.x の dividendYield は百分率（0.31 = 0.31%）


def currency_label(currency):
    return "円" if currency == "JPY" else (currency or "")


# ==========================================================
# データ取得（キャッシュなし。Streamlit側でキャッシュを被せる）
# ==========================================================
def fetch_info(sym, retries=0, wait=3.0):
    """yfinanceの銘柄情報を取得する。取得時刻 `_fetched_at` を付ける。
    アクセス制限(429)のときは retries 回まで待って再試行し、それでもだめなら簡易データ(fast_info)で代替する。"""
    for attempt in range(retries + 1):
        ticker = yf.Ticker(sym)
        try:
            info = ticker.info
            if not info:
                return {}
            info = dict(info)
            if not info.get("currentPrice"):  # ETFなどは currentPrice が無い
                p = info.get("regularMarketPrice") or info.get("previousClose")
                if p:
                    info["currentPrice"] = p
            info["_fetched_at"] = now_jst().isoformat(timespec="seconds")
            return info
        except Exception as e:
            if not is_rate_limit_error(e):
                raise
            if attempt < retries:
                time.sleep(wait * (attempt + 1))
                continue
            try:
                fi = ticker.fast_info
                return {
                    "currentPrice": getattr(fi, "last_price", None),
                    "marketCap": getattr(fi, "market_cap", None),
                    "currency": getattr(fi, "currency", "JPY"),
                    "fiftyTwoWeekHigh": getattr(fi, "year_high", None),
                    "shortName": sym,
                    "_rate_limited": True,
                    "_fetched_at": now_jst().isoformat(timespec="seconds"),
                }
            except Exception:
                raise e
    return {}


def _strip_tz(df):
    if df is not None and len(df) and getattr(df.index, "tz", None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df


def fetch_dividends(sym):
    try:
        return _strip_tz(yf.Ticker(sym).dividends)
    except Exception:
        return pd.Series(dtype="float64")


def fetch_history(sym, period="1y"):
    try:
        h = yf.Ticker(sym).history(period=period)
        return _strip_tz(h) if h is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


SECTOR_MAP = {
    "Basic Materials": "素材", "Consumer Cyclical": "一般消費財", "Financial Services": "金融",
    "Real Estate": "不動産", "Consumer Defensive": "生活必需品", "Healthcare": "ヘルスケア",
    "Utilities": "公益事業", "Communication Services": "通信サービス", "Energy": "エネルギー",
    "Industrials": "資本財", "Technology": "情報技術",
}


def get_jp_company_info(sym, default_name, default_sector):
    """日本株は Yahoo!ファイナンス(日本)から日本語の社名を取得し、業種を日本語に変換して返す"""
    name = default_name
    if sym.endswith(".T") or sym.endswith(".t"):
        try:
            req = urllib.request.Request(f"https://finance.yahoo.co.jp/quote/{sym}", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                html = response.read().decode("utf-8")
            match = re.search(r"<title>(.*?)【", html)
            if match:
                name = match.group(1).replace("(株)", "").strip()
        except Exception:
            pass
    sector_jp = SECTOR_MAP.get(default_sector, default_sector if default_sector else "不明")
    return name, sector_jp


# 有料購読が必要なメディア（ニュースから除外）
BANNED_SOURCES = ["日経", "日本経済新聞", "ブルームバーグ", "Bloomberg", "ウォール", "WSJ", "ダイヤモンド",
                  "四季報", "NewsPicks", "朝日新聞", "毎日新聞", "読売新聞", "産経新聞"]


def fetch_news(sym, company_name, limit=3):
    """Google Newsから無料で読める日本語ニュースを取得する。(記事のリスト, エラー文 or None) を返す"""
    try:
        query = urllib.parse.quote(f"{sym.split('.')[0]} {company_name}")
        url = f"https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
        items = []
        for item in ET.fromstring(xml_data).findall(".//channel/item"):
            title = item.find("title").text if item.find("title") is not None else "タイトル不明"
            link = item.find("link").text if item.find("link") is not None else "#"
            source = item.find("source").text if item.find("source") is not None else ""
            if any(b in title or b in source for b in BANNED_SOURCES):
                continue
            items.append({"title": title, "link": link})
            if len(items) >= limit:
                break
        return items, None
    except Exception as e:
        return [], str(e)


@dataclass
class Fetchers:
    """データ取得関数のセット。Webアプリではキャッシュ付きの関数に、テストでは偽物に差し替える"""
    info: Callable
    dividends: Callable
    history: Callable
    jp_info: Callable
    news: Callable


def default_fetchers():
    """キャッシュなしの標準のデータ取得関数（呼び出し時に解決するので、テストで差し替えやすい）"""
    return Fetchers(info=fetch_info, dividends=fetch_dividends, history=fetch_history,
                    jp_info=get_jp_company_info, news=fetch_news)


# ==========================================================
# 指標の計算
# ==========================================================
def calc_technicals(hist):
    """移動平均・RSI(14日)・直近1ヶ月リターン。データが50日以下なら None"""
    if hist is None or len(hist) <= 50 or "Close" not in hist:
        return None
    close = hist["Close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain / loss))

    def last(s):
        v = s.iloc[-1]
        return None if pd.isna(v) else float(v)

    price_1mo = close.iloc[-21] if len(close) > 20 else close.iloc[0]
    return {
        "last_price": float(close.iloc[-1]),
        "sma20": last(close.rolling(20).mean()),
        "sma50": last(close.rolling(50).mean()),
        "sma200": last(close.rolling(200).mean()),
        "rsi14": last(rsi),
        "mo1_perf": float((close.iloc[-1] - price_1mo) / price_1mo * 100),
    }


def technical_text(t):
    """AIに渡すテクニカル指標の文章"""
    if not t:
        return "データ不足のため算出不可"

    def f(v, nd=1):
        return f"{v:.{nd}f}" if v is not None else "N/A"

    s = f"現在値: {f(t['last_price'])}, 20日移動平均: {f(t['sma20'])}, 50日移動平均: {f(t['sma50'])}, "
    if t.get("sma200") is not None:
        s += f"200日移動平均: {f(t['sma200'])}, "
    return s + f"RSI(14日): {f(t['rsi14'])}, 直近1ヶ月リターン: {t['mo1_perf']:.1f}%"


def trend_label(price, sma50, sma200, cur):
    """200日線を基準にしたトレンド表示"""
    if isnum(sma50) and isnum(sma200) and price:
        if price > sma200 and sma50 > sma200:
            return f"📈 上昇トレンド (200日線 {sma200:,.1f} {cur}を上抜け)"
        if price < sma200:
            return f"📉 下降・安値圏 (200日線 {sma200:,.1f} {cur}より {(price - sma200) / sma200 * 100:.1f}%)"
        return f"📊 もみ合い (200日線 {sma200:,.1f} {cur}付近)"
    return "トレンド判定不能"


def analyze_dividends(dividends, cur, this_year=None):
    """配当の年間推移・配当月・「減配していない年数」を計算する"""
    out = {"trend_str": "データなし", "progressive_years": 0, "months_str": "N/A", "annual": {}}
    if dividends is None or len(dividends) == 0:
        return out
    if getattr(dividends.index, "tz", None) is not None:
        dividends = dividends.copy()
        dividends.index = dividends.index.tz_localize(None)
    this_year = this_year or pd.Timestamp.now().year
    try:
        out["months_str"] = " ".join(f"{m}月" for m in sorted(set(dividends.tail(4).index.month)))
    except Exception:
        pass
    annual = dividends.groupby(dividends.index.year).sum()
    last_years = annual[annual.index <= this_year].tail(6)
    if last_years.empty:
        return out
    years = [int(y) for y in last_years.index]
    vals = [float(v[0]) if hasattr(v, "__iter__") and not isinstance(v, str) else float(v) for v in last_years.values]
    out["annual"] = dict(zip(years, vals))
    hist_list = [f"{y}年: {v:.1f}{cur}{'（暫定）' if y == this_year else ''}" for y, v in zip(years, vals)]
    out["trend_str"] = " → ".join(hist_list[-5:])
    check = []
    for y, v in zip(years, vals):
        if y != this_year or v >= (check[-1] if check else 0):
            check.append(v)
    for i in range(len(check) - 1, 0, -1):
        if check[i] >= check[i - 1] * 0.98:
            out["progressive_years"] += 1
        else:
            break
    return out


# ==========================================================
# 指標の評価（色分け用の目安）
# ==========================================================
LEVEL_TEXT = {"good": ("良好", "green"), "neutral": ("普通", "gray"), "caution": ("注意", "orange"), "na": ("―", "gray")}

METRIC_HELP = {
    "price": "現在の株価（Yahoo Finance。最大20分程度遅れることがあります）。",
    "div_month": "直近の配当が支払われた月と、次回の権利落ち日。",
    "pe": "株価収益率。低いほど利益に対して割安。目安：15倍未満=良好、25倍超=注意、赤字は注意。業種で水準が大きく異なります。",
    "div_yield": "年間配当÷株価。目安：3.5%以上=良好、7%以上は減配リスクの可能性があり注意。",
    "payout": "利益のうち配当に回す割合。目安：60%以下=良好、80%超=注意（無理して配当を出している可能性）。",
    "roe": "自己資本利益率。高いほど資本を効率よく使えている。目安：15%以上=良好、8%未満=注意。",
    "de": "負債比率(D/Eレシオ)。低いほど財務が安定。目安：50%未満=良好、150%超=注意。金融業は高くなりがちです。",
    "beta": "市場全体に対する値動きの大きさ。1より小さいほど穏やか。目安：0.8未満=良好、1.2超=注意。",
    "revenue": "直近12ヶ月の売上高。",
    "op_margin": "売上に対する営業利益の割合。目安：15%以上=良好、5%未満=注意。",
    "rev_growth": "売上高の前年比成長率。目安：10%以上=良好、マイナス=注意。",
    "eps_growth": "純利益の前年比成長率。目安：10%以上=良好、マイナス=注意。",
    "rsi": "RSI(14日)。30以下は売られすぎ（良好＝買い場の目安）、70以上は買われすぎ（注意）。",
    "mo1": "約1ヶ月前（21営業日前）との株価の変化率。",
    "trend": "現在値と50日線・200日線の位置関係から見たトレンド。",
}


def rate_metric(key, v):
    """指標を「良好 / 普通 / 注意 / 取得不可」に分類する（一般的な目安であり、投資判断の保証ではない）"""
    if not isnum(v):
        return "na"
    v = float(v)
    if key == "div_yield":
        return "good" if 0.035 <= v < 0.07 else ("caution" if v >= 0.07 else "neutral")
    if key == "pe":
        return "caution" if v <= 0 else ("good" if v < 15 else ("neutral" if v <= 25 else "caution"))
    if key == "payout":
        return "caution" if v > 0.8 else ("good" if 0 <= v <= 0.6 else "neutral")
    if key == "roe":
        return "good" if v >= 0.15 else ("neutral" if v >= 0.08 else "caution")
    if key == "de":
        return "good" if v < 50 else ("neutral" if v <= 150 else "caution")
    if key == "beta":
        return "good" if v < 0.8 else ("neutral" if v <= 1.2 else "caution")
    if key == "op_margin":
        return "good" if v >= 0.15 else ("neutral" if v >= 0.05 else "caution")
    if key in ("rev_growth", "eps_growth"):
        return "good" if v >= 0.10 else ("neutral" if v >= 0 else "caution")
    if key == "rsi":
        return "good" if v <= 30 else ("caution" if v >= 70 else "neutral")
    return "neutral"


def judge_signals(div_yield, payout, pe, roe, beta, progressive_years):
    """個別銘柄のシグナル判定（タブ1）"""
    sig = []
    if isnum(div_yield) and div_yield >= 0.04 and isnum(payout) and payout < 0.6:
        sig.append("🟢 **【高配当シグナル】** 配当利回り4%以上 ＆ 配当性向60%未満の「お宝高配当銘柄」の条件を満たしました。")
    if isnum(pe) and pe < 15 and isnum(roe) and roe >= 0.15:
        sig.append("🟢 **【割安優良シグナル】** PER15倍未満 ＆ ROE15%以上の「割安優良銘柄」の条件を満たしました。")
    if isnum(beta) and beta < 0.8:
        sig.append("🔵 **【安定シグナル】** ベータ値0.8未満の「価格変動が穏やかな安定銘柄」です。")
    if progressive_years >= 3:
        sig.append(f"🔥 **【累進配当シグナル】** 直近{progressive_years}年間、実質的な減配がない継続的な株主還元銘柄です。")
    return sig


# ==========================================================
# 銘柄の総合分析（タブ1・Bot共通）
# ==========================================================
def _revenue_str(total, currency, cur):
    if not isnum(total) or total <= 0:
        return "N/A"
    if currency == "JPY":
        return f"{total / 1e8:,.0f} 億円"
    return f"{total / 1e9:,.1f} Billion {cur}"


def data_quality_notes(a):
    """データの信頼性に関する注意書き（簡易データ・欠けている項目）"""
    notes = []
    if a.get("rate_limited"):
        notes.append("⚠️ Yahoo Financeのアクセス制限を受けたため、簡易データ（株価・時価総額・52週高値のみ）で表示しています。時間をおいて再取得してください。")
    missing = [label for label, v in (("PER", a["pe"]), ("配当利回り", a["div_yield"]), ("配当性向", a["payout"]),
                                      ("ROE", a["roe"]), ("負債比率", a["de"]), ("売上高成長率", a["rev_growth"]),
                                      ("営業利益率", a["op_margin"])) if not isnum(v)]
    if missing and not a.get("rate_limited"):
        notes.append("取得できなかった項目: " + "、".join(missing) + "（無配・赤字・上場間もない等の場合があります）")
    return notes


def collect_analysis(sym, fetchers=None, hist_period="1y", with_news=True):
    """銘柄の分析に必要なデータをまとめて取得・計算する。存在しない銘柄は ValueError"""
    f = fetchers or default_fetchers()
    info = f.info(sym)
    if is_unknown_ticker(info):
        raise ValueError(f"銘柄コード「{sym}」の情報が見つかりませんでした")

    raw_name = info.get("shortName", sym)
    raw_sector = info.get("sector", "")
    company_name, sector_jp = f.jp_info(sym, raw_name, raw_sector)
    currency = info.get("currency", "")
    cur = currency_label(currency)
    price = info.get("currentPrice")

    ex_ts = info.get("exDividendDate")
    ex_div = datetime.datetime.fromtimestamp(ex_ts, JST) if ex_ts else None
    div = analyze_dividends(f.dividends(sym), cur)
    hist = f.history(sym, hist_period)
    tech = calc_technicals(hist)

    news, news_error = ([], None)
    if with_news:
        news, news_error = f.news(sym, company_name)
    if news:
        news_text = "".join(f"・{n['title']}\n" for n in news)
    else:
        news_text = "ニュース取得に失敗しました" if news_error else "関連ニュースなし"

    a = {
        "symbol": sym, "info": info, "company_name": company_name, "sector_jp": sector_jp, "raw_sector": raw_sector,
        "currency": currency, "cur": cur, "price": price,
        "pe": info.get("trailingPE"), "div_yield": calc_div_yield(info, price),
        "payout": info.get("payoutRatio"), "roe": info.get("returnOnEquity"), "de": info.get("debtToEquity"),
        "beta": info.get("beta"), "op_margin": info.get("operatingMargins"),
        "rev_growth": info.get("revenueGrowth"), "eps_growth": info.get("earningsGrowth"),
        "total_revenue": info.get("totalRevenue") or 0,
        "rev_str": _revenue_str(info.get("totalRevenue"), currency, cur),
        "ex_div": ex_div, "div": div, "hist": hist, "tech": tech, "technical_text": technical_text(tech),
        "news": news, "news_error": news_error, "news_text": news_text,
        "fetched_at": info.get("_fetched_at"), "rate_limited": bool(info.get("_rate_limited")),
    }
    a["notes"] = data_quality_notes(a)
    return a


# ==========================================================
# AI（OpenAI）プロンプト
# ==========================================================
WEB_SYSTEM = ("あなたは優秀な株式アナリスト兼長期投資家です。業界ごとの基準の違い（銀行業はPERが低い、ITは高い等）を"
              "必ず考慮に入れ、業界水準と比較しながら分析してください。")


def _s(v, fmt):
    return fmt(v) if isnum(v) else "N/A"


def build_report_prompt(a, discord=False):
    """AI分析レポート用のプロンプト。discord=True のときはDiscord向け（2000字以内）"""
    pe = f"{a['pe']:.1f}倍" if isnum(a["pe"]) else "N/A"
    common = f"""
        【企業基本情報】
        企業名: {a['company_name']}
        セクター(業種): {a['sector_jp']} ({a['raw_sector']})

        【財務指標】
        PER: {pe}
        ROE: {fmt_pct(a['roe'])}
        営業利益率: {fmt_pct(a['op_margin'])}

        【成長性・安全性】
        売上高: {a['rev_str']}
        売上高成長率(前年比): {fmt_pct(a['rev_growth'])}
        純利益成長率(前年比): {fmt_pct(a['eps_growth'])}
        配当性向: {fmt_pct(a['payout'])}
        負債比率 (D/E): {_s(a['de'], lambda v: f'{v:.2f}%')}
        ベータ値: {_s(a['beta'], lambda v: f'{v:.2f}')}

        【配当実績】
        推移: {a['div']['trend_str']}
        → 連続減配なし（累進配当実績）: {a['div']['progressive_years']}年間
"""
    ex = a["ex_div"].strftime("%Y/%m/%d") if a["ex_div"] else "不明"
    if discord:
        return f"""
        あなたはウォーレン・バフェットのような優秀な長期投資家です。
        以下の企業指標、ニュース、過去の配当推移をもとに、この銘柄を長期保有する視点で分析してください。
{common}        次回権利落ち日: {ex}

        【テクニカル指標 (直近)】
        {a['technical_text']}

        【直近のニュース】
        {a['news_text']}

        ---
        以下の構成でマークダウン形式で簡潔にレポートを作成してください。(Discord文字数制限2000字以内を厳守)

        ### 📊 1. 業界平均との比較評価
        ### 📈 2. テクニカル分析＆スイング視点
        ### ✅ 3. 長期保有における強み
        ### ⚠️ 4. 留意すべきリスク
        ### 🎁 5. 株主優待・権利確定日
        この企業の株主優待の有無や内容について簡潔に紹介し、次回権利落ち日（{ex}）も明記してください。
        ### 💡 6. 総合評価
        """
    return f"""
        あなたはウォーレン・バフェットのような優秀な長期投資家です。
        以下の企業指標、ニュース、過去の配当推移をもとに、この銘柄を長期保有する視点で分析してください。
{common}
        【テクニカル指標 (直近)】
        {a['technical_text']}

        【直近のニュース】
        {a['news_text']}

        ---
        以下の構成でマークダウン形式で簡潔にレポートを作成してください。

        ### 📊 1. 業界平均との比較評価（最重要）
        この銘柄が属する「{a['sector_jp']}業界」の一般的な平均PER・ROE水準（あなたの知識に基づく大体の数値）を明記し、本銘柄の数値がそれと比べて「割安か割高か」「収益性は優秀か劣後しているか」を必ず解説してください。

        ### 📈 2. テクニカル分析＆スイングトレード視点
        現在の株価モメンタム（移動平均線やRSI、直近パフォーマンス等）を踏まえ、スイングトレードとして短期的なエントリー妙味があるか、それとも高値警戒感やトレンド転換のリスクがあるか等をテクニカル分析の視点で解説してください。

        ### ✅ 3. 長期保有における強み (Strengths)
        ### ⚠️ 4. 留意すべきリスク (Risks)
        ### 🎁 5. 株主優待情報 (Shareholder Benefits)
        この企業（特に日本株の場合）の株主優待の有無や内容について、あなたの知識データベースから簡潔に紹介してください。すでに廃止されている有名な優待（JTやオリックスなど）はその旨を必ず警告してください。※米国株など優待文化がない場合は「なし」として過度な説明は省き、最後に「※優待内容は変更・廃止される可能性があるため最新の公式HPを必ずご確認ください」と免責を添えてください。

        ### 💡 6. 総合評価 (Conclusion)
        """


def report_messages(a, discord=False):
    """OpenAIに渡す messages と max_tokens を返す"""
    if discord:
        system = (f"現在の日付は {now_jst().strftime('%Y年%m月%d日')} です。あなたは優秀な株式アナリスト兼長期投資家です。"
                  "Discordで出力するため、1500文字程度で非常に簡潔かつ要点を突いて回答してください。")
        tokens = 1500
    else:
        system, tokens = WEB_SYSTEM, 2000
    return ([{"role": "system", "content": system}, {"role": "user", "content": build_report_prompt(a, discord)}], tokens)


def generate_report(client, a, discord=False, model="gpt-4o-mini"):
    messages, tokens = report_messages(a, discord)
    resp = client.chat.completions.create(model=model, messages=messages, max_tokens=tokens)
    return resp.choices[0].message.content


# AI会議（Bot）
MEETING_SYSTEM = {
    "tech": "あなたは『テクニカルアナリスト』。チャート至上主義で、モメンタムやトレンドなど短期〜中期の値動きを最重視する専門家。Discordのチャットなので短く250文字以内で、客観的かつプロフェッショナルに答えて。",
    "funda": "あなたは『ファンダメンタルアナリスト』。財務諸表（PER、負債、ROE）やマクロ経済、企業価値を最重視する慎重な専門家。Discordチャットなので短く250文字以内で、客観的かつプロフェッショナルに答えて。",
    "mod": "あなたは『モデレーター』。非常に優秀で冷静沈着な司会役。テクニカルアナリストとファンダメンタルアナリストの意見を客観的にまとめ、最終的にユーザーはこの銘柄を「短期トレードすべきか、長期保有すべきか、様子見か」の結論を導き出す。文字数は300文字以内で、箇条書きを使ってスッキリまとめて。",
}


def meeting_context(a):
    ex = a["ex_div"].strftime("%Y/%m/%d") if a["ex_div"] else "不明"
    t = a["tech"]
    tech = (f"現在値:{t['last_price']:.1f}, 20日線:{_s(t['sma20'], lambda v: f'{v:.1f}')}, "
            f"50日線:{_s(t['sma50'], lambda v: f'{v:.1f}')}, RSI:{_s(t['rsi14'], lambda v: f'{v:.1f}')}") if t else "データ不足"
    return f"""
        【企業】 {a['company_name']} ({a['sector_jp']})
        【価格】 {a['price']} {a['cur']}
        【テクニカル】 {tech}
        【財務】 PER: {a['pe'] if isnum(a['pe']) else 'N/A'}倍, ROE: {_s(a['roe'], lambda v: f'{v * 100:.1f}')}%, 負債比率: {a['de'] if isnum(a['de']) else 'N/A'}
        【配当推移】 {a['div']['trend_str']} (連続減配なし {a['div']['progressive_years']}年)
        【権利確定】 次回権利落ち日: {ex}
        """


def basic_summary_text(a):
    """OpenAIキーがないときに返す基本データ（Bot）"""
    ex = a["ex_div"].strftime("%Y/%m/%d") if a["ex_div"] else "不明"
    return (f"🏢 **{a['company_name']} ({a['symbol']})**（{a['sector_jp']}）\n"
            f"- 現在値: {a['price']} {a['cur']}\n"
            f"- PER: {_s(a['pe'], lambda v: f'{v:.1f}倍')} / 配当利回り: {fmt_pct(a['div_yield'])} / "
            f"配当性向: {fmt_pct(a['payout'], 1)} / ROE: {fmt_pct(a['roe'], 1)}\n"
            f"- 配当推移: {a['div']['trend_str']}\n- 次回権利落ち日: {ex}\n- テクニカル: {a['technical_text']}")


# ==========================================================
# Discord
# ==========================================================
def split_for_discord(text, limit=1900):
    """Discordの1メッセージ上限(2000文字)に収まるよう、空行の区切りを優先して分割する"""
    chunks, buf = [], ""
    for block in text.split("\n\n"):
        while len(block) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(block[:limit])
            block = block[limit:]
        if buf and len(buf) + len(block) + 2 > limit:
            chunks.append(buf)
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        chunks.append(buf)
    return chunks


def send_discord(webhook_url, content, pause=0.5):
    """DiscordのWebhookへ送信する（2000文字を超える場合は分割）。送信した通数を返す。失敗時は例外"""
    chunks = split_for_discord(content)
    for chunk in chunks:
        req = urllib.request.Request(
            webhook_url, data=json.dumps({"content": chunk}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15):
            pass
        time.sleep(pause)
    return len(chunks)


# ==========================================================
# 監視リストのスキャン（アラート判定）
# ==========================================================
SCAN_DEFAULTS = {"drop_pct": 15.0, "yield_pct": 4.0, "target_pct": 20.0, "days_before": 14,
                 "rsi": 30.0, "payout_pct": 80.0, "renotify_days": 7}

ALERT_NAMES = {"drop": "高値から急落", "yield": "高配当化", "target": "目標株価より割安", "rsi": "RSI大底",
               "payout": "減配リスク", "earnings": "決算接近", "exdiv": "権利日接近"}


def alert_type(key):
    return key.split(":")[0]


def evaluate_alerts(info, hist, settings, now=None):
    """1銘柄のアラートを判定する。{price, cur, trend, rsi, alerts:[{key,text}]} を返す"""
    s = {**SCAN_DEFAULTS, **(settings or {})}
    now = now or now_jst()
    price = info.get("currentPrice")
    high_52 = info.get("fiftyTwoWeekHigh")
    cur = currency_label(info.get("currency", ""))

    drop = (high_52 - price) / high_52 if price and high_52 and high_52 > 0 else 0
    div_yield = calc_div_yield(info, price) or 0

    tech = calc_technicals(hist)
    rsi = tech["rsi14"] if tech else None
    trend = trend_label(price, tech["sma50"] if tech else None, tech["sma200"] if tech else None, cur)

    alerts = []
    if drop * 100 >= s["drop_pct"]:
        alerts.append({"key": "drop", "text": f"⚠️ **高値から急落!!** (52週高値 {high_52} → 現在 {price} : **{drop * 100:.1f}%下落**)"})
    if div_yield * 100 >= s["yield_pct"]:
        alerts.append({"key": "yield", "text": f"💰 **お宝高配当化!!** (現在の配当利回り: **{div_yield * 100:.2f}%**)"})
    target = info.get("targetMeanPrice")
    if target and price and price > 0:
        diff = (target - price) / price * 100
        if diff >= s["target_pct"]:
            alerts.append({"key": "target", "text": f"🎯 **超割安放置株!!** アナリスト平均目標株価({target})に対して現在値が約 **{diff:.1f}%も割安** です。"})
    if rsi is not None and rsi <= s["rsi"]:
        alerts.append({"key": "rsi", "text": f"📉 **【大底検知】** RSIが **{rsi:.1f}** まで低下しており、パニック的な売られすぎ水準にあります。"})
    payout = info.get("payoutRatio")
    if isnum(payout) and payout * 100 >= s["payout_pct"]:
        alerts.append({"key": "payout", "text": f"🚨 **【減配リスク警戒】** 配当性向が **{payout * 100:.1f}%** と極めて高く、無理して配当を出しているため減配リスクがあります。"})

    ts = info.get("earningsTimestamp")
    if ts:
        d = datetime.datetime.fromtimestamp(ts, JST)
        days = (d - now).days
        if 0 <= days <= s["days_before"]:
            alerts.append({"key": f"earnings:{d.strftime('%Y-%m-%d')}",
                           "text": f"🗓️ **【決算目前】** {days}日後 ({d.strftime('%Y/%m/%d')}) に決算発表があります！"})
    ts = info.get("exDividendDate")
    if ts:
        d = datetime.datetime.fromtimestamp(ts, JST)
        days = (d - now).days
        if 0 <= days <= s["days_before"]:
            alerts.append({"key": f"exdiv:{d.strftime('%Y-%m-%d')}",
                           "text": f"🎁 **【権利日目前】** {days}日後 ({d.strftime('%Y/%m/%d')}) が配当落ち日です！"})
    return {"price": price, "cur": cur, "trend": trend, "rsi": rsi, "alerts": alerts}


def scan_tickers(tickers, settings, info_fn, hist_fn, name_fn=None, progress=None, pause=0.5, now=None):
    """複数銘柄をスキャンして結果のリストを返す。取得に失敗した銘柄は error に理由を入れる"""
    results = []
    for i, t in enumerate(tickers):
        try:
            info = info_fn(t)
            if is_unknown_ticker(info):
                raise ValueError("銘柄情報が見つかりません")
            hist = hist_fn(t, "1y")
            name = name_fn(t, info) if name_fn else info.get("shortName", t)
            res = {"symbol": t, "name": name, "error": None, **evaluate_alerts(info, hist, settings, now)}
        except Exception as e:
            res = {"symbol": t, "name": t, "error": str(e), "alerts": [], "price": None, "cur": "", "trend": "", "rsi": None}
        results.append(res)
        if progress:
            progress(i, len(tickers), res)
        if pause and i < len(tickers) - 1:
            time.sleep(pause)
    return results


def format_alert_block(res, keys=None):
    """1銘柄分のアラート文。keys を渡すと、そのアラートだけに絞る"""
    alerts = [a for a in res["alerts"] if keys is None or a["key"] in keys]
    if not alerts:
        return None
    return (f"### 🏢 {res['name']} ({res['symbol']})\n💰現在値: {res['price']} {res['cur']} | {res['trend']}\n"
            + "\n".join(f"- {a['text']}" for a in alerts))


def format_scan_message(results, title, only=None):
    """スキャン結果のDiscord用メッセージ。only は {(銘柄, アラートkey)} で、渡すと新規アラートだけに絞る。通知対象が無ければ None"""
    blocks = []
    for r in results:
        keys = None if only is None else {k for (s, k) in only if s == r["symbol"]}
        b = format_alert_block(r, keys)
        if b:
            blocks.append(b)
    failed = [r["symbol"] for r in results if r.get("error")]
    if not blocks:
        return None
    msg = title + "\n\n" + "\n\n".join(blocks)
    if failed:
        msg += "\n\n⚠️ データ取得に失敗: " + "、".join(failed)
    return msg


# 通知済みアラートの管理（同じアラートを毎回通知しない）
STATE_COLUMNS = ["銘柄コード", "アラート種別", "初回通知日", "最終確認日"]


def filter_new_alerts(results, state_df, today, renotify_days=7):
    """今回のアラートのうち「新規」のものを返し、更新後の通知済み状態も返す。
    - 状態に無いアラート → 新規（通知する）
    - 状態にあり、初回通知から renotify_days 日未満 → 通知しない
    - 状態にあり、renotify_days 日以上 → 再通知
    - 状態にあるが今回は発生していない（今回スキャンできた銘柄のみ）→ 状態から削除（再発時は新規扱い）
    - 取得に失敗した銘柄・今回スキャンしていない銘柄の状態は変更しない
    """
    today_s = today.strftime("%Y-%m-%d") if hasattr(today, "strftime") else str(today)
    prev = {}
    if state_df is not None and len(state_df):
        for _, r in state_df.iterrows():
            prev[(str(r["銘柄コード"]), str(r["アラート種別"]))] = (str(r["初回通知日"]), str(r["最終確認日"]))
    scanned_ok = {r["symbol"] for r in results if not r.get("error")}
    new_items, new_state = set(), {}
    for (sym, key), v in prev.items():
        if sym not in scanned_ok:
            new_state[(sym, key)] = v
    current = {(r["symbol"], a["key"]) for r in results if not r.get("error") for a in r["alerts"]}
    for item in current:
        if item in prev:
            first = prev[item][0]
            try:
                age = (datetime.date.fromisoformat(today_s) - datetime.date.fromisoformat(first)).days
            except ValueError:
                age = renotify_days
            if age >= renotify_days:
                new_items.add(item)
                new_state[item] = (today_s, today_s)
            else:
                new_state[item] = (first, today_s)
        else:
            new_items.add(item)
            new_state[item] = (today_s, today_s)
    rows = [{"銘柄コード": s, "アラート種別": k, "初回通知日": f, "最終確認日": l} for (s, k), (f, l) in sorted(new_state.items())]
    return new_items, pd.DataFrame(rows, columns=STATE_COLUMNS)


HISTORY_COLUMNS = ["日時", "銘柄コード", "企業名", "アラート種別", "内容", "現在値", "実行元"]


def history_rows(results, items, source, when):
    """スキャン履歴に記録する行（新規アラートのみ）"""
    rows = []
    for r in results:
        for a in r["alerts"]:
            if (r["symbol"], a["key"]) in items:
                rows.append({"日時": when.strftime("%Y-%m-%d %H:%M"), "銘柄コード": r["symbol"], "企業名": r["name"],
                             "アラート種別": a["key"], "内容": a["text"].replace("*", ""),
                             "現在値": r["price"] if r["price"] is not None else "", "実行元": source})
    return rows


def evaluate_alert_history(history_df, hist_fn, horizons=(5, 20)):
    """過去のアラート後に株価がどう動いたか（アラート時の価格を基準にしたN営業日後のリターン）を計算する"""
    df = history_df.copy()
    for h in horizons:
        df[f"{h}日後"] = float("nan")
    cache = {}
    for idx, row in df.iterrows():
        sym = str(row["銘柄コード"])
        base = pd.to_numeric(row["現在値"], errors="coerce")
        when = pd.to_datetime(str(row["日時"])[:10], errors="coerce")
        if pd.isna(base) or base <= 0 or pd.isna(when):
            continue
        if sym not in cache:
            cache[sym] = hist_fn(sym, "2y")
        h = cache[sym]
        if h is None or h.empty:
            continue
        close = h["Close"]
        pos = close.index.searchsorted(when)
        for n in horizons:
            if pos + n < len(close):
                df.at[idx, f"{n}日後"] = float(close.iloc[pos + n] / base - 1)
    return df


def summarize_alert_history(eval_df, horizons=(5, 20)):
    """アラート種別ごとの件数・平均リターン・上昇した割合"""
    if eval_df.empty:
        return pd.DataFrame()
    d = eval_df.copy()
    d["種別"] = d["アラート種別"].map(lambda k: ALERT_NAMES.get(alert_type(str(k)), str(k)))
    out = []
    for name, g in d.groupby("種別"):
        row = {"種別": name, "件数": len(g)}
        for n in horizons:
            col = g[f"{n}日後"].dropna()
            row[f"{n}日後 平均"] = col.mean() if len(col) else float("nan")
            row[f"{n}日後 上昇率"] = (col > 0).mean() if len(col) else float("nan")
            row[f"{n}日後 件数"] = len(col)
        out.append(row)
    return pd.DataFrame(out)


# ==========================================================
# 一覧・絞り込み・比較
# ==========================================================
def overview_row(sym, name, info, hist, dividends=None):
    """ウォッチリスト一覧の1行"""
    price = info.get("currentPrice")
    prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
    if not prev and hist is not None and len(hist) >= 2:
        prev = float(hist["Close"].iloc[-2])
    high = info.get("fiftyTwoWeekHigh")
    tech = calc_technicals(hist)
    prog = analyze_dividends(dividends, "")["progressive_years"] if dividends is not None else None
    dy = calc_div_yield(info, price)
    roe, payout = info.get("returnOnEquity"), info.get("payoutRatio")
    return {
        "銘柄コード": sym, "企業名": name, "現在値": price,
        "前日比(%)": (price / prev - 1) * 100 if price and prev else None,
        "配当利回り(%)": dy * 100 if dy is not None else None,
        "PER": info.get("trailingPE"),
        "ROE(%)": roe * 100 if isnum(roe) else None,
        "配当性向(%)": payout * 100 if isnum(payout) else None,
        "高値から(%)": -(high - price) / high * 100 if price and high else None,
        "RSI": tech["rsi14"] if tech else None,
        "非減配(年)": prog,
        "通貨": currency_label(info.get("currency", "")),
    }


def filter_overview(df, min_yield=None, max_pe=None, min_roe=None, max_payout=None, min_prog=None, max_rsi=None):
    """条件を指定した列は、値が取れていない銘柄を除外して絞り込む"""
    d = df

    def keep(cond):
        return d[cond.fillna(False)]

    if min_yield is not None:
        d = keep(d["配当利回り(%)"] >= min_yield)
    if max_pe is not None:
        d = keep((d["PER"] <= max_pe) & (d["PER"] > 0))
    if min_roe is not None:
        d = keep(d["ROE(%)"] >= min_roe)
    if max_payout is not None:
        d = keep(d["配当性向(%)"] <= max_payout)
    if min_prog is not None:
        d = keep(d["非減配(年)"] >= min_prog)
    if max_rsi is not None:
        d = keep(d["RSI"] <= max_rsi)
    return d


def normalize_prices(hists, base=100.0):
    """銘柄ごとの株価を、期間の最初を100とした指数にそろえる（比較チャート用）"""
    cols = {}
    for sym, h in hists.items():
        if h is not None and len(h):
            c = h["Close"].dropna()
            if len(c):
                cols[sym] = c / c.iloc[0] * base
    return pd.DataFrame(cols).sort_index()


# ==========================================================
# 保有株（ポートフォリオ）
# ==========================================================
PORTFOLIO_COLUMNS = ["銘柄コード", "企業名", "株数", "取得単価", "メモ"]


def calc_portfolio(holdings, infos, fx_usdjpy=None):
    """保有株の評価額・損益・予想配当を計算する。(明細DataFrame, 通貨別の合計DataFrame) を返す"""
    rows = []
    for _, h in holdings.iterrows():
        sym = normalize_ticker(h.get("銘柄コード"))
        qty = pd.to_numeric(h.get("株数"), errors="coerce")
        cost = pd.to_numeric(h.get("取得単価"), errors="coerce")
        if not sym or pd.isna(qty) or qty <= 0:
            continue
        info = infos.get(sym) or {}
        price = info.get("currentPrice")
        cur_code = info.get("currency") or ("JPY" if sym.endswith(".T") else "USD")
        div_rate = info.get("dividendRate") or 0
        acquired = qty * cost if not pd.isna(cost) else None
        value = qty * price if price else None
        pnl = value - acquired if value is not None and acquired is not None else None
        annual_div = qty * div_rate
        fx = 1.0 if cur_code == "JPY" else (fx_usdjpy if cur_code == "USD" else None)

        def yen(x):
            return x * fx if x is not None and fx is not None else None

        rows.append({
            "銘柄コード": sym, "企業名": h.get("企業名") or info.get("shortName", sym), "通貨": cur_code,
            "株数": qty, "取得単価": cost if not pd.isna(cost) else None, "現在値": price,
            "取得額": acquired, "評価額": value, "損益": pnl,
            "損益率(%)": pnl / acquired * 100 if pnl is not None and acquired else None,
            "予想年間配当": annual_div if div_rate else 0.0,
            "取得ベース利回り(%)": annual_div / acquired * 100 if acquired and div_rate else None,
            "現在利回り(%)": div_rate / price * 100 if price and div_rate else None,
            "取得額(円換算)": yen(acquired), "評価額(円換算)": yen(value), "損益(円換算)": yen(pnl),
            "予想年間配当(円換算)": yen(annual_div if div_rate else 0.0),
        })
    detail = pd.DataFrame(rows)
    if detail.empty:
        return detail, pd.DataFrame()
    tot = detail.groupby("通貨").agg(取得額=("取得額", "sum"), 評価額=("評価額", "sum"), 損益=("損益", "sum"),
                                     予想年間配当=("予想年間配当", "sum")).reset_index()
    tot["損益率(%)"] = tot["損益"] / tot["取得額"] * 100
    tot["配当利回り(評価額ベース,%)"] = tot["予想年間配当"] / tot["評価額"] * 100
    return detail, tot


# ==========================================================
# テンバガー発掘（タブ3）
# ==========================================================
def screen_tenbagger(sym, name, info):
    """AIが挙げた候補を時価総額・売上成長率で審査する。('valid' | 'rejected' | 'skip', 表示用dict)"""
    mcap = info.get("marketCap")
    if mcap is None:
        return "skip", None
    currency = info.get("currency", "")
    mcap_usd = mcap / 150 if currency == "JPY" else mcap
    mcap_str = f"{mcap / 1e8:,.0f}億円" if currency == "JPY" else f"${mcap / 1e9:,.1f} Billion"
    rg = info.get("revenueGrowth")
    growth = f"{rg * 100:.1f}%" if rg is not None else "不明"
    om = info.get("operatingMargins")
    margin = (f"{om * 100:.1f}%" + ("（赤字）" if om < 0 else "")) if isnum(om) else "不明"
    card = {"symbol": sym, "name": name, "price": info.get("currentPrice"), "currency": currency, "mcap": mcap_str,
            "growth": growth, "margin": margin}
    if rg is not None and rg < 0:
        return "rejected", {**card, "reason": "マイナス成長"}
    if mcap_usd > 15_000_000_000:
        return "rejected", {**card, "reason": "時価総額が大きすぎる"}
    return "valid", card


MAX_CANDIDATES = 30
_SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}")


def parse_candidates(text, max_items=MAX_CANDIDATES):
    """貼り付けた候補リストを読み取る。1行に1銘柄「コード 企業名」（区切りは空白・カンマ・タブ・|）。
    「# テーマ名」の行は、以降の銘柄のテーマになる。
    (候補のリスト[{theme, symbol, name}], 読み取れなかった行, 上限を超えて除いた件数) を返す"""
    theme, items, bad, seen, over = "貼り付けた候補", [], [], set(), 0
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[0] in "#＃":
            theme = line.lstrip("#＃").strip() or theme
            continue
        parts = re.split(r"[,	、，|｜]+|\s+", line, maxsplit=1)
        sym = normalize_ticker(parts[0])
        if not sym or not _SYMBOL_RE.fullmatch(sym):
            bad.append(line)
            continue
        if sym in seen:
            continue
        seen.add(sym)
        if len(items) >= max_items:
            over += 1
            continue
        items.append({"theme": theme, "symbol": sym, "name": parts[1].strip() if len(parts) > 1 else ""})
    return items, bad, over
