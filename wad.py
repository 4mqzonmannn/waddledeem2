
# -*- coding: utf-8 -*-

import discord
from discord.ext import commands
from flask import Flask
from threading import Thread
import os
import asyncio
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
if TOKEN is None:
    print("エラー: DISCORD_BOT_TOKENが.envファイルに設定されていません。")
    exit()

# ボットのインテントを設定
# サーバーのメンバーに関する情報を取得するためにintents.membersを有効にする
intents = discord.Intents.default()
intents.message_content = True
intents.members = True # Cogsでctx.authorなどを使うために推奨

# コマンドのプレフィックスを設定
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    """ボットがログインしたときに呼び出されるイベント"""
    print(f'{bot.user.name} としてログインしました')
    print('------')
    # Cogsの再読み込み（リロード）コマンドを同期する
    # これにより、/reload コマンドがDiscordに登録される
    try:
        synced = await bot.tree.sync()
        print(f"スラッシュコマンドを {len(synced)} 件同期しました。")
    except Exception as e:
        print(f"スラッシュコマンドの同期に失敗しました: {e}")


async def load_cogs():
    """cogsフォルダ内の.pyファイルをすべて読み込む"""
    print("Cogsを読み込んでいます...")
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py') and not filename.startswith('_'):
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                print(f'- {filename} を読み込みました。')
            except Exception as e:
                print(f'[エラー] {filename} の読み込みに失敗しました: {e}')
                print(f"Traceback: {e.__traceback__}")


# --- ボットの管理用コマンド ---
# 開発中にコードを修正した際、ボットを再起動せずにCogsをリロードできる
@bot.tree.command(name="reload", description="指定したCogを再読み込みします。")
@commands.is_owner() # ボットのオーナーのみ実行可能
async def reload(interaction: discord.Interaction, cog_name: str):
    """指定したCogをリロードするスラッシュコマンド"""
    try:
        await bot.reload_extension(f"cogs.{cog_name}")
        await interaction.response.send_message(f"`cogs.{cog_name}` をリロードしました。", ephemeral=True)
    except commands.ExtensionNotLoaded:
        await interaction.response.send_message(f"`cogs.{cog_name}` は読み込まれていません。", ephemeral=True)
    except commands.ExtensionNotFound:
        await interaction.response.send_message(f"`cogs.{cog_name}` が見つかりません。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"リロード中にエラーが発生しました: `{e}`", ephemeral=True)


async def main():
    """COGをロードしてボットを実行するメイン関数"""
    async with bot:
        await load_cogs()
        await bot.start(TOKEN)

# ボットの実行
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nボットを停止します。")

