# -*- coding: utf-8 -*-

import discord
from discord.ext import commands
import yt_dlp
import os
import asyncio
import uuid
import re # 正規表現ライブラリ
import aiohttp # サムネイルダウンロード用

# 一時ファイルを保存するディレクトリ名を定義
TEMP_DIR = "temp_audio"
# Discordのファイルサイズ上限 (無料枠 25MB)
DISCORD_FILE_LIMIT = 26214400 

class MusicCog(commands.Cog):
    """音楽・動画関連のコマンドをまとめたCog"""
    def __init__(self, bot):
        self.bot = bot
        # ボット起動時に一時フォルダを作成
        os.makedirs(TEMP_DIR, exist_ok=True)
        # aiohttpのセッションを初期化
        self.http_session = aiohttp.ClientSession()

    async def cog_unload(self):
        # Cogがアンロードされるときにセッションを閉じる
        await self.http_session.close()

    # -----------------------------------------------------------------
    # 内部処理用の共通メソッド
    # -----------------------------------------------------------------
    async def _download_and_process_media(self, ctx, url: str, is_mp3: bool, get_thumbnail: bool):
        """
        動画のダウンロード、変換、送信を行う共通メソッド
        :param is_mp3: TrueならMP3、FalseならMP4
        :param get_thumbnail: Trueならサムネイルも送信
        """
        processing_message = await ctx.reply("処理中です... URLから情報を取得しています。")
        
        # サーバー側のファイル名は安全なUUIDを使用
        temp_filename_base = str(uuid.uuid4())
        temp_filepath = os.path.join(TEMP_DIR, temp_filename_base)
        final_filepath = ""
        thumbnail_filepath = ""
        loop = asyncio.get_running_loop()

        try:
            # 1. まず動画情報をダウンロードせずに取得
            info_opts = {
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
            }
            video_title = "downloaded_media"
            thumbnail_url = None
            
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info_dict = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(url, download=False)
                )
                video_title = info_dict.get('title', video_title)
                thumbnail_url = info_dict.get('thumbnail')
                
            # ファイル名として使えない文字をサニタイズ
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', video_title)
            safe_title = re.sub(r'\s+', ' ', safe_title).strip()
            if not safe_title:
                safe_title = "downloaded_media"
            if len(safe_title) > 80:
                safe_title = safe_title[:80]

            # 2. ダウンロードと変換のオプションを設定
            ydl_opts = {
                'format': 'bestaudio/best' if is_mp3 else 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'outtmpl': temp_filepath,
                'noplaylist': True,
                'quiet': True,
                'no_warnings': True,
            }

            if is_mp3:
                # MP3変換のポストプロセッサを追加
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
                final_extension = ".mp3"
            else:
                # MP4の場合はファイルがそのまま出力される (yt-dlpが拡張子を自動で付加)
                final_extension = ".mp4" # もしくは .webm などになる可能性もある

            await processing_message.edit(content=f"処理中です... 「{video_title}」をダウンロード・変換しています。")

            # 3. ダウンロード実行
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                download_info = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(url, download=True)
                )
            
            # 最終的なファイルパスを特定
            if is_mp3:
                final_filepath = temp_filepath + ".mp3"
            else:
                # MP4の場合、yt-dlpが付加した拡張子を取得する
                final_filepath = download_info.get('requested_downloads')[0].get('filepath', temp_filepath + ".mp4")
                # 拡張子が指定したものでない場合、リネーム（稀なケース対策）
                if not final_filepath.endswith(final_extension):
                     # yt-dlpが .webm などで保存した場合
                     # .mp4 に強制変換する方が安全だが、ここでは簡略化
                     final_extension = "." + final_filepath.split('.')[-1]


            if not os.path.exists(final_filepath):
                raise FileNotFoundError("変換・ダウンロード後のファイルが見つかりませんでした。")
            
            if os.path.getsize(final_filepath) > DISCORD_FILE_LIMIT:
                await processing_message.edit(content=f"エラー: ファイル「{video_title}」はサイズが25MBを超えているため、送信できません。")
                return

            await processing_message.edit(content="処理完了！ファイルを作成しています...")
            
            # 4. 送信するファイルリストを作成
            files_to_send = []
            
            # メインのファイル (MP3 or MP4)
            files_to_send.append(
                discord.File(final_filepath, filename=f"{safe_title}{final_extension}")
            )

            # 5. サムネイル取得オプションの処理
            if get_thumbnail and thumbnail_url:
                try:
                    async with self.http_session.get(thumbnail_url) as resp:
                        if resp.status == 200:
                            thumbnail_data = await resp.read()
                            thumbnail_filepath = os.path.join(TEMP_DIR, f"{temp_filename_base}_thumb.jpg")
                            with open(thumbnail_filepath, 'wb') as f:
                                f.write(thumbnail_data)
                            
                            files_to_send.append(
                                discord.File(thumbnail_filepath, filename=f"{safe_title}_thumbnail.jpg")
                            )
                except Exception as thumb_e:
                    print(f"サムネイルのダウンロードに失敗しました: {thumb_e}")
                    # サムネイルが無くても処理は続行する

            # 6. ファイルをまとめて送信
            await ctx.reply(files=files_to_send)
            await processing_message.delete()

        except yt_dlp.utils.DownloadError as e:
            print(f"yt-dlp Error: {e}")
            error_message = str(e).lower()
            if "age restricted" in error_message:
                await processing_message.edit(content="エラー: この動画は**年齢制限**が設定されているため、処理できません。")
            elif "copyright" in error_message or "this video is unavailable" in error_message:
                await processing_message.edit(content="エラー: **著作権上の問題**、または**動画が非公開**に設定されているため、処理できません。")
            elif "private" in error_message:
                await processing_message.edit(content="エラー: この動画は**非公開**のため、処理できません。")
            elif "geo-restricted" in error_message:
                 await processing_message.edit(content="エラー: この動画は**お住まいの地域では視聴できない**ため、処理できません。")
            else:
                await processing_message.edit(content="エラー: URLの処理に失敗しました。\nサポートされていないサイトか、無効なURLの可能性があります。")

        except FileNotFoundError as e:
            print(f"File Error: {e}")
            await processing_message.edit(content=f"エラー: 変換後のファイルが見つかりませんでした。FFmpegが正しくインストールされているか確認してください。")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            await processing_message.edit(content=f"予期せぬエラーが発生しました。\n`{e}`")
        finally:
            # 処理完了またはエラー時、一時ファイルを削除する
            if final_filepath and os.path.exists(final_filepath):
                try: os.remove(final_filepath)
                except OSError as e: print(f"Error deleting file {final_filepath}: {e}")
            if thumbnail_filepath and os.path.exists(thumbnail_filepath):
                try: os.remove(thumbnail_filepath)
                except OSError as e: print(f"Error deleting file {thumbnail_filepath}: {e}")


    # -----------------------------------------------------------------
    # Discordコマンド
    # -----------------------------------------------------------------

    def _parse_args(self, args):
        """コマンドの引数をパースしてURLとオプションを分離する"""
        url = None
        get_thumbnail = False
        
        for arg in args:
            if arg.lower() in ('--thumb', '-t', '--thumbnail'):
                get_thumbnail = True
            elif 'http://' in arg or 'https://' in arg:
                url = arg
        
        if not url:
            raise commands.BadArgument("URLが見つかりません。")
            
        return url, get_thumbnail

    @commands.command(name="mp3")
    async def mp3_command(self, ctx, *args):
        """
        指定されたURLの音声をMP3に変換します。
        使い方: !mp3 <URL> [--thumb または -t]
        """
        try:
            url, get_thumbnail = self._parse_args(args)
            await self._download_and_process_media(ctx, url, is_mp3=True, get_thumbnail=get_thumbnail)
        except commands.BadArgument as e:
            await ctx.reply(f"エラー: {e}\n使い方: `!mp3 <URL> [--thumb]`")

    @commands.command(name="mp4")
    async def mp4_command(self, ctx, *args):
        """
        指定されたURLの動画をMP4としてダウンロードします。
        使い方: !mp4 <URL> [--thumb または -t]
        """
        try:
            url, get_thumbnail = self._parse_args(args)
            await self._download_and_process_media(ctx, url, is_mp3=False, get_thumbnail=get_thumbnail)
        except commands.BadArgument as e:
            await ctx.reply(f"エラー: {e}\n使い方: `!mp4 <URL> [--thumb]`")


# このCogをボットに読み込ませるためのセットアップ関数
async def setup(bot):
    await bot.add_cog(MusicCog(bot))

