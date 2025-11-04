# -*- coding: utf-8 -*-

import discord
from discord import app_commands
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
import librosa
import numpy as np
import soundfile
import aiohttp
from urllib.parse import urlparse
from typing import List, Dict, Any, Tuple # 型ヒントのため

# 一時ファイルを保存するディレクトリ名を定義
TEMP_DIR = "temp_audio"
# Discordのファイルサイズ上限
DISCORD_FILE_LIMIT = 8388608
# URLからのダウンロード時の最大ファイルサイズ
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024

# --- 譜面選択用のUIコンポーネント ---
class ChartSelectDropdown(discord.ui.Select):
    def __init__(self, chart_options: List[Tuple[str, str]], view_ref: 'ChartSelectView'):
        options = [
            discord.SelectOption(label=f"{i+1}. {version}"[:100], value=path) # labelは100文字制限
            for i, (path, version) in enumerate(chart_options)
        ]
        super().__init__(placeholder="処理したい譜面を選択してください...", min_values=1, max_values=1, options=options)
        self.view_ref = view_ref # 親Viewへの参照

    async def callback(self, interaction: discord.Interaction):
        selected_chart_path = self.values[0]
        await interaction.response.edit_message(content=f"`{os.path.basename(selected_chart_path)}` を選択しました。処理を開始します...", view=None)
        
        # --- 修正: 親Viewの process_selected_chart に interaction を渡す ---
        await self.view_ref.process_selected_chart(interaction, selected_chart_path)

class ChartSelectView(discord.ui.View):
    # --- 修正: __init__ で target_bpms を受け取る ---
    def __init__(self, *, timeout=180, chart_options: List[Tuple[str, str]], 
                 original_zip_name: str, attachment_bytes: bytes, 
                 final_rates: List[float], target_bpms: List[float], # <-- target_bpms を追加
                 desofflan: bool, no_pitch: bool, is_bpm_mode: bool,
                 cog_ref: 'MalodyCog'):
        super().__init__(timeout=timeout)
        self.chart_options = chart_options
        self.original_zip_name = original_zip_name
        self.attachment_bytes = attachment_bytes
        self.final_rates_base = final_rates # レート指定
        self.target_bpms = target_bpms # BPM指定
        self.desofflan = desofflan
        self.no_pitch = no_pitch
        self.is_bpm_mode = is_bpm_mode
        self.cog_ref = cog_ref
        self.interaction_to_edit: discord.Interaction = None

        self.add_item(ChartSelectDropdown(chart_options, self))

    async def on_timeout(self):
        if self.interaction_to_edit:
            await self.interaction_to_edit.edit_original_response(content="譜面選択がタイムアウトしました。", view=None)

    async def process_selected_chart(self, interaction: discord.Interaction, selected_chart_path: str):
        """選択された譜面の処理を実行する"""
        try:
            # --- 修正: 選択された譜面の情報を使って final_rates をここで計算 ---
            selected_chart_info = self.cog_ref.get_chart_info_from_zip(self.attachment_bytes, selected_chart_path)
            
            final_rates = set(self.final_rates_base) # レート指定を初期値とする
            if self.is_bpm_mode:
                if selected_chart_info["original_bpm"] > 0:
                    for tbpm in self.target_bpms: final_rates.add(tbpm / selected_chart_info["original_bpm"])
                else:
                    await interaction.followup.send(f"警告: 選択譜面 `{os.path.basename(selected_chart_path)}` BPM不明のためBPM指定不可。", ephemeral=True)
                    if not final_rates: # BPM指定しかなく、変換もできなかった場合
                         raise ValueError("BPM指定がありましたが、選択譜面のBPM不明なためレート計算不可。")

            if not final_rates: raise ValueError("有効なレートが生成されませんでした。")
            final_rates_list = sorted(list(final_rates))
            # ---

            await self.cog_ref.run_malody_processing(
                interaction, self.original_zip_name, self.attachment_bytes, 
                selected_chart_path, 
                final_rates_list, # <-- 計算済みのレートリストを渡す
                self.desofflan, self.no_pitch, self.is_bpm_mode,
                selected_chart_info # <-- 譜面情報も渡す
            )
        except Exception as e:
            print(traceback.format_exc())
            await interaction.followup.send(f"エラー: 選択された譜面の処理中に予期せぬ問題が発生しました。\n`{e}`")


class MalodyCog(commands.Cog):
    """Malodyの譜面レート差分を生成するCog"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http_session = aiohttp.ClientSession()
        os.makedirs(TEMP_DIR, exist_ok=True)
        print("- malody_cog.py を読み込みました。")

    async def cog_unload(self):
        await self.http_session.close()

    # -----------------------------------------------------------------
    # Helper Functions (変更なし、省略)
    # -----------------------------------------------------------------
    def _upload_to_litterbox(self, file_bytes, file_name):
        # ... (変更なし) ...
        try:
            files = { 'reqtype': (None, 'fileupload'), 'time': (None, '24h'), 'fileToUpload': (file_name, file_bytes, 'application/zip'), }
            response = requests.post('https://litterbox.catbox.moe/resources/internals/api.php', files=files, timeout=300)
            response.raise_for_status(); response_text = response.text
            if response.status_code == 200 and (response_text.startswith("https://litterbox.catbox.moe/") or response_text.startswith("https://litter.catbox.moe/")): return response_text
            else: raise Exception(f"Litterboxアップロード失敗: {response_text}")
        except requests.exceptions.RequestException as e: print(f"Litterboxアップロードエラー: {e}"); raise Exception(f"Litterboxアップロード失敗。\n`{e}`")

    def _desofflan(self, chart_data):
        # ... (変更なし) ...
        new_chart_data = json.loads(json.dumps(chart_data)); beat_to_abs = lambda beat: beat[0] * 4 + beat[1] / beat[2] * 4 if isinstance(beat, list) and len(beat) == 3 else 0
        time_events = sorted(new_chart_data.get("time", []), key=lambda e: beat_to_abs(e.get("beat", [0,0,1])))
        if not time_events or (len(time_events) == 1 and time_events[0].get("beat") == [0,0,1]): new_chart_data["effect"] = [e for e in new_chart_data.get("effect", []) if "scroll" not in e]; return new_chart_data
        notes = sorted(new_chart_data.get("note", []), key=lambda n: beat_to_abs(n.get("beat", [0,0,1]))); chart_end_beat = beat_to_abs(notes[-1]["beat"]) if notes else beat_to_abs(time_events[-1]["beat"])
        bpm_durations = {}; last_beat_value = 0
        if not time_events[0].get("bpm"): raise ValueError("timeイベントにBPM未設定。"); last_bpm = time_events[0]["bpm"]
        for event in time_events:
            current_beat_value = beat_to_abs(event["beat"]); duration = current_beat_value - last_beat_value
            if duration > 0: bpm_durations[last_bpm] = bpm_durations.get(last_bpm, 0) + duration
            last_beat_value = current_beat_value; last_bpm = event.get("bpm", last_bpm)
        final_duration = chart_end_beat - last_beat_value; 
        if final_duration > 0: bpm_durations[last_bpm] = bpm_durations.get(last_bpm, 0) + final_duration
        main_bpm = time_events[0]["bpm"] if not bpm_durations else float(max(bpm_durations, key=bpm_durations.get))
        new_chart_data["effect"] = [e for e in new_chart_data.get("effect", []) if "scroll" not in e]
        for event in time_events:
            if event.get("bpm", 0) > 0: new_chart_data["effect"].append({ "beat": event["beat"], "scroll": main_bpm / event["bpm"] })
        return new_chart_data

    def _process_mc_file(self, chart_data, rate, new_audio_name, desofflan, original_bpm):
        # ... (変更なし) ...
        new_data = json.loads(json.dumps(chart_data)); new_data["meta"] = new_data.get("meta", {}); original_version = new_data["meta"].get("version", ""); clean_version = re.sub(r"\s\([^)]+\)$", "", original_version)
        version_suffix = ""; is_desofflan_only = desofflan and abs(rate - 1.0) < 1e-9
        if desofflan:
            version_suffix += " (De-sofflan"; 
            if not is_desofflan_only: version_suffix += f" {rate:.3f}x"
            version_suffix += ")"
        else: version_suffix = f" ({round(original_bpm * rate)}bpm)" if 'is_bpm_mode_flag' in new_data and new_data['is_bpm_mode_flag'] and original_bpm > 0 else f" ({rate:.3f}x)"
        new_data["meta"]["version"] = f"{clean_version}{version_suffix}"
        if new_data["meta"].get("preview"): new_data["meta"]["preview"] = round(new_data["meta"]["preview"] / rate)
        if new_data.get("time"):
            if not desofflan:
                for e in new_data["time"]:
                    if e.get("bpm"): e["bpm"] *= rate
        if new_data.get("effect"):
            if desofflan:
                 for e in new_data["effect"]:
                     if e.get("scroll"): e["scroll"] *= rate
        audio_basename = os.path.basename(new_audio_name); audio_updated = False
        
        # --- 修正箇所: インデントバグの修正 ---
        if new_data.get("meta", {}).get("song", {}).get("audio"):
            new_data["meta"]["song"]["audio"] = audio_basename
            if "offset" in new_data["meta"]["song"]:
                new_data["meta"]["song"]["offset"] = round(new_data["meta"]["song"]["offset"] / rate)
            audio_updated = True
        
        if not audio_updated and new_data.get("note"):
            for note in new_data["note"]:
                if note.get("sound"):
                    note["sound"] = audio_basename
                    # オフセット更新とbreakを、if note.get("sound") ブロックの中に正しくインデント
                    if "offset" in note:
                        note["offset"] = round(note["offset"] / rate)
                    break # soundを持つ最初のノーツのみ更新してループを抜ける
        # --- 修正ここまで ---

        if 'is_bpm_mode_flag' in new_data: del new_data['is_bpm_mode_flag']
        return new_data

    def _process_audio(self, audio_bytes, audio_format, rate, no_pitch: bool):
        # ... (変更なし) ...
        try: sound = AudioSegment.from_file(io.BytesIO(audio_bytes), format=audio_format)
        except Exception as e:
            try: sound = AudioSegment.from_file(io.BytesIO(audio_bytes))
            except Exception as e2: raise ValueError(f"音声読込失敗(形式: {audio_format})。\n詳細: {e2}")
        if not sound.raw_data: raise ValueError("無音ファイル/読込失敗。")
        output_buffer = io.BytesIO()
        if no_pitch:
            try:
                y = np.array(sound.get_array_of_samples()).astype(np.float32) / (1 << (sound.sample_width * 8 - 1)); y = y.reshape((-1, 2)).T if sound.channels == 2 else y
                y_stretched = librosa.effects.time_stretch(y=y, rate=rate); temp_wav_buffer = io.BytesIO(); y_stretched_sf = y_stretched.T if y_stretched.ndim == 2 else y_stretched
                soundfile.write(temp_wav_buffer, y_stretched_sf, sound.frame_rate, format='WAV'); temp_wav_buffer.seek(0)
                stretched_sound = AudioSegment.from_wav(temp_wav_buffer); stretched_sound.export(output_buffer, format="mp3", bitrate="192k")
            except Exception as e: print(f"Librosa/Soundfileエラー: {e}"); print(traceback.format_exc()); raise ValueError(f"ピッチ維持変換失敗。\n詳細: {e}")
        else: new_frame_rate = int(sound.frame_rate * rate); new_sound = sound._spawn(sound.raw_data, overrides={"frame_rate": new_frame_rate}); new_sound.export(output_buffer, format="mp3", bitrate="192k")
        return output_buffer.getvalue()

    async def _download_file_from_url(self, url: str) -> tuple[bytes, str]:
        # ... (変更なし) ...
        try:
            async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status == 200:
                    cl = resp.headers.get('Content-Length'); 
                    if cl and int(cl) > MAX_DOWNLOAD_SIZE: raise ValueError(f"ファイルサイズ超過 ({int(cl)/(1024*1024):.1f}MB)。上限{MAX_DOWNLOAD_SIZE/(1024*1024):.0f}MB。")
                    fb = await resp.read(); fn = "unknown_file.zip"; cd = resp.headers.get('Content-Disposition')
                    if cd: p = cd.split('filename='); fn = p[1].strip('" ') if len(p) > 1 else fn
                    else: pu = urlparse(url); pf = os.path.basename(pu.path); fn = pf if pf else fn
                    if not (fn.lower().endswith((".mcz", ".zip"))): fn = fn + ".zip" if "." not in fn else fn; raise ValueError(f"DLファイル拡張子不正: {fn}")
                    return fb, fn
                else: raise ValueError(f"URLからのDL失敗 (HTTP: {resp.status})。")
        except asyncio.TimeoutError: raise ValueError("URLからのDLタイムアウト (5分)。")
        except aiohttp.ClientError as e: raise ValueError(f"URL接続/DLエラー。\n`{e}`")
        except Exception as e: raise ValueError(f"URL処理中予期せぬエラー。\n`{e}`")

    # --- ZIP読み込みヘルパー (変更なし) ---
    def get_chart_info_from_zip(self, attachment_bytes: bytes, chart_path: str = None) -> Dict[str, Any]:
        # ... (変更なし) ...
        input_zip_buffer = io.BytesIO(attachment_bytes); all_charts_info = []
        try:
            with zipfile.ZipFile(input_zip_buffer, 'r') as in_zip:
                for item in in_zip.infolist():
                    if item.is_dir(): continue
                    try: file_path_utf8 = item.filename.encode('cp437').decode('utf-8')
                    except UnicodeDecodeError:
                        try: file_path_utf8 = item.filename.encode('cp437').decode('shift-jis')
                        except UnicodeDecodeError: continue
                    if file_path_utf8.startswith("__MACOSX/"): continue
                    if file_path_utf8.lower().endswith(".mc"):
                        file_bytes = in_zip.read(item.filename)
                        try: chart_data = json.loads(file_bytes.decode('utf-8'))
                        except Exception: continue
                        version = chart_data.get("meta", {}).get("version", os.path.basename(file_path_utf8))
                        audio_file_name_mc = chart_data.get("meta", {}).get("song", {}).get("audio")
                        if not audio_file_name_mc:
                            for note in chart_data.get("note", []):
                                if note.get("sound"): audio_file_name_mc = note["sound"]; break
                        original_bpm = 0; time_events_mc = chart_data.get("time")
                        if time_events_mc and isinstance(time_events_mc, list) and time_events_mc:
                            if isinstance(time_events_mc[0], dict) and "bpm" in time_events_mc[0]: original_bpm = time_events_mc[0].get("bpm", 0)
                        chart_info = {"path": file_path_utf8, "data": chart_data, "version": version, "audio_name_mc": audio_file_name_mc, "original_bpm": original_bpm}
                        if chart_path and file_path_utf8 == chart_path: return chart_info
                        all_charts_info.append(chart_info)
        except zipfile.BadZipFile: raise ValueError("添付ファイルは有効なZIP/.mczではありません。")
        except Exception as zip_read_e: raise ValueError(f"ZIP/.mcz読込エラー。\n`{zip_read_e}`")
        if not chart_path: return all_charts_info
        raise ValueError(f"選択された譜面 `{chart_path}` がZIP内に見つかりません。")

    # --- Main Processing Function (変更なし、省略) ---
    async def run_malody_processing(self, interaction: discord.Interaction,
                                    original_zip_name: str, attachment_bytes: bytes,
                                    selected_chart_path: str,
                                    final_rates_list: List[float],
                                    desofflan: bool, no_pitch: bool, is_bpm_mode: bool,
                                    selected_chart_info: Dict[str, Any]):
        # ... (変更なし) ...
        try:
            input_zip_buffer = io.BytesIO(attachment_bytes); output_zip_buffer = io.BytesIO(); audio_files = {}; original_files = {}
            try:
                with zipfile.ZipFile(input_zip_buffer, 'r') as in_zip:
                    for item in in_zip.infolist():
                        if item.is_dir(): continue
                        try: file_path_utf8 = item.filename.encode('cp437').decode('utf-8')
                        except UnicodeDecodeError:
                            try: file_path_utf8 = item.filename.encode('cp437').decode('shift-jis')
                            except UnicodeDecodeError: continue
                        if file_path_utf8.startswith("__MACOSX/"): continue
                        file_bytes = in_zip.read(item.filename); original_files[file_path_utf8] = file_bytes
                        if file_path_utf8.lower().endswith(('.mp3', '.ogg', '.wav')): audio_files[file_path_utf8] = file_bytes
            except zipfile.BadZipFile: raise ValueError("添付ファイルは有効なZIP/.mczではありません。")
            except Exception as zip_read_e: raise ValueError(f"ZIP/.mcz読込エラー。\n`{zip_read_e}`")
            chart = selected_chart_info
            with zipfile.ZipFile(output_zip_buffer, 'w', zipfile.ZIP_DEFLATED) as out_zip:
                for file_path, file_bytes in original_files.items(): out_zip.writestr(file_path, file_bytes)
                await interaction.edit_original_response(content=f"処理中... 選択譜面 `{chart['version']}` x {len(final_rates_list)}レート = 計{len(final_rates_list)}差分生成。")
                total_charts_processed = 0
                target_audio_name_mc = chart.get("audio_name_mc"); actual_audio_bytes = None; actual_audio_path = None; actual_audio_format = None; found_audio = False; chart_dir = os.path.dirname(chart['path'])
                if target_audio_name_mc:
                    target_basename = os.path.basename(target_audio_name_mc); potential_path = os.path.join(chart_dir, target_basename) if chart_dir else target_basename
                    if potential_path in audio_files: actual_audio_path = potential_path; actual_audio_bytes = audio_files[actual_audio_path]; found_audio = True
                    if not found_audio:
                        for zip_audio_path, zip_audio_bytes in audio_files.items():
                            if os.path.basename(zip_audio_path) == target_basename: actual_audio_path = zip_audio_path; actual_audio_bytes = zip_audio_bytes; found_audio = True; await interaction.followup.send(f"情報: 譜面 `{os.path.basename(chart['path'])}` 指定音源 `{target_audio_name_mc}` 発見場所: `{os.path.dirname(actual_audio_path)}/`", ephemeral=True); break
                    if not found_audio:
                        base_name_mc = os.path.splitext(target_basename)[0].lower()
                        for zip_audio_path, zip_audio_bytes in audio_files.items():
                            zip_base_name = os.path.splitext(os.path.basename(zip_audio_path))[0].lower()
                            if zip_base_name == base_name_mc: actual_audio_path = zip_audio_path; actual_audio_bytes = zip_audio_bytes; await interaction.followup.send(f"情報: 譜面 `{os.path.basename(chart['path'])}` 指定音源 `{target_audio_name_mc}` 不一致。類似名 `{os.path.basename(actual_audio_path)}` 使用。", ephemeral=True); found_audio = True; break
                if not found_audio and len(audio_files) == 1: actual_audio_path, actual_audio_bytes = list(audio_files.items())[0]; await interaction.followup.send(f"情報: 譜面 `{os.path.basename(chart['path'])}` 音源指定なし/不一致。ZIP内唯一音源 `{os.path.basename(actual_audio_path)}` 使用。", ephemeral=True); found_audio = True
                if not found_audio: raise ValueError(f"譜面 `{os.path.basename(chart['path'])}` 対応音源不明。")
                actual_audio_format = actual_audio_path.rsplit('.', 1)[-1].lower()
                base_chart_data = self._desofflan(chart["data"]) if desofflan else chart["data"]
                base_chart_data['is_bpm_mode_flag'] = is_bpm_mode
                for rate in final_rates_list:
                    if abs(rate - 1.0) < 1e-9 and not desofflan: continue
                    try:
                        new_audio_bytes = self._process_audio(actual_audio_bytes, actual_audio_format, rate, no_pitch)
                        original_audio_dir = os.path.dirname(actual_audio_path); original_mc_dir = os.path.dirname(chart['path'])
                        new_audio_name_base = os.path.basename(actual_audio_path).rsplit('.', 1)[0] + f"_rate{rate:.3f}x.mp3"
                        new_audio_path = os.path.join(original_audio_dir, new_audio_name_base) if original_audio_dir else new_audio_name_base
                        new_mc_data = self._process_mc_file(base_chart_data, rate, new_audio_name_base, desofflan, chart["original_bpm"])
                        new_mc_name_base = chart["path"].rsplit('.', 1)[0] + f"_{'desofflan_' if desofflan else ''}rate{rate:.3f}x.mc"
                        new_mc_path = os.path.join(original_mc_dir, os.path.basename(new_mc_name_base)) if original_mc_dir else os.path.basename(new_mc_name_base)
                        out_zip.writestr(new_audio_path, new_audio_bytes); out_zip.writestr(new_mc_path, json.dumps(new_mc_data, indent=2).encode('utf-8'))
                        total_charts_processed += 1
                    except Exception as process_e:
                        await interaction.followup.send(f"警告: レート `{rate:.3f}x` 譜面 `{os.path.basename(chart['path'])}` 処理失敗。スキップ。\n`{process_e}`", ephemeral=True); print(traceback.format_exc())
            if total_charts_processed == 0:
                if final_rates_list and (all(abs(r - 1.0) < 1e-9 for r in final_rates_list) and not desofflan): raise ValueError("1.0倍速（ソフラン除去なし）差分はスキップ。元ファイル保持。")
                else: raise ValueError("処理できる有効な差分がありませんでした。")
            output_zip_buffer.seek(0); file_bytes = output_zip_buffer.getvalue(); file_size = len(file_bytes)
            pack_name_base = original_zip_name.rsplit('.', 1)[0]; selected_version_display = chart['version']; safe_version = re.sub(r'[\\/*?:"<>|]', "_", selected_version_display)
            new_zip_name = f"{pack_name_base}_{safe_version}_rate_pack.mcz"
            result_msg_prefix = f"処理完了！計 {total_charts_processed} 個の差分を (譜面: `{selected_version_display}`) 追加。"
            if file_size > DISCORD_FILE_LIMIT:
                await interaction.edit_original_response(content=f"{result_msg_prefix}\nファイルサイズ超過(8MB)、アップロード中...")
                try:
                    loop = self.bot.loop; download_url = await loop.run_in_executor(None, self._upload_to_litterbox, file_bytes, new_zip_name)
                    embed = discord.Embed(title="差分生成完了", description=f"**[ここをクリックしてダウンロード]({download_url})**", color=discord.Color.green())
                    embed.add_field(name="ファイル名", value=new_zip_name); embed.add_field(name="サイズ", value=f"{file_size / (1024*1024):.2f} MB"); embed.set_footer(text="※リンクは24時間後に自動削除されます。")
                    await interaction.edit_original_response(content=None, embed=embed, view=None)
                except Exception as upload_e: await interaction.edit_original_response(content=f"エラー: {result_msg_prefix} ファイルサイズ超過({file_size / (1024*1024):.2f} MB)、アップロード失敗。\n`{upload_e}`", view=None)
            else:
                await interaction.edit_original_response(content=f"{result_msg_prefix} ファイル送信中...", view=None)
                await interaction.followup.send(file=discord.File(io.BytesIO(file_bytes), filename=new_zip_name))
                await interaction.edit_original_response(content=f"✅ `{original_zip_name}` (譜面: `{selected_version_display}`) の差分生成・送信完了。")
        except Exception as e:
            print(traceback.format_exc())
            try: await interaction.edit_original_response(content=f"エラー: 譜面処理中に予期せぬ問題発生。\n`{e}`", view=None)
            except discord.NotFound: await interaction.followup.send(f"エラー: 譜面処理中に予期せぬ問題発生。\n`{e}`", ephemeral=True)


    # -----------------------------------------------------------------
    # Discordコマンド
    # -----------------------------------------------------------------
    @app_commands.command(name="malody", description="Malody譜面(.mcz/.zip)のレート差分を生成(ファイル添付 or URL指定)")
    @app_commands.describe(
        attachment="譜面ファイル (.mcz または .zip) ※urlとどちらか一方を指定",
        url="譜面ファイルのダウンロードURL ※attachmentとどちらか一方を指定",
        chart_version="処理対象にする譜面のバージョン名(非推奨。オプションなしでも後から選択可能)",
        rates="個別のレート指定 (例: '1.05 1.1 1.15')",
        range_option="レート範囲指定 (例: '1.05 1.2 0.05')",
        bpm="個別BPM指定 (例: '180 200')",
        bpm_range_option="BPM範囲指定 (例: '150 180 5')",
        desofflan="Trueにするとソフランを除去(他のレート指定がない場合は1.0倍速)",
        no_pitch="Trueにするとピッチを変更しません。"
    )
    async def malody_slash(self, interaction: discord.Interaction,
                         attachment: discord.Attachment = None,
                         url: str = None,
                         chart_version: str = None, 
                         rates: str = None,
                         range_option: str = None,
                         bpm: str = None,
                         bpm_range_option: str = None,
                         desofflan: bool = False,
                         no_pitch: bool = False):
        if attachment and url: await interaction.response.send_message("エラー: `attachment`と`url`同時指定不可。", ephemeral=True); return
        if not attachment and not url: await interaction.response.send_message("エラー: `attachment`か`url`を指定。", ephemeral=True); return

        await interaction.response.defer(thinking=True)
        original_zip_name = ""
        attachment_bytes = None
        try:
            # ... (ファイル取得部分は変更なし) ...
            if attachment:
                if not (attachment.filename.lower().endswith((".mcz", ".zip"))): await interaction.followup.send("エラー: 添付は`.mcz`か`.zip`で。"); return
                original_zip_name = attachment.filename
                if attachment.size > MAX_DOWNLOAD_SIZE * 1.1: await interaction.followup.send(f"エラー: 添付サイズ超過({attachment.size/(1024*1024):.1f}MB)。上限約{MAX_DOWNLOAD_SIZE/(1024*1024):.0f}MB。"); return
                attachment_bytes = await attachment.read()
            elif url:
                await interaction.edit_original_response(content=f"処理中... URLからDL中...\n`{url}`")
                attachment_bytes, original_zip_name = await self._download_file_from_url(url)
                if not (original_zip_name.lower().endswith((".mcz", ".zip"))): await interaction.followup.send(f"エラー: DLファイル拡張子不正: {original_zip_name}"); return
        except ValueError as ve: await interaction.followup.send(f"エラー: {ve}"); return
        except Exception as e: await interaction.followup.send(f"エラー: 譜面ファイル取得失敗。\n`{e}`"); return

        rates_to_generate = []
        target_bpms = []
        is_bpm_mode = False
        
        try:
            # ... (引数パース部分は変更なし) ...
            has_rate_or_bpm_input = rates or range_option or bpm or bpm_range_option
            if rates: rates_to_generate.extend([float(r) for r in rates.split()])
            if range_option:
                parts = range_option.split(); L=len(parts); 
                if L != 3: raise ValueError("range_option は3数値(開始 終了 刻み幅)。")
                start, end, step = float(parts[0]), float(parts[1]), float(parts[2]); 
                if step <= 0: raise ValueError("レート刻み幅 > 0")
                r = start; 
                while r <= end + 1e-9: rates_to_generate.append(r); r += step
            if bpm: is_bpm_mode = True; target_bpms.extend([float(b) for b in bpm.split()])
            if bpm_range_option:
                parts = bpm_range_option.split(); L=len(parts); 
                if L != 3: raise ValueError("bpm_range_option は3数値(開始 終了 刻み幅)。")
                bpm_start, bpm_end, bpm_step = float(parts[0]), float(parts[1]), float(parts[2]); 
                if bpm_step <= 0: raise ValueError("BPM刻み幅 > 0")
                is_bpm_mode = True; b = bpm_start; 
                while b <= bpm_end + 1e-9: target_bpms.append(b); b += bpm_step
            if desofflan and not has_rate_or_bpm_input: rates_to_generate.append(1.0)
            elif not desofflan and not has_rate_or_bpm_input: raise ValueError("レート/BPM/desofflan のいずれかを指定。")
        except Exception as e: await interaction.followup.send(f"エラー: オプション指定不正。\n`{e}`\n\n使い方: `/malody [attachment または url] [オプション]`"); return

        try:
            await interaction.edit_original_response(content=f"処理中... `{original_zip_name}` 解析中。")
            all_charts_info = self.get_chart_info_from_zip(attachment_bytes)
            if not all_charts_info: raise ValueError("`.mcz`内に`.mc`譜面が見つかりません。")

            selected_chart_path = None; selected_chart_info = None
            if len(all_charts_info) == 1:
                selected_chart_info = all_charts_info[0]
                selected_chart_path = selected_chart_info['path']
            elif chart_version is not None:
                found_charts = [c for c in all_charts_info if c["version"] == chart_version]
                count = len(found_charts) # <-- 修正: countを定義
                if count == 1: 
                    selected_chart_info = found_charts[0]
                    selected_chart_path = selected_chart_info['path']
                elif count > 1: raise ValueError(f"エラー: バージョン名 '{chart_version}' 一致譜面複数有。")
                else:
                    chart_list_msg = f"エラー: 指定バージョン名 '{chart_version}' 不一致。\n利用可能:\n" + "\n".join([f"- `{c['version']}`" for c in all_charts_info])
                    raise ValueError(chart_list_msg)
            else: 
                chart_list_for_select = [(c['path'], c['version']) for c in all_charts_info]
                view = ChartSelectView(
                    chart_options=chart_list_for_select,
                    original_zip_name=original_zip_name,
                    attachment_bytes=attachment_bytes,
                    final_rates=rates_to_generate, # <-- 修正
                    target_bpms=target_bpms, # <-- 修正
                    desofflan=desofflan, no_pitch=no_pitch, is_bpm_mode=is_bpm_mode,
                    cog_ref=self
                )
                view.interaction_to_edit = interaction 
                await interaction.edit_original_response(content="譜面パック内に複数の譜面が見つかりました。処理したい譜面を以下から選択してください:", view=view)
                return 

            if not selected_chart_info: raise ValueError("内部エラー: 処理対象の譜面が不明です。")

            final_rates = set(rates_to_generate)
            if is_bpm_mode:
                if selected_chart_info["original_bpm"] > 0:
                    for tbpm in target_bpms: final_rates.add(tbpm / selected_chart_info["original_bpm"])
                else:
                    await interaction.followup.send(f"警告: 選択譜面 `{selected_chart_info['version']}` BPM不明のためBPM指定不可。", ephemeral=True)
                    if not final_rates: raise ValueError("BPM指定がありましたが、選択譜面のBPM不明なためレート計算不可。")
            if not final_rates: raise ValueError("有効なレートが生成されませんでした。")
            final_rates_list = sorted(list(final_rates))

            await self.run_malody_processing(
                interaction, original_zip_name, attachment_bytes,
                selected_chart_path, final_rates_list, desofflan, no_pitch, is_bpm_mode,
                selected_chart_info
            )
        except ValueError as ve: await interaction.edit_original_response(content=str(ve), view=None)
        except Exception as e:
            print(traceback.format_exc())
            await interaction.edit_original_response(content=f"エラー: 予期せぬ問題が発生しました。\n`{e}`", view=None)

# Cogセットアップ関数
async def setup(bot: commands.Bot):
    await bot.add_cog(MalodyCog(bot))

