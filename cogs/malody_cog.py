# -*- coding: utf-8 -*-

import discord
from discord.ext import commands
import json
import zipfile
import io
import re
import os
from pydub import AudioSegment
import traceback
import requests
import time
import librosa # ピッチ維持のタイムストレッチに必要
import numpy as np # librosaのデータ処理に必要
import soundfile # タイムストレッチ後の音声書き出しに必要

# 一時ファイルを保存するディレクトリ名を定義
TEMP_DIR = "temp_audio"
# Discordのファイルサイズ上限 (無料枠は8MB (8 * 1024 * 1024 = 8388608 bytes))
DISCORD_FILE_LIMIT = 8388608 

class MalodyCog(commands.Cog):
    """Malodyの譜面レート差分を生成するCog"""
    def __init__(self, bot):
        self.bot = bot
        os.makedirs(TEMP_DIR, exist_ok=True)
        print("- malody_cog.py を読み込みました。")

    # -----------------------------------------------------------------
    # Litterbox アップロード機能
    # -----------------------------------------------------------------
    def _upload_to_litterbox(self, file_bytes, file_name):
        """litterbox.catbox.moeにファイルをアップロードし、ダウンロードURLを返す"""
        try:
            files = {
                'reqtype': (None, 'fileupload'),
                'time': (None, '24h'), # 24時間で削除
                'fileToUpload': (file_name, file_bytes, 'application/zip'),
            }
            response = requests.post('https://litterbox.catbox.moe/resources/internals/api.php', files=files, timeout=300) # 5分タイムアウト
            response.raise_for_status() # エラーチェック
            
            if response.status_code == 200 and response.text.startswith("https://litterbox.catbox.moe/"):
                return response.text
            else:
                raise Exception(f"Litterboxへのアップロードに失敗しました: {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"Litterboxアップロードエラー: {e}")
            raise Exception(f"Litterboxへのファイルアップロードに失敗しました。\n`{e}`")

    # -----------------------------------------------------------------
    # Malody 譜面処理のコアロジック
    # -----------------------------------------------------------------
    
    def _desofflan(self, chart_data):
        """
        譜面データのソフラン（BPM変化）を除去する (JSロジックのPython版)
        """
        new_chart_data = json.loads(json.dumps(chart_data)) # Deep copy
        
        def beat_to_abs(beat):
            if not isinstance(beat, list) or len(beat) != 3: return 0
            return beat[0] * 4 + beat[1] / beat[2] * 4

        time_events = sorted(new_chart_data.get("time", []), key=lambda e: beat_to_abs(e.get("beat", [0,0,1])))
        if len(time_events) == 0 or (len(time_events) == 1 and time_events[0].get("beat") == [0,0,1]):
            return new_chart_data

        notes = sorted(new_chart_data.get("note", []), key=lambda n: beat_to_abs(n.get("beat", [0,0,1])))
        chart_end_beat = beat_to_abs(notes[-1]["beat"]) if notes else beat_to_abs(time_events[-1]["beat"])

        bpm_durations = {}
        last_beat_value = 0
        
        if not time_events[0].get("bpm"):
             raise ValueError("譜面のtimeイベントにBPMが設定されていません。")

        last_bpm = time_events[0]["bpm"]

        for event in time_events:
            current_beat_value = beat_to_abs(event["beat"])
            duration = current_beat_value - last_beat_value
            if duration > 0:
                bpm_durations[last_bpm] = bpm_durations.get(last_bpm, 0) + duration
            last_beat_value = current_beat_value
            last_bpm = event["bpm"]
        
        final_duration = chart_end_beat - last_beat_value
        if final_duration > 0:
            bpm_durations[last_bpm] = bpm_durations.get(last_bpm, 0) + final_duration

        if not bpm_durations:
             main_bpm = time_events[0]["bpm"]
        else:
            main_bpm = float(max(bpm_durations, key=bpm_durations.get))

        new_chart_data["effect"] = [e for e in new_chart_data.get("effect", []) if "scroll" not in e]

        for event in time_events:
            if event.get("bpm", 0) > 0:
                new_chart_data["effect"].append({
                    "beat": event["beat"],
                    "scroll": main_bpm / event["bpm"]
                })
        
        new_chart_data["time"] = [{"beat": [0,0,1], "bpm": main_bpm}]
        return new_chart_data

    def _process_mc_file(self, chart_data, rate, new_audio_name, desofflan, original_bpm):
        """
        .mc (JSON) データ内のBPM、オフセット、ファイル名を変更する
        """
        new_data = json.loads(json.dumps(chart_data)) # Deep copy
        
        if not new_data.get("meta"): new_data["meta"] = {}
        original_version = new_data["meta"].get("version", "")
        clean_version = re.sub(r"\s\([^)]+\)$", "", original_version)
        
        version_suffix = ""
        if desofflan:
            version_suffix += " (De-sofflan"
            if abs(rate - 1.0) > 1e-9:
                version_suffix += f" {rate:.3f}x"
            version_suffix += ")"
        else:
            version_suffix = f" ({rate:.3f}x)"
            
        new_data["meta"]["version"] = f"{clean_version}{version_suffix}"

        if new_data["meta"].get("preview"):
            new_data["meta"]["preview"] = round(new_data["meta"]["preview"] / rate)
        
        if new_data.get("time"):
            for e in new_data["time"]:
                if e.get("bpm"): e["bpm"] *= rate
        if new_data.get("effect"):
            for e in new_data["effect"]:
                if e.get("scroll"): e["scroll"] *= rate
        
        audio_updated = False
        if new_data.get("meta", {}).get("song", {}).get("audio"):
            new_data["meta"]["song"]["audio"] = new_audio_name
            if "offset" in new_data["meta"]["song"]:
                new_data["meta"]["song"]["offset"] = round(new_data["meta"]["song"]["offset"] / rate)
            audio_updated = True
        
        if not audio_updated and new_data.get("note"):
            for note in new_data["note"]:
                if note.get("sound"):
                    note["sound"] = new_audio_name
                    if "offset" in note:
                        note["offset"] = round(note["offset"] / rate)
                    break 

        return new_data

    def _process_audio(self, audio_bytes, audio_format, rate, no_pitch: bool):
        """
        pydub/librosaを使って音声の速度を変更し、MP3にエンコードする
        no_pitch=True の場合は librosa を使ってタイムストレッチ（ピッチ維持）
        no_pitch=False の場合は pydub を使ってリサンプル（ピッチ変更）
        """
        try:
            sound = AudioSegment.from_file(io.BytesIO(audio_bytes), format=audio_format)
        except Exception as e:
            try:
                sound = AudioSegment.from_file(io.BytesIO(audio_bytes))
            except Exception as e2:
                raise ValueError(f"音声ファイルの読み込みに失敗しました (形式: {audio_format})。\nOggやWavの場合、正しく処理できないことがあります。\n詳細: {e2}")

        if not sound.raw_data:
            raise ValueError("無音の音声ファイル、または読み込みに失敗したため処理できません。")

        output_buffer = io.BytesIO()

        if no_pitch:
            # --- ピッチを維持する (librosa タイムストレッチ) ---
            try:
                # 1. pydubからNumpy配列に変換
                y = np.array(sound.get_array_of_samples()).astype(np.float32) / (1 << (sound.sample_width * 8 - 1))
                if sound.channels == 2:
                    y = y.reshape((-1, 2)).T # (n_samples, 2) -> (2, n_samples) [librosa形式]
                
                # 2. タイムストレッチ実行
                y_stretched = librosa.effects.time_stretch(y=y, rate=rate)
                
                # 3. 一時WAVファイルとしてメモリに書き出す
                temp_wav_buffer = io.BytesIO()
                # soundfileは (n_samples, n_channels) 形式を期待
                if y_stretched.ndim == 2:
                    y_stretched_sf = y_stretched.T # (2, n_samples) -> (n_samples, 2)
                else:
                    y_stretched_sf = y_stretched
                    
                soundfile.write(temp_wav_buffer, y_stretched_sf, sound.frame_rate, format='WAV')
                temp_wav_buffer.seek(0)
                
                # 4. WAVをpydubで読み込み、MP3に変換
                stretched_sound = AudioSegment.from_wav(temp_wav_buffer)
                stretched_sound.export(output_buffer, format="mp3", bitrate="192k")
                
            except Exception as e:
                print(f"Librosa/Soundfile タイムストレッチエラー: {e}")
                print(traceback.format_exc())
                raise ValueError(f"ピッチ維持（タイムストレッチ）の変換に失敗しました。\n詳細: {e}")
        
        else:
            # --- ピッチも変更する (pydub リサンプル - 従来の方法) ---
            new_frame_rate = int(sound.frame_rate * rate)
            new_sound = sound._spawn(sound.raw_data, overrides={"frame_rate": new_frame_rate})
            new_sound.export(output_buffer, format="mp3", bitrate="192k")

        return output_buffer.getvalue()

    # -----------------------------------------------------------------
    # Discordコマンド
    # -----------------------------------------------------------------

    @commands.command(name="malody",
    help="""
    添付された.mcz/.zip譜面のレート差分を生成します。
    元の譜面と音源は保持されたまま、新しい差分が追加されます。

    **【基本の使い方】**
    `!malody [レート1] [レート2] ...`
    指定したレートの差分を複数作成します。
    例: `!malody 1.05 1.1 1.15`

    **【オプション】**
    `--desofflan`
    譜面のBPM変化（ソフラン）を除去し、BPMを一定にします。
    レート指定と組み合わせて使用します。
    例: `!malody 1.1 1.2 --desofflan`

    `--desofflan-only`
    レートを変更せず、ソフラン除去のみを行います。(1.0倍速扱い)
    例: `!malody --desofflan-only`

    `--no-pitch` または `-np`
    速度を変更した際に、ピッチ（音の高さ）が変わらないようにします。
    例: `!malody 1.1 --no-pitch`

    `--range [開始] [終了] [刻み幅]`
    指定した範囲のレート差分をまとめて作成します。
    例: `!malody --range 1.05 1.2 0.05`

    `--bpm [BPM1] [BPM2] ...`
    レートの代わりに、目標のBPMを指定します。
    譜面のBPMが150の場合、`--bpm 180 200`と指定すると、1.2倍と1.33倍の差分が作成されます。
    例: `!malody --bpm 180 200 --desofflan`
    """
    )
    async def malody_command(self, ctx, *args):
        """
        添付された.mcz/.zip譜面のレート差分を生成します。
        """
        
        # --- 1. 引数と添付ファイルのバリデーション ---
        if not ctx.message.attachments:
            return await ctx.reply("エラー: `.mcz` または `.zip` ファイルを添付してください。")
        
        attachment = ctx.message.attachments[0]
        if not (attachment.filename.lower().endswith(".mcz") or attachment.filename.lower().endswith(".zip")):
            return await ctx.reply("エラー: 添付ファイルは `.mcz` または `.zip` である必要があります。")

        original_zip_name = attachment.filename
        
        try:
            attachment_bytes = await attachment.read()
        except Exception as e:
            return await ctx.reply(f"エラー: 添付ファイルの読み込みに失敗しました。\n`{e}`")

        # --- 2. 引数のパース ---
        rates_to_generate = []
        desofflan = False
        desofflan_only = False
        target_bpms = []
        is_bpm_mode = False
        no_pitch = False # ピッチ維持フラグ

        try:
            i = 0
            while i < len(args):
                arg = args[i].lower()
                if arg == "--desofflan":
                    desofflan = True
                    i += 1
                elif arg == "--desofflan-only":
                    desofflan_only = True
                    desofflan = True
                    i += 1
                elif arg in ("--no-pitch", "-np"): # <-- NEW
                    no_pitch = True
                    i += 1
                elif arg == "--range":
                    if i + 3 >= len(args): raise ValueError("--range には3つの引数（開始, 終了, 刻み幅）が必要です。")
                    start, end, step = float(args[i+1]), float(args[i+2]), float(args[i+3])
                    if step <= 0: raise ValueError("刻み幅は0より大きい必要があります。")
                    r = start
                    while r <= end + 1e-9:
                        rates_to_generate.append(r)
                        r += step
                    i += 4
                elif arg == "--bpm":
                    is_bpm_mode = True
                    i += 1
                    if i >= len(args) or args[i].startswith("--"): raise ValueError("--bpm には少なくとも1つのBPM値を指定が必要です。")
                    while i < len(args) and not args[i].startswith("--"):
                        target_bpms.append(float(args[i]))
                        i += 1
                else:
                    rates_to_generate.append(float(arg))
                    i += 1
            
            if not rates_to_generate and not target_bpms and not desofflan_only:
                raise ValueError("レート、BPM、または `--desofflan-only` のいずれかを指定してください。")
            if desofflan_only:
                rates_to_generate.append(1.0) # ソフラン除去のみ

        except Exception as e:
            return await ctx.reply(f"エラー: コマンドの引数が正しくありません。\n`{e}`\n\n**使い方:** `!malody [レート/オプション] (譜面ファイルを添付)`\n**例:** `!malody 1.1 1.2 --desofflan`\n`!help malody` で詳細を確認できます。")

        await ctx.message.add_reaction("⏳") # 処理中リアクション
        processing_message = await ctx.reply(f"処理中です... `{original_zip_name}` を解析しています。")

        try:
            # --- 3. メインのZIP処理 ---
            input_zip_buffer = io.BytesIO(attachment_bytes)
            output_zip_buffer = io.BytesIO()

            charts_to_process = []
            audio_files = {} # audio_name: audio_bytes
            original_files = {} # file_name: file_bytes

            with zipfile.ZipFile(input_zip_buffer, 'r') as in_zip:
                for item in in_zip.infolist():
                    if item.is_dir():
                        continue
                    
                    file_name = item.filename
                    if file_name.startswith("__MACOSX/"):
                        continue
                        
                    file_bytes = in_zip.read(file_name)
                    original_files[file_name] = file_bytes # すべての元のファイルを保存

                    if file_name.lower().endswith(".mc"):
                        try:
                            chart_data = json.loads(file_bytes.decode('utf-8'))
                        except Exception as e:
                            await ctx.send(f"警告: 譜面ファイル `{file_name}` はJSONとして解析できませんでした。スキップします。\n`{e}`")
                            continue
                            
                        audio_file_name = chart_data.get("meta", {}).get("song", {}).get("audio")
                        if not audio_file_name:
                            for note in chart_data.get("note", []):
                                if note.get("sound"):
                                    audio_file_name = note["sound"]
                                    break
                        
                        original_bpm = 0
                        if chart_data.get("time") and len(chart_data["time"]) > 0:
                            original_bpm = chart_data["time"][0].get("bpm", 0)

                        charts_to_process.append({
                            "name": file_name,
                            "data": chart_data,
                            "audio_name": audio_file_name,
                            "original_bpm": original_bpm
                        })
                    
                    elif file_name.lower().endswith(('.mp3', '.ogg', '.wav')):
                        audio_files[file_name] = file_bytes
            
            if not charts_to_process:
                raise ValueError("`.mcz` ファイル内に `.mc` 譜面ファイルが見つかりません。")

            # --- 4. 出力ZIPの作成 ---
            with zipfile.ZipFile(output_zip_buffer, 'w', zipfile.ZIP_DEFLATED) as out_zip:
                # 最初に、元のファイルをすべて出力ZIPに書き込む
                for file_name, file_bytes in original_files.items():
                    out_zip.writestr(file_name, file_bytes)
                
                final_rates = set()
                if is_bpm_mode:
                    for chart in charts_to_process:
                        if chart["original_bpm"] > 0:
                            for bpm in target_bpms:
                                final_rates.add(bpm / chart["original_bpm"])
                        else:
                            await ctx.send(f"警告: 譜面 `{chart['name']}` のBPMが不明なため、BPM指定の差分を作成できません。")
                final_rates.update(rates_to_generate)
                
                if not final_rates:
                    raise ValueError("有効なレートが生成されませんでした。")

                final_rates = sorted(list(final_rates))
                
                await processing_message.edit(content=f"処理中です... {len(charts_to_process)}譜面 x {len(final_rates)}レート = 計{len(charts_to_process) * len(final_rates)}差分を生成します。")
                
                total_charts_processed = 0
                
                # --- 5. レートごとに譜面と音声を処理 ---
                for chart in charts_to_process:
                    if not chart["audio_name"] or chart["audio_name"] not in audio_files:
                        await ctx.send(f"警告: 譜面 `{chart['name']}` に対応する音源 `{chart['audio_name']}` が見つからないため、スキップします。")
                        continue
                    
                    audio_bytes = audio_files[chart["audio_name"]]
                    audio_format = chart["audio_name"].rsplit('.', 1)[-1].lower()
                    
                    base_chart_data = self._desofflan(chart["data"]) if desofflan else chart["data"]

                    for rate in final_rates:
                        # 1.0倍かつソフラン除去なしの差分はスキップ (元ファイルが既にあるため)
                        if abs(rate - 1.0) < 1e-9 and not desofflan:
                            continue 
                        
                        try:
                            # 音声を処理 (no_pitchフラグを渡す)
                            new_audio_bytes = self._process_audio(audio_bytes, audio_format, rate, no_pitch)
                            
                            new_audio_name = chart["audio_name"].rsplit('.', 1)[0] + f"_rate{rate:.3f}x.mp3"
                            
                            new_mc_data = self._process_mc_file(base_chart_data, rate, new_audio_name, desofflan, chart["original_bpm"])
                            new_mc_name = chart["name"].rsplit('.', 1)[0] + f"_{'desofflan_' if desofflan else ''}rate{rate:.3f}x.mc"
                            
                            # 新しい差分ファイルを追加 (元ファイルはすでにあるので上書きではない)
                            out_zip.writestr(new_audio_name, new_audio_bytes)
                            out_zip.writestr(new_mc_name, json.dumps(new_mc_data, indent=2).encode('utf-8'))
                            
                            total_charts_processed += 1
                        
                        except Exception as process_e:
                            await ctx.send(f"警告: レート `{rate:.3f}x` の譜面 `{chart['name']}` の処理中にエラーが発生しました。スキップします。\n`{process_e}`")
                            print(traceback.format_exc())


            # --- 6. 結果を送信 ---
            
            if total_charts_processed == 0:
                # 1.0倍速のみが指定された場合など
                if len(final_rates) > 0 and (all(abs(r - 1.0) < 1e-9 for r in final_rates) and not desofflan):
                     raise ValueError("1.0倍速（ソフラン除去なし）の差分はスキップされました。元のファイルが保持されています。")
                else:
                    raise ValueError("処理できる有効な差分がありませんでした。")

            output_zip_buffer.seek(0)
            file_bytes = output_zip_buffer.getvalue()
            file_size = len(file_bytes)
            new_zip_name = original_zip_name.rsplit('.', 1)[0] + "_rate_pack.mcz"

            if file_size > DISCORD_FILE_LIMIT:
                # --- ファイルサイズが上限を超える場合: Litterboxにアップロード ---
                await processing_message.edit(content=f"処理完了！合計 {total_charts_processed} 個の差分を追加しました。\nファイルサイズが8MBを超えたため、一時ホスティングサービスにアップロードしています...")
                
                try:
                    loop = self.bot.loop
                    download_url = await loop.run_in_executor(
                        None,
                        self._upload_to_litterbox,
                        file_bytes,
                        new_zip_name
                    )
                    
                    embed = discord.Embed(
                        title="譜面パックの準備ができました（大容量）",
                        description=f"ファイルサイズがDiscordの上限を超えたため、一時ダウンロードリンクを生成しました。\n**[ここをクリックしてダウンロード]({download_url})**",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="ファイル名", value=new_zip_name)
                    embed.add_field(name="サイズ", value=f"{file_size / (1024*1024):.2f} MB")
                    embed.set_footer(text="※リンクはLitterboxのサーバーポリシーに基づき、24時間後に自動的に削除されます。")
                    
                    await processing_message.edit(content=None, embed=embed)
                    await ctx.message.remove_reaction("⏳", self.bot.user)

                except Exception as upload_e:
                    await processing_message.edit(content=f"エラー: {total_charts_processed}個の差分を追加しましたが、ファイルサイズが大きすぎ（{file_size / (1024*1024):.2f} MB）、一時ホスティングサービスへのアップロードにも失敗しました。\n`{upload_e}`")
                    await ctx.message.add_reaction("❌")
            
            else:
                # --- ファイルサイズが上限内の場合: 通常通り添付 ---
                await processing_message.edit(content=f"処理完了！合計 {total_charts_processed} 個の差分を追加しました。ファイルを送信します。")
                await ctx.reply(file=discord.File(io.BytesIO(file_bytes), filename=new_zip_name))
                await ctx.message.remove_reaction("⏳", self.bot.user)
                

        except Exception as e:
            print(traceback.format_exc())
            await processing_message.edit(content=f"エラー: 譜面の処理中に予期せぬ問題が発生しました。\n`{e}`")
            await ctx.message.remove_reaction("⏳", self.bot.user)
            await ctx.message.add_reaction("❌")


# このCogをボットに読み込ませるためのセットアップ関数
async def setup(bot):
    await bot.add_cog(MalodyCog(bot))

