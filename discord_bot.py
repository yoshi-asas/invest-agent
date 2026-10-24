import asyncio
import os

import discord
import openai
from discord.ext import commands
from dotenv import load_dotenv

import invest_core as core

# 環境変数の読み込み（このファイルと同じフォルダの .env）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Botの準備
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)


def web_search_text(query):
    """DuckDuckGoで検索し、AIに渡す参考テキストを返す（失敗時は空文字）"""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = [r for r in ddgs.text(query, max_results=3, region="jp-jp")]
        if not results:
            return ""
        text = "\n\n【参考: リアルタイムWeb検索結果】\n"
        for res in results:
            text += f"- {res.get('title', '')}: {res.get('body', '')}\n"
        return text
    except Exception as e:
        print(f"Web検索エラー: {e}")
        return ""


async def chat_complete(client, **kwargs):
    """OpenAIの呼び出しを別スレッドで実行する（待っている間にBot全体が固まらないように）"""
    return await asyncio.to_thread(client.chat.completions.create, **kwargs)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("銘柄コードも一緒に送ってや。例: `!分析 7203` / `!会議 AAPL`")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"コマンドエラー: {error}")


@bot.command(name="分析")
async def analyze(ctx, ticker_symbol: str):
    await ctx.send(f"🔍 `{ticker_symbol}` のデータを取得・分析しています。少々お待ちください...")
    sym = core.normalize_ticker(ticker_symbol)
    try:
        # データ取得・指標計算は別スレッドで実行（待っている間もBotは他の操作に応答できる）
        a = await asyncio.to_thread(core.collect_analysis, sym)

        if not OPENAI_API_KEY:
            await ctx.send("⚠️ OpenAI APIキーが設定されていないため、AIの分析レポートは出力できません。基本データだけ表示します。\n"
                           + core.basic_summary_text(a))
            return

        messages, tokens = core.report_messages(a, discord=True)
        response = await chat_complete(openai.OpenAI(api_key=OPENAI_API_KEY), model="gpt-4o-mini", messages=messages, max_tokens=tokens)
        parts = core.split_for_discord(response.choices[0].message.content, limit=1850)
        for i, part in enumerate(parts):
            await ctx.send(f"🏢 **{a['company_name']} ({sym})** の分析結果:\n\n{part}" if i == 0 else part)
    except Exception as e:
        await ctx.send(f"❌ データの取得に失敗しました。銘柄コードが正しいか確認してください。（エラー詳細: `{e}`）")


@bot.command(name="会議")
async def debate(ctx, ticker_symbol: str):
    await ctx.send(f"🤖 専門チームを招集して `{ticker_symbol}` の会議を開始します。白熱するかもしれないので少々お待ちください...")
    sym = core.normalize_ticker(ticker_symbol)
    try:
        a = await asyncio.to_thread(core.collect_analysis, sym, None, "1y", False)   # 会議ではニュースは使わない

        if not OPENAI_API_KEY:
            await ctx.send("⚠️ OpenAI APIキーが設定されていないため、AI会議は実行できません。")
            return

        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        context = core.meeting_context(a)

        # 専門的な3人のAIキャラクターによるプロンプトチェーン（前の人の意見を次の人に渡す）
        resp_tech = (await chat_complete(
            client, model="gpt-4o-mini",
            messages=[{"role": "system", "content": core.MEETING_SYSTEM["tech"]},
                      {"role": "user", "content": f"以下のデータを見て、テクニカル視点での分析をお願いします。\n\n{context}"}]
        )).choices[0].message.content

        resp_funda = (await chat_complete(
            client, model="gpt-4o-mini",
            messages=[{"role": "system", "content": core.MEETING_SYSTEM["funda"]},
                      {"role": "user", "content": f"以下のデータと、先ほどのテクニカルアナリストの意見を踏まえて、ファンダメンタル視点からの厳密な分析をお願いします。\n\n【データ】\n{context}\n\n【テクニカルアナリストの意見】\n{resp_tech}"}]
        )).choices[0].message.content

        resp_mod = (await chat_complete(
            client, model="gpt-4o-mini",
            messages=[{"role": "system", "content": core.MEETING_SYSTEM["mod"]},
                      {"role": "user", "content": f"以下の二人の意見をまとめて、最終提案を出してください。\n\n【テクニカルアナリストの意見】\n{resp_tech}\n\n【ファンダメンタルアナリストの意見】\n{resp_funda}"}]
        )).choices[0].message.content

        # 結果の送信（時間差で送信して会話感を出す）
        await ctx.send(f"**【📈 テクニカルアナリスト】**\n{resp_tech}")
        await asyncio.sleep(1.5)
        await ctx.send(f"**【💼 ファンダメンタルアナリスト】**\n{resp_funda}")
        await asyncio.sleep(1.5)
        await ctx.send(f"**【💡 モデレーター】**\n{resp_mod}")
    except Exception as e:
        await ctx.send(f"❌ 会議中にトラブルが発生しました...（エラー詳細: `{e}`）")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # コマンド（!会議 や @bot 会議 など）として成立しているかチェック
    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    # Botへのメンションがある場合（かつコマンドではない場合＝通常のAIチャット）
    if bot.user in message.mentions:
        if not OPENAI_API_KEY:
            await message.channel.send("⚠️ OpenAI APIキーが設定されていないため、チャット機能は使用できません。")
            return

        # メンション部分を文字列から除去して本文だけを取り出す
        prompt_text = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not prompt_text:
            await message.channel.send("まいど！投資AIボットやで！銘柄分析（`!分析 〇〇`）や、投資環境などの雑談・ご相談になんでも答えるで〜。")
            return
        async with message.channel.typing():
            try:
                # Web検索で最新情報を取得
                search_results_text = await asyncio.to_thread(web_search_text, prompt_text)
                final_prompt_text = (prompt_text + search_results_text
                                     + "\n\n上記の【参考: リアルタイムWeb検索結果】がある場合はそれを加味して、ユーザーの質問に最新の情勢で答えてください。")
                response = await chat_complete(
                    openai.OpenAI(api_key=OPENAI_API_KEY), model="gpt-4o-mini", max_tokens=1500,
                    messages=[
                        {"role": "system", "content": f"現在の日付は {core.now_jst().strftime('%Y年%m月%d日')} です。あなたは優秀な株式アナリスト兼長期投資家です。ユーザーからの投資に関する質問や相談、相場動向について、プロの視点から【必ず親しみやすい関西弁で】答えてください。Discordでのチャットになるため、要点を1500文字程度で簡潔にまとめてください。"},
                        {"role": "user", "content": final_prompt_text},
                    ])
                for part in core.split_for_discord(response.choices[0].message.content, limit=1900):
                    await message.channel.send(part)
            except Exception as e:
                await message.channel.send(f"❌ AIとの通信中にエラーが発生しました: `{e}`")


if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        print("エラー: DISCORD_BOT_TOKEN が .env に設定されていません。")
    else:
        print("Botを起動しています...")
        bot.run(TOKEN)
