# -*- coding: utf-8 -*-

import discord
from discord import app_commands # スラッシュコマンドのために必要
from discord.ext import commands
import yt_dlp
import os
import asyncio
import traceback
import aiohttp
import re
import io
# BPM関連のインポートを削除 (librosa, numpy, tempo)

# 一時ファイルを保存するディレクトリ名を定義
TEMP_DIR = "temp_audio"

class MusicCog(commands.Cog):
    """MP3/MP4のダウンロードと変換機能を提供するCog""" # 説明を修正
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http_session = aiohttp.ClientSession()
        os.makedirs(TEMP_DIR, exist_ok=True)
        print("- music_cog.py を読み込みました。")

    async def cog_unload(self):
        await self.http_session.close()

    # -----------------------------------------------------------------
    # BPM分析関連の関数 (_download_audio_for_wav_analysis, _analyze_bpm_sync, _create_bpm_embed) を削除
    # -----------------------------------------------------------------
    
    # -----------------------------------------------------------------
    # MP3/MP4 コマンドのコアロジック (BPM関連部分を削除)
    # -----------------------------------------------------------------

    async def _download_thumbnail(self, url):
        # ... (変更なし) ...
        try:
            async with self.http_session.get(url) as resp:
                if resp.status == 200:
                    return io.BytesIO(await resp.read())
        except Exception as e:
            print(f"サムネイルのダウンロードに失敗: {e}")
            return None

    async def _download_and_process_media(
        self,
        interaction: discord.Interaction,
        url: str,
        is_mp3: bool,
        get_thumbnail: bool
        # analyze_bpm パラメータを削除
    ):
        """
        指定されたURLからメディアを処理する共通関数 (スラッシュコマンド対応)
        """
        await interaction.response.defer(thinking=True)

        # 1. yt-dlpで情報取得
        ydl_opts_info = {'quiet': True, 'no_warnings': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as e:
                await interaction.followup.send(f"エラー: URLから情報を取得できませんでした。\n`{e}`")
                return

        title = info.get('title', 'unknown_title')
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        thumbnail_url = info.get('thumbnail')

        # 2. メディアのダウンロード (MP3 or MP4)
        await interaction.edit_original_response(content=f"処理中です... `{title}` をダウンロード・変換しています。")

        temp_filename = os.path.join(TEMP_DIR, f"{safe_title}_{os.urandom(4).hex()}")

        if is_mp3:
            ydl_opts_download = {
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
                'outtmpl': temp_filename, 'quiet': True, 'no_warnings': True
            }
            final_extension = ".mp3"
        else:
            ydl_opts_download = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'outtmpl': temp_filename, 'quiet': True, 'no_warnings': True
            }
            final_extension = None

        downloaded_filepath = None
        try:
            loop = self.bot.loop
            download_info = await loop.run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(ydl_opts_download).extract_info(url, download=True)
            )

            downloaded_filepath = download_info.get('requested_downloads', [{}])[0].get('filepath')
            if not downloaded_filepath or not os.path.exists(downloaded_filepath):
                 if final_extension:
                     downloaded_filepath = temp_filename + final_extension
                 else:
                     potential_ext = download_info.get('ext')
                     if potential_ext: downloaded_filepath = temp_filename + "." + potential_ext
                     else:
                         found_files = [f for f in os.listdir(TEMP_DIR) if f.startswith(os.path.basename(temp_filename))]
                         if found_files: downloaded_filepath = os.path.join(TEMP_DIR, found_files[0])
                         else: raise FileNotFoundError("ダウンロードされたファイルが見つかりません。")

            if not os.path.exists(downloaded_filepath):
                 raise FileNotFoundError(f"ファイルが見つかりません: {downloaded_filepath}")

            # 3. ファイル送信の準備
            files_to_send = []
            final_filename = f"{safe_title}{os.path.splitext(downloaded_filepath)[1]}"
            main_file = discord.File(downloaded_filepath, filename=final_filename)
            files_to_send.append(main_file)

            thumbnail_file = None
            if get_thumbnail and thumbnail_url:
                thumb_data = await self._download_thumbnail(thumbnail_url)
                if thumb_data:
                    thumbnail_file = discord.File(thumb_data, filename="thumbnail.jpg")
                    files_to_send.append(thumbnail_file)

            # 4. ファイル送信
            await interaction.edit_original_response(content=f"処理完了！ `{title}` をアップロードしています...")
            await interaction.followup.send(files=files_to_send)
            await interaction.edit_original_response(content=f"✅ `{title}` の送信が完了しました。")

            # 5. BPM分析の実行部分を削除

        except Exception as e:
            print(traceback.format_exc())
            try:
                await interaction.edit_original_response(content=f"エラー: メディアの処理中に予期せぬ問題が発生しました。\n`{e}`")
            except discord.NotFound:
                 await interaction.followup.send(f"エラー: メディアの処理中に予期せぬ問題が発生しました。\n`{e}`", ephemeral=True)

        finally:
            if downloaded_filepath and os.path.exists(downloaded_filepath):
                try:
                    os.remove(downloaded_filepath)
                except OSError as e:
                    print(f"一時ファイル {downloaded_filepath} の削除に失敗: {e}")


    # --- スラッシュコマンド定義 (bpmオプションを削除) ---
    @app_commands.command(name="mp3", description="YouTube等のURLから音声をMP3として抽出します。")
    @app_commands.describe(
        url="音声に変換したい動画や音楽のURL",
        thumbnail="Trueにすると、動画のサムネイルも一緒に送信します。"
        # bpmオプションを削除
    )
    async def mp3_slash(self, interaction: discord.Interaction, url: str, thumbnail: bool = False): # bpmパラメータを削除
        try:
            # analyze_bpm=False を削除
            await self._download_and_process_media(interaction, url, is_mp3=True, get_thumbnail=thumbnail)
        except Exception as e:
            error_message = f"予期せぬエラーが発生しました: {e}"
            if not interaction.response.is_done():
                 await interaction.response.send_message(error_message, ephemeral=True)
            else:
                 await interaction.followup.send(error_message, ephemeral=True)

    @app_commands.command(name="mp4", description="YouTube等のURLから動画をMP4(または最適形式)でダウンロードします。")
    @app_commands.describe(
        url="ダウンロードしたい動画のURL",
        thumbnail="Trueにすると、動画のサムネイルも一緒に送信します。"
        # bpmオプションを削除
    )
    async def mp4_slash(self, interaction: discord.Interaction, url: str, thumbnail: bool = False): # bpmパラメータを削除
        try:
            # analyze_bpm=False を削除
            await self._download_and_process_media(interaction, url, is_mp3=False, get_thumbnail=thumbnail)
        except Exception as e:
            error_message = f"予期せぬエラーが発生しました: {e}"
            if not interaction.response.is_done():
                 await interaction.response.send_message(error_message, ephemeral=True)
            else:
                 await interaction.followup.send(error_message, ephemeral=True)

# Cogセットアップ関数
async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))

