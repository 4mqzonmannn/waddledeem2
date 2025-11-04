# -*- coding: utf-8 -*-

import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
if TOKEN is None:
    print("エラー: DISCORD_BOT_TOKENが.envファイルに設定されていません。")
    print("README.mdの手順に従って.envファイルを作成してください。")
    exit()

# ボットのインテントを設定
# サーバーのメンバーに関する情報を取得するためにintents.membersを有効にする
intents = discord.Intents.default()
# スラッシュコマンドでは message_content は通常不要だが、将来的な機能のために残すことも可能
# intents.message_content = True
intents.members = True # Userinfoコマンドなどで必要になる可能性

# --- 修正箇所: command_prefix を削除 ---
# commands.Botからdiscord.Clientに変更しても良いが、Cogの仕組みを使うためにBotのままにする
# スラッシュコマンドのみを使用するため、プレフィックスは不要
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None) # help_command=None で組み込みヘルプを無効化
# --- 修正ここまで ---


@bot.event
async def on_ready():
    """ボットがログインしたときに呼び出されるイベント"""
    print(f'{bot.user.name} としてログインしました')
    print('------')
    # Cogsの読み込み
    await load_cogs()
    # スラッシュコマンドをDiscordに同期（登録）する
    # 起動時に毎回同期するのは推奨されない場合もあるが、開発中は便利
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)}個のスラッシュコマンドを同期しました。")
    except Exception as e:
        print(f"スラッシュコマンドの同期中にエラーが発生しました: {e}")
    print('------')
    print("ボットの準備が完了しました。")

# --- 修正箇所: Cog読み込みロジック ---
async def load_cogs():
    """cogsフォルダから拡張機能（Cog）を読み込む"""
    print("Cogsを読み込んでいます...")
    
    # 読み込むべきCogのファイル名を明示的に指定する
    # これにより、'beat.py' や 'mania.py' などのライブラリを
    # Cogとして読み込もうとするのを防ぎます。
    cogs_to_load = [
        'convert_cog',  # (ハイブリッド版)
        'malody_cog',   # (ログより)
        'music_cog'     # (ログより)
        # 他に 'setup' 関数を持つCogファイルがあればここに追加
    ]

    # cogs ディレクトリにある全ファイル
    all_files_in_cogs = []
    if os.path.exists('./cogs'):
        all_files_in_cogs = os.listdir('./cogs')
    else:
        print("[エラー] 'cogs' ディレクトリが見つかりません。")
        return

    for filename in all_files_in_cogs:
        cog_name = filename[:-3] # .py を除いた名前
        
        # 読み込むべきリストに含まれていなければスキップ
        if filename.endswith('.py') and cog_name in cogs_to_load:
            try:
                await bot.load_extension(f'cogs.{cog_name}')
                print(f'- {filename} を読み込みました。')
            except Exception as e:
                print(f'[エラー] {filename} の読み込みに失敗しました: {e}')
                print(f"Traceback: {e.__traceback__}")
        
        # ライブラリファイル (beat.py など) や、リストに含まれないファイル
        # (red_to_green.py など) は、読み飛ばされる。
        elif filename.endswith('.py') and not filename.startswith('_'):
            print(f"  (i) {filename} はライブラリまたは対象外のため、Cogとして読み込みません。")

# --- 修正ここまで ---


# --- ボットの管理用コマンド (スラッシュコマンド) ---
# 開発中にコードを修正した際、ボットを再起動せずにCogsをリロードできる
@bot.tree.command(name="reload", description="指定したCogを再読み込みします。")
@commands.is_owner() # ボットのオーナーのみ実行可能
async def reload(interaction: discord.Interaction, cog_name: str):
    """指定したCogをリロードするスラッシュコマンド"""
    try:
        await bot.reload_extension(f"cogs.{cog_name}")
        await interaction.response.send_message(f"`cogs.{cog_name}` をリロードしました。", ephemeral=True) # ephemeral=True で本人にのみ表示
    except commands.ExtensionNotLoaded:
        await interaction.response.send_message(f"`cogs.{cog_name}` は読み込まれていません。", ephemeral=True)
    except commands.ExtensionNotFound:
        await interaction.response.send_message(f"`cogs.{cog_name}` が見つかりません。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"リロード中にエラーが発生しました: `{e}`", ephemeral=True)


# --- ボットの起動 ---
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("エラー: トークンが不正です。")
        print(".envファイルの DISCORD_BOT_TOKEN を確認してください。")
    except Exception as e:
        print(f"ボットの実行中に予期せぬエラーが発生しました: {e}")
