"""Discord Bot のコマンドを、疑似のコンテキストで実行して確認する（実際のDiscord・OpenAI・Yahooには接続しない）"""
import asyncio
import inspect
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest import mock

import openai
from discord.ext import commands

import discord_bot as bot_mod
import invest_core as core
from tests.fakes import FakeMarket, FakeOpenAI, make_info


class Ctx:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class BotBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.market = FakeMarket().add("7203.T", info=make_info(shortName="TOYOTA", currentPrice=3105.0, dividendRate=100.0))
        stack.enter_context(self.market.install())
        stack.enter_context(mock.patch.object(core, "get_jp_company_info", lambda s, n, sec: ("トヨタ自動車", "一般消費財")))
        stack.enter_context(mock.patch.object(core, "fetch_news", lambda s, n, limit=3: ([{"title": "ニュース", "link": "x"}], None)))
        stack.enter_context(mock.patch.object(openai, "OpenAI", FakeOpenAI))
        stack.enter_context(mock.patch.object(bot_mod, "OPENAI_API_KEY", "sk-fake"))
        FakeOpenAI.calls.clear()


class TestCommands(BotBase):
    async def test_analyze(self):
        ctx = Ctx()
        await bot_mod.analyze.callback(ctx, "7203")
        self.assertIn("`7203`", ctx.sent[0])
        self.assertTrue(ctx.sent[1].startswith("🏢 **トヨタ自動車 (7203.T)** の分析結果:"))
        self.assertIn("(fake) AIレポート本文", ctx.sent[1])
        prompt = FakeOpenAI.calls[-1]["messages"][-1]["content"]
        self.assertIn("Discord文字数制限2000字以内", prompt)
        self.assertEqual(FakeOpenAI.calls[-1]["max_tokens"], 1500)

    async def test_analyze_without_key_shows_basic_data(self):
        ctx = Ctx()
        with mock.patch.object(bot_mod, "OPENAI_API_KEY", None):
            await bot_mod.analyze.callback(ctx, "7203")
        self.assertIn("基本データだけ表示します", ctx.sent[-1])
        self.assertIn("トヨタ自動車", ctx.sent[-1])
        self.assertIn("PER:", ctx.sent[-1])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_unknown_ticker(self):
        ctx = Ctx()
        await bot_mod.analyze.callback(ctx, "ZZZZ9999")
        self.assertTrue(ctx.sent[-1].startswith("❌ データの取得に失敗しました"))
        self.assertIn("見つかりませんでした", ctx.sent[-1])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_long_report_is_split(self):
        class Long(FakeOpenAI):
            def _create(self, **kw):
                return NS(choices=[NS(message=NS(content="\n\n".join("あ" * 900 for _ in range(6))))])
        ctx = Ctx()
        with mock.patch.object(openai, "OpenAI", Long):
            await bot_mod.analyze.callback(ctx, "7203")
        parts = ctx.sent[1:]
        self.assertTrue(len(parts) >= 3 and all(len(p) <= 2000 for p in parts), [len(p) for p in parts])

    async def test_debate_chain(self):
        ctx = Ctx()
        with mock.patch.object(asyncio, "sleep", mock.AsyncMock()):
            await bot_mod.debate.callback(ctx, "7203")
        self.assertEqual(len(ctx.sent), 4)
        self.assertIn("テクニカルアナリスト", ctx.sent[1])
        self.assertIn("ファンダメンタルアナリスト", ctx.sent[2])
        self.assertIn("モデレーター", ctx.sent[3])
        self.assertEqual(len(FakeOpenAI.calls), 3)
        self.assertIn("(fake) AIレポート本文", FakeOpenAI.calls[1]["messages"][-1]["content"])    # 前の人の意見が次に渡る
        self.assertIn("ファンダメンタルアナリストの意見", FakeOpenAI.calls[2]["messages"][-1]["content"])

    async def test_debate_without_key_and_unknown(self):
        ctx = Ctx()
        with mock.patch.object(bot_mod, "OPENAI_API_KEY", None):
            await bot_mod.debate.callback(ctx, "7203")
        self.assertIn("AI会議は実行できません", ctx.sent[-1])
        ctx = Ctx()
        await bot_mod.debate.callback(ctx, "ZZZZ9999")
        self.assertTrue(ctx.sent[-1].startswith("❌ 会議中にトラブルが発生しました"))

    async def test_command_errors(self):
        ctx = Ctx()
        param = commands.Parameter("ticker_symbol", inspect.Parameter.POSITIONAL_OR_KEYWORD)
        await bot_mod.on_command_error(ctx, commands.MissingRequiredArgument(param))
        self.assertIn("!分析 7203", ctx.sent[0])
        ctx2 = Ctx()
        await bot_mod.on_command_error(ctx2, commands.CommandNotFound("x"))
        self.assertEqual(ctx2.sent, [])


class TestChat(BotBase):
    async def test_mention_chat_with_search(self):
        me = NS(id=123)
        chan = NS(sent=[])

        async def send(m):
            chan.sent.append(m)

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        chan.send, chan.typing = send, (lambda: Typing())
        msg = NS(author=NS(id=1), content="<@123> 高配当株の選び方は？", mentions=[me], channel=chan, guild=None, _state=None)
        with mock.patch.object(bot_mod.bot._connection, "user", me, create=True), \
                mock.patch.object(bot_mod, "web_search_text", lambda q: "\n\n【参考: リアルタイムWeb検索結果】\n- fake検索結果\n"):
            await bot_mod.on_message(msg)
        self.assertEqual(chan.sent, ["(fake) AIレポート本文"])
        self.assertIn("fake検索結果", FakeOpenAI.calls[-1]["messages"][-1]["content"])
        self.assertIn("関西弁", FakeOpenAI.calls[-1]["messages"][0]["content"])


class TestResponsiveness(BotBase):
    async def test_event_loop_is_not_blocked_by_slow_io(self):
        """データ取得やAIの待ち時間中も、Botのイベントループが止まらない（止まるとDiscordとの接続が切れる）"""
        slow_fetch = core.fetch_info

        def slow_info(sym, *a, **k):
            time.sleep(0.8)
            return slow_fetch(sym, *a, **k)

        class SlowAI(FakeOpenAI):
            def _create(self, **kw):
                time.sleep(0.8)
                return super()._create(**kw)

        gaps, stop = [], [False]

        async def beat():
            last = time.perf_counter()
            while not stop[0]:
                await asyncio.sleep(0.02)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        ctx = Ctx()
        with mock.patch.object(core, "fetch_info", slow_info), mock.patch.object(openai, "OpenAI", SlowAI):
            task = asyncio.create_task(beat())
            await asyncio.sleep(0.05)
            await bot_mod.analyze.callback(ctx, "7203")
            stop[0] = True
            await task
        self.assertGreater(len(gaps), 20, "処理中に心拍が打てていない（イベントループが止まっていた）")
        self.assertLess(max(gaps), 0.5, f"イベントループが {max(gaps):.2f} 秒止まった")
        self.assertIn("(fake)", ctx.sent[-1])


if __name__ == "__main__":
    unittest.main()
