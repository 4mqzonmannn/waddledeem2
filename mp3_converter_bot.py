# -*- coding: utf-8 -*-

import discord
from discord.ext import commands
import yt_dlp
import os
import asyncio
import uuid
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# Discordボットのトークンを環境変数から取得
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
if TOKEN is None:
    print("エラー: DISCORD_BOT_TOKENが.envファイルに設定されていません。")
    exit()

# ボットのインテントを設定
intents = discord.Intents.default()
intents.message_content = True

# コマンドのプレフィックスを設定
bot = commands.Bot(command_prefix='!', intents=intents)

# Discordのファイルサイズ上限 (Nitroなしの場合)
# 25MB = 25 * 1024 * 1024 bytes
DISCORD_FILE_LIMIT = 26214400

@bot.event
async def on_ready():
    """ボットがログインしたときに呼び出されるイベント"""
    print(f'{bot.user.name} としてログインしました')
    print('------')

@bot.command()
async def mp3(ctx, *, url: str):
    """
    指定されたURLから音声を抽出し、MP3ファイルとして送信するコマンド
    """
    # 処理中であることをユーザーに通知
    processing_message = await ctx.reply("処理中です... URLから情報を取得しています。")

    # 一時ファイル名を生成 (他の処理と競合しないように)
    temp_filename_base = str(uuid.uuid4())
    temp_filepath = f"./{temp_filename_base}"
    final_filepath = ""

    loop = asyncio.get_running_loop()

    try:
        # yt-dlpのオプション設定
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_filepath, # 出力テンプレート (拡張子なし)
            'noplaylist': True,       # プレイリストのダウンロードを無効化
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192', # 音質 (ビットレート)
            }],
        }

        # ダウンロードと変換処理 (ブロッキング処理なので別スレッドで実行)
        await processing_message.edit(content="処理中です... 音声ファイルをダウンロード・変換しています。")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # `download`はブロッキングなので、executorで実行する
            await loop.run_in_executor(
                None, lambda: ydl.download([url])
            )

        # 変換後のファイルパス (yt-dlpが自動で.mp3を付与する)
        final_filepath = temp_filepath + ".mp3"

        # ファイルが存在するかチェック
        if not os.path.exists(final_filepath):
            raise FileNotFoundError("変換後のMP3ファイルが見つかりませんでした。")
        
        # ファイルサイズのチェック
        file_size = os.path.getsize(final_filepath)
        if file_size > DISCORD_FILE_LIMIT:
            await processing_message.edit(content=f"エラー: ファイルサイズが大きすぎます ({file_size / 1024 / 1024:.2f} MB)。Discordの制限により送信できません。")
            return

        # ファイルをDiscordに送信
        await processing_message.edit(content="処理完了！ファイルをアップロードしています...")
        await ctx.reply(file=discord.File(final_filepath))
        await processing_message.delete() # 処理中メッセージを削除

    except yt_dlp.utils.DownloadError as e:
        # yt-dlp関連のエラー
        print(f"yt-dlp Error: {e}")
        await processing_message.edit(content=f"エラー: URLの処理に失敗しました。\nサポートされていないサイトか、無効なURLの可能性があります。")

    except Exception as e:
        # その他の予期せぬエラー
        print(f"An unexpected error occurred: {e}")
        await processing_message.edit(content=f"予期せぬエラーが発生しました。\n`{e}`")

    finally:
        # 処理が終わったら、生成された一時ファイルを削除
        if os.path.exists(final_filepath):
            try:
                os.remove(final_filepath)
                print(f"Deleted temporary file: {final_filepath}")
            except OSError as e:
                print(f"Error deleting file {final_filepath}: {e}")

# ボットを実行
bot.run(TOKEN)
