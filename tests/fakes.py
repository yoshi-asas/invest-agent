"""テスト用の偽物（外部ネットワークに一切つながない）"""
import json
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest import mock

import numpy as np
import pandas as pd
import yfinance as yf

import invest_core


def make_hist(n=260, start=80.0, end=100.0, last_date="2026-10-20", volume=1000):
    """直線的に上昇する株価履歴（営業日）"""
    idx = pd.bdate_range(end=last_date, periods=n)
    close = np.linspace(start, end, n)
    return pd.DataFrame({"Open": close * 0.99, "High": close * 1.01, "Low": close * 0.98, "Close": close,
                         "Volume": volume}, index=idx)


def make_dividends(values=(2.0, 3.0, 4.0, 5.0, 5.0), first_year=2022, month=3):
    idx = pd.to_datetime([f"{first_year + i}-{month:02d}-30" for i in range(len(values))])
    return pd.Series(list(values), index=idx, dtype="float64")


def make_info(**kw):
    info = {"shortName": "FAKE CO", "sector": "Technology", "currentPrice": 100.0, "currency": "JPY",
            "trailingPE": 12.0, "dividendRate": 5.0, "payoutRatio": 0.3, "returnOnEquity": 0.2,
            "debtToEquity": 40.0, "beta": 0.5, "totalRevenue": 5e10, "operatingMargins": 0.12,
            "revenueGrowth": 0.05, "earningsGrowth": 0.1, "marketCap": 1e10, "fiftyTwoWeekHigh": 100.0,
            "previousClose": 99.0}
    info.update(kw)
    return info


class FakeMarket:
    """銘柄コードごとの偽データ。未登録の銘柄は『存在しない銘柄』として空の情報を返す"""

    def __init__(self):
        self.data = {}
        self.calls = []

    def add(self, sym, info=None, dividends=None, hist=None, raises=None, rate_limited=False):
        self.data[sym] = {"info": info if info is not None else make_info(),
                          "dividends": dividends if dividends is not None else make_dividends(),
                          "hist": hist if hist is not None else make_hist(),
                          "raises": raises, "rate_limited": rate_limited}
        return self

    def ticker(self, sym):
        self.calls.append(sym)
        d = self.data.get(sym)
        m = self

        class T:
            @property
            def info(s):
                if d is None:
                    return {}
                if d["raises"]:
                    raise d["raises"]
                return dict(d["info"])

            @property
            def fast_info(s):
                return NS(last_price=d["info"].get("currentPrice"), market_cap=d["info"].get("marketCap"),
                          currency=d["info"].get("currency"), year_high=d["info"].get("fiftyTwoWeekHigh"))

            @property
            def dividends(s):
                if d is None:
                    return pd.Series(dtype="float64")
                return d["dividends"].copy()

            def history(s, period="1y"):
                return pd.DataFrame() if d is None else d["hist"].copy()

        return T()

    @contextmanager
    def install(self):
        with mock.patch.object(yf, "Ticker", self.ticker):
            yield self


def fetchers_for(market, news=None, jp_names=None):
    """FakeMarketを使う Fetchers（ニュース・日本語社名もネットワークなし）"""
    jp_names = jp_names or {}

    def info(sym):
        d = market.data.get(sym)
        if d is None:
            return {}
        if d["raises"]:
            raise d["raises"]
        i = dict(d["info"])
        i.setdefault("_fetched_at", "2026-10-24T10:00:00+09:00")
        return i

    return invest_core.Fetchers(
        info=info,
        dividends=lambda sym: market.data[sym]["dividends"].copy() if sym in market.data else pd.Series(dtype="float64"),
        history=lambda sym, period="1y": market.data[sym]["hist"].copy() if sym in market.data else pd.DataFrame(),
        jp_info=lambda sym, name, sector: (jp_names.get(sym, name), invest_core.SECTOR_MAP.get(sector, sector or "不明")),
        news=lambda sym, name, limit=3: news if news is not None else ([{"title": "テストニュース", "link": "https://example.com/1"}], None),
    )


class FakeOpenAI:
    """OpenAI の偽物。呼び出し内容を記録する"""
    calls = []

    def __init__(self, **kw):
        self.chat = NS(completions=NS(create=self._create))

    THEMES = {"themes": [{"theme_name": "テストテーマ", "reason": "テスト用の理由",
                          "tickers": [{"symbol": "SMALL", "company_name": "小型成長"}, {"symbol": "BIG", "company_name": "大企業"},
                                      {"symbol": "NEG", "company_name": "減収"}, {"symbol": "NOPE", "company_name": "存在しない"}]}]}

    def _create(self, **kw):
        FakeOpenAI.calls.append(kw)
        content = json.dumps(FakeOpenAI.THEMES) if kw.get("response_format") else "(fake) AIレポート本文"
        return NS(choices=[NS(message=NS(content=content))])


class FakeDiscord:
    """Webhookへの送信を横取りする。それ以外の通信はそのまま通す"""

    def __init__(self):
        self.sent = []
        self._real = urllib.request.urlopen

    def _urlopen(self, req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "fake-webhook.invalid" in url:
            self.sent.append(json.loads(req.data.decode("utf-8"))["content"])
            return _Resp()
        return self._real(req, *a, **kw)

    @contextmanager
    def install(self):
        with mock.patch.object(urllib.request, "urlopen", self._urlopen):
            yield self


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return b""


WEBHOOK = "https://fake-webhook.invalid/api/webhooks/1/x"
