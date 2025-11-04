# -*- coding: utf-8 -*-

import discord
# --- ↓ 修正箇所 ↓ ---
# 必要なインポート文をすべて揃える
from discord import app_commands
from discord.ext import commands
import json
import zipfile
import io
import re
import os
import traceback
import requests
import time # timeモジュール
import aiohttp
from urllib.parse import urlparse
from typing import List, Dict, Any, Tuple
import asyncio
import fractions # 高精度なビート計算のために追加

# --- osu_mania_base.txt の内容を定数として埋め込み ---
OSU_MANIA_BASE_TEMPLATE = """osu file format v14

[General]
AudioFilename: {audio}
AudioLeadIn: {leadin}
PreviewTime: {preview}
Countdown: 0
SampleSet: Soft
StackLeniency: 0.7
Mode: 3
LetterboxInBreaks: 0
SpecialStyle: 0
WidescreenStoryboard: 1

[Editor]
Bookmarks: 0
DistanceSpacing: 0.8
BeatDivisor: 8
GridSize: 32
TimelineZoom: 1.4

[Metadata]
Title:{title}
TitleUnicode:{title_unicode}
Artist:{artist}
ArtistUnicode:{artist_unicode}
Creator:{creator}
Version:{version}
Source:
Tags:{tags}
BeatmapID:-1
BeatmapSetID:-1

[Difficulty]
HPDrainRate:{hp}
CircleSize:{key_amount}
OverallDifficulty:{od}
ApproachRate:5
SliderMultiplier:1.4
SliderTickRate:1

[Events]
//Background and Video events
0,0,"{background}",0,0
//Break Periods
{breaks}
//Storyboard Layer 0 (Background)
//Storyboard Layer 1 (Fail)
//Storyboard Layer 2 (Pass)
//Storyboard Layer 3 (Foreground)
//Storyboard Sound Samples
"""

# 一時ファイルを保存するディレクトリ名を定義
TEMP_DIR = "temp_audio"
# Discordのファイルサイズ上限
DISCORD_FILE_LIMIT = 8388608
# URLからのダウンロード時の最大ファイルサイズ
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024


# -----------------------------------------------------------------
# beat.py (高精度ビート計算クラス)
# -----------------------------------------------------------------
class Beat(object):
    """
    Malodyの [a, b, c] (a + b/c ビート) を高精度で扱うクラス
    fractions モジュールを利用
    """
    def __init__(self, a: int = 0, b: int = 0, c: int = 1):
        self.a = a
        self.b = b
        self.c = c
        if self.c == 0: # ゼロ除算防止
             self.c = 1
             self.b = 0
        if self.c < 0: # 分母が負の場合、分子と分母の両方に -1 をかける
            self.b = -self.b
            self.c = -self.c
        self.reduction()

    def __str__(self):
        return f"[{str(self.a)}, {str(self.b)}, {str(self.c)}]"

    def reduction(self):
        """ ビートの約分と繰り上がり・繰り下がり処理 """
        try:
            if self.c == 0:
                self.b = 0
                self.c = 1
                return self

            # 小数部 (b/c) が 1 以上または -1 以下の場合、整数部 (a) に移動
            if abs(self.b) >= self.c:
                self.a += (self.b // self.c)
                self.b = self.b % self.c
            
            # 整数部と小数部の符号を合わせる
            if self.a > 0 and self.b < 0:
                self.a -= 1
                self.b += self.c
            elif self.a < 0 and self.b > 0:
                self.a += 1
                self.b -= self.c

            if self.b == 0:
                self.c = 1
            else:
                # fractions を使って約分
                fraction = fractions.Fraction(self.b, self.c)
                self.b = fraction.numerator
                self.c = fraction.denominator
            return self
        except Exception:
            # 予期せぬエラーの場合はデフォルト値に
            self.a = 0
            self.b = 0
            self.c = 1
            return self

    def __iadd__(self, beat_2):
        """ self += beat_2 """
        # 通分して足し算
        common_c = self.c * beat_2.c
        new_b = (self.b * beat_2.c) + (beat_2.b * self.c)
        self.a += beat_2.a
        self.b = new_b
        self.c = common_c
        return self.reduction()

    def __add__(self, beat_2):
        """ self + beat_2 """
        res = Beat(self.a, self.b, self.c)
        return res.__iadd__(beat_2)

    def __sub__(self, beat_2):
        """ self - beat_2 """
        # 通分して引き算
        common_c = self.c * beat_2.c
        new_b = (self.b * beat_2.c) - (beat_2.b * self.c)
        res = Beat(self.a - beat_2.a, new_b, common_c)
        return res.reduction()

    # --- 比較演算子 ---
    def to_fraction(self) -> fractions.Fraction:
        """ 比較のため、Beatオブジェクトを単一の Fraction に変換 """
        if self.c == 0: return fractions.Fraction(self.a)
        return fractions.Fraction(self.a) + fractions.Fraction(self.b, self.c)

    def __gt__(self, beat_2):
        return self.to_fraction() > beat_2.to_fraction()

    def __ge__(self, beat_2):
        return self.to_fraction() >= beat_2.to_fraction()
    
    def __lt__(self, beat_2):
        return self.to_fraction() < beat_2.to_fraction()

    def __le__(self, beat_2):
        return self.to_fraction() <= beat_2.to_fraction()
        
    def __eq__(self, beat_2):
        return self.to_fraction() == beat_2.to_fraction()
    
    def __ne__(self, beat_2):
        return self.to_fraction() != beat_2.to_fraction()


    @staticmethod
    def from_time(time: float or fractions.Fraction, millseconds_per_beat: float or fractions.Fraction, max_denominator: int = 192):
        """
        ミリ秒とBPMから高精度なBeatオブジェクトを生成する。
        元のcogのロジック（192分音符）とfractionsを組み合わせた方式。
        time と millseconds_per_beat は float または Fraction
        """
        try:
            # ゼロ除算防止
            if millseconds_per_beat == 0:
                return Beat(0, 0, 1) 
            
            # float が来ても Fraction に変換
            time_frac = fractions.Fraction(time)
            ms_per_beat_frac = fractions.Fraction(millseconds_per_beat)

            if ms_per_beat_frac == 0:
                return Beat(0, 0, 1)

            # Fraction で計算
            total_beats_frac = time_frac / ms_per_beat_frac
            
            # floor と同じ挙動
            beat_int = int(total_beats_frac.numerator / total_beats_frac.denominator) 
            if total_beats_frac < 0: # 負の数の場合
                beat_int = int(total_beats_frac)

            beat_fraction_frac = total_beats_frac - beat_int # 小数部
            
            # 浮動小数点を指定された最大分母で分数に変換
            f = beat_fraction_frac.limit_denominator(max_denominator)
            
            beat_obj = Beat(beat_int, f.numerator, f.denominator)
            return beat_obj.reduction() # 整数繰り上がりなどを処理
        except Exception:
            return Beat(0, 0, 1) # エラー時は 0拍目

    def to_time(self, millseconds_per_beat: float or fractions.Fraction) -> float:
        """ Beatオブジェクトをミリ秒 (float) に変換 """
        try:
            if self.c == 0: 
                self.c = 1
            
            # Fraction で計算してから float に変換
            ms_per_beat_frac = fractions.Fraction(millseconds_per_beat)
            result_frac = (fractions.Fraction(self.a) + fractions.Fraction(self.b, self.c)) * ms_per_beat_frac
            return float(result_frac)
        except Exception:
            return 0.0 # エラー時は 0 ms

# -----------------------------------------------------------------
# Discord Cog
# -----------------------------------------------------------------
class ConverterCog(commands.Cog):
    """Malody <-> osu! 譜面フォーマット相互変換機能"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http_session = aiohttp.ClientSession()
        os.makedirs(TEMP_DIR, exist_ok=True)
        print("- converter_cog.py を読み込みました。")

    async def cog_unload(self):
        await self.http_session.close()

    # -----------------------------------------------------------------
    # Helper Functions (Litterbox/URL Download)
    # -----------------------------------------------------------------
    def _upload_to_litterbox(self, file_bytes, file_name):
        try:
            files = { 'reqtype': (None, 'fileupload'), 'time': (None, '24h'), 'fileToUpload': (file_name, file_bytes, 'application/zip'), }
            response = requests.post('https://litterbox.catbox.moe/resources/internals/api.php', files=files, timeout=300)
            response.raise_for_status()
            response_text = response.text
            if response.status_code == 200 and (response_text.startswith("https://litterbox.catbox.moe/") or response_text.startswith("https://litter.catbox.moe/")):
                return response_text
            else:
                raise Exception(f"Litterboxアップロード失敗: {response_text}")
        except requests.exceptions.RequestException as e:
            print(f"Litterboxアップロードエラー: {e}")
            raise Exception(f"Litterboxアップロード失敗。\n`{e}`")

    async def _download_file_from_url(self, url: str) -> tuple[bytes, str]:
        try:
            # 一般的なブラウザの User-Agent を設定
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
            }
            async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=300), headers=headers) as resp:
                if resp.status == 200:
                    cl = resp.headers.get('Content-Length'); 
                    if cl and int(cl) > MAX_DOWNLOAD_SIZE: raise ValueError(f"ファイルサイズ超過 ({int(cl)/(1024*1024):.1f}MB)。上限{MAX_DOWNLOAD_SIZE/(1024*1024):.0f}MB。")
                    fb = await resp.read(); fn = "unknown_file.zip"; cd = resp.headers.get('Content-Disposition')
                    if cd: p = cd.split('filename='); fn = p[1].strip('" ') if len(p) > 1 else fn
                    else: pu = urlparse(url); pf = os.path.basename(pu.path); fn = pf if pf else fn
                    if not (fn.lower().endswith((".mcz", ".osz", ".zip"))): # .oszも許可
                        if "." not in fn: fn += ".zip"
                        else: raise ValueError(f"DLファイルの拡張子不正: {fn}")
                    return fb, fn
                else:
                    raise ValueError(f"URLからのDL失敗 (HTTP: {resp.status})。")
        except asyncio.TimeoutError: raise ValueError("URLからのDLタイムアウト (5分)。")
        except aiohttp.ClientError as e: raise ValueError(f"URL接続/DLエラー。\n`{e}`")
        except Exception as e: raise ValueError(f"URL処理中予期せぬエラー。\n`{e}`")

    # -----------------------------------------------------------------
    # Osu! <-> Malody 変換ロジック
    # -----------------------------------------------------------------

    def _parse_osu(self, osu_content: str) -> Dict[str, Any]:
        """ .osu ファイル (INI形式) をパースして辞書に変換 (変更なし) """
        lines = osu_content.splitlines()
        data = {}
        current_section = ''
        list_sections = ['TimingPoints', 'HitObjects', 'Events']
        for line in lines:
            line = line.strip()
            if line.startswith('//') or line == '': continue
            if line.startswith('['):
                current_section = line[1:-1]
                if current_section in list_sections: data[current_section] = []
                else: data[current_section] = {}
            elif current_section:
                if current_section in list_sections: data[current_section].append(line)
                else:
                    parts = line.split(':', 1)
                    if len(parts) == 2: data[current_section][parts[0].strip()] = parts[1].strip()
        return data

    # --- [！修正箇所！] ---
    def _convert_osu_to_mc(self, osu_content: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """ .osu (osu!mania 4K) の内容を .mc (Malody 4K) のJSONデータに変換 (赤線SV->緑線SV変換ロジック + 高精度ビート計算 適用) """
        osu_data = self._parse_osu(osu_content)

        if not osu_data.get('General') or osu_data['General'].get('Mode') != '3': return None
        if not osu_data.get('Difficulty') or osu_data['Difficulty'].get('CircleSize') != '4': return None

        metadata = osu_data.get('Metadata', {}); general = osu_data.get('General', {}); events = osu_data.get('Events', [])
        bg_event = next((e for e in events if e.startswith('0,0,') or e.startswith('1,0,')), None)
        background = os.path.basename(bg_event.split(',')[2].replace('"', '')) if bg_event else ''
        audio_filename = os.path.basename(general.get('AudioFilename', 'audio.mp3'))
        
        timing_points = []
        for line in osu_data.get('TimingPoints', []):
            try: 
                # Fraction で読み込み
                parts = line.split(',')
                if len(parts) < 7: continue # 無効な行をスキップ
                # time (parts[0]) と beatLength (parts[1]) を Fraction で読み込む
                point_data = [
                    fractions.Fraction(parts[0]), # time
                    fractions.Fraction(parts[1]), # beatLength (msPerBeat or SV)
                ]
                point_data.extend(map(float, parts[2:])) # 残り
                timing_points.append(point_data)
            except (ValueError, TypeError, IndexError): 
                print(f"Skipping invalid timing point line: {line}")
        hit_objects = [line.split(',') for line in osu_data.get('HitObjects', [])]

        # --- 赤線SV -> 緑線SV 変換ロジック ---
        original_red_points = [p for p in timing_points if len(p) >= 7 and p[6] > 0]
        original_green_points = [p for p in timing_points if len(p) >= 7 and p[6] == 0]
        
        if not original_red_points:
             # 赤線が1つもない場合
             first_point = timing_points[0] if timing_points else None
             if first_point and first_point[1] > 0:
                 original_red_points = [first_point]
             else:
                 raise ValueError("BPMを定義するタイミングポイント(赤線)が見つかりません。")
        
        # 最初の赤線をベースBPMとする
        base_red_point = min(original_red_points, key=lambda p: p[0])
        
        # base_ms_per_beat は Fraction オブジェクトになる
        base_ms_per_beat = base_red_point[1] 
        if base_ms_per_beat <= 0:
             raise ValueError(f"ベースBPMの解析に失敗しました (ms_per_beat={base_ms_per_beat})。")
        
        # 他の赤線を緑線（SV）に変換する
        new_green_points_from_red = []
        for point in original_red_points:
            # point[1] (msPerBeat) も Fraction オブジェクト
            if point[1] <= 0: continue 
            
            # Fraction 同士の割り算 (BPM比 = SV倍率)
            new_sv_multiplier = base_ms_per_beat / point[1] 
            if new_sv_multiplier == 0: continue
            
            # Fraction で計算 (beatLength = -100 / SV倍率)
            new_beat_length = fractions.Fraction(-100) / new_sv_multiplier
            
            # new_green_point[1] には Fraction オブジェクトが入る
            new_green_point = [point[0], new_beat_length, point[2], point[3], point[4], point[5], 0, point[7] if len(point) > 7 else 0]
            new_green_points_from_red.append(new_green_point)

        # 変換対象の緑線リスト = 元の緑線 + 赤線から変換した緑線
        # (元の緑線は beatLength が負のものだけがSV、正のものは無視)
        inherited_points = [p for p in original_green_points if p[1] < 0] + new_green_points_from_red
        inherited_points.sort(key=lambda p: p[0]) # 時間順にソート

        # MalodyのBPM定義 (timeセクション) に使うのは「ベースBPM」のみ
        uninherited_points = [base_red_point]
        # --- 赤線SV -> 緑線SV 変換ロジック ここまで ---
        
        timing_map = []; 
        last_beat = Beat(0, 0, 1) # Beat オブジェクトに変更
        
        if not uninherited_points: raise ValueError("タイミングポイント(uninherited_points)の解析に失敗しました。")

        for point in uninherited_points:
            # point_time と ms_per_beat は Fraction オブジェクト
            point_time, ms_per_beat = point[0], point[1]
            if not timing_map:
                timing_map.append({'time': point_time, 'ms_per_beat': ms_per_beat, 'start_beat': Beat(0, 0, 1)}) # Beat オブジェクト
            else:
                last_point = timing_map[-1]; 
                # Fraction 同士の引き算
                time_diff = point_time - last_point['time']
                
                # Beat.from_time を使って高精度にビート差を計算 (Fraction を渡す)
                beats_diff_obj = Beat.from_time(time_diff, last_point['ms_per_beat'])
                last_beat += beats_diff_obj # Beat オブジェクト同士の足し算
                
                timing_map.append({'time': point_time, 'ms_per_beat': ms_per_beat, 'start_beat': last_beat})
        
        if not timing_map: raise ValueError("タイミングポイントの解析に失敗しました。")
        
        osu_offset_float = float(timing_map[0]['time']); malody_offset = -osu_offset_float

        # --- 修正: time_to_beat (高精度ビート計算) ---
        def time_to_beat(timestamp: float) -> List[int]:
            """ osu!のミリ秒時間 (float) を Malody の [beat, num, den] 形式に変換 """
            
            # timestamp は float のままなので Fraction に変換
            timestamp_frac = fractions.Fraction(timestamp)
            
            segment = timing_map[0]
            for i in range(len(timing_map) - 1, -1, -1):
                # Fraction 同士で比較 (0.1msの許容)
                if timing_map[i]['time'] <= timestamp_frac + fractions.Fraction(1, 10): 
                    segment = timing_map[i]; break
            
            # Fraction 同士の引き算
            time_in_segment = timestamp_frac - segment['time']
            
            # Beat.from_time を使用 (Fraction を渡す)
            beats_in_segment = Beat.from_time(time_in_segment, segment['ms_per_beat'])
            
            # Beat オブジェクト同士の足し算
            total_beat_obj = segment['start_beat'] + beats_in_segment
            total_beat_obj.reduction() # 念のため
            
            return [total_beat_obj.a, total_beat_obj.b, total_beat_obj.c]
        # --- 修正ここまで ---

        mc_data = {
            "meta": { "creator": metadata.get('Creator', ''), "background": background, "version": metadata.get('Version', ''), "preview": int(general.get('PreviewTime', -1)), "mode": 0, "time": int(time.time()), "song": { "title": metadata.get('Title', 'Unknown Title'), "artist": metadata.get('Artist', 'Unknown Artist'), "id": 0 }, "mode_ext": { "column": 4 }},
            # time と ms_per_beat を float に戻して格納
            "time": [ {"beat": time_to_beat(float(p['time'])), "bpm": 60000.0 / float(p['ms_per_beat'])} for p in timing_map ],
            "note": [], "effect": []
        }
        mc_data["note"].append({ "beat": [0, 0, 1], "sound": audio_filename, "offset": round(malody_offset), "type": 1 })

        # --- 修正箇所: SV (緑線) を effect に変換 (逆走・停止対応) ---
        if settings.get('sv', {}).get('enabled', True):
            for point in inherited_points: # `inherited_points` を使う (赤線SV変換後のリスト)
                # point_time と beat_length は Fraction オブジェクト
                point_time, beat_length = point[0], point[1]
                
                # beat_length が 0 (ありえないが) または正 (逆走SVではない) の場合
                if beat_length == 0: continue
                
                # Fraction で計算
                scroll_speed_frac = fractions.Fraction(-100) / beat_length
                # float に変換して格納
                scroll_speed = float(scroll_speed_frac)

                # Malodyは停止(0.0)も逆走(負の値)も対応しているため、補正ロジックは不要
                
                mc_data["effect"].append({
                    "beat": time_to_beat(float(point_time)), # time_to_beat に float を渡す
                    "scroll": scroll_speed
                })
        # --- 修正ここまで ---

        for obj in hit_objects:
            try:
                x, timestamp_float, obj_type = int(obj[0]), int(obj[2]), int(obj[3])
                column = int(x * 4 // 512)
                if column < 0 or column > 3: continue
                
                # 高精度な time_to_beat を使用 (float を渡す)
                note = {"beat": time_to_beat(timestamp_float), "column": column}
                
                if (obj_type & 128) == 128:
                    end_time_float = int(obj[5].split(':')[0])
                    note["endbeat"] = time_to_beat(end_time_float) # 高精度な time_to_beat を使用
                
                mc_data["note"].append(note)
            except (ValueError, IndexError, TypeError) as e:
                print(f"Warning: Skipping HitObject line due to parse error: {obj} | Error: {e}")
        return { "mcData": mc_data, "artist": metadata.get('Artist', 'Unknown'), "title": metadata.get('Title', 'Unknown') }

    # --- [！修正箇所！] ---
    def _convert_mc_to_osu(self, mc_content: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """ .mc (Malody 4K) を .osu (osu!mania 4K) に変換 (高精度ビート計算 適用) """
        mc_data = json.loads(mc_content)
        meta = mc_data.get('meta', {}); song_info = meta.get('song', {})
        title = song_info.get('title', 'Unknown Title'); artist = song_info.get('artist', 'Unknown Artist')
        creator = meta.get('creator', 'Unknown Creator'); version = meta.get('version', '4K Normal')
        preview_time = meta.get('preview', -1)

        mode_ext = meta.get('mode_ext', {})
        if mode_ext.get('column') and mode_ext['column'] != 4:
            raise ValueError(f"4K譜面のみ対応です。'{version}' は {mode_ext['column']}K です。")

        # --- 修正箇所: beat_to_decimal (高精度) ---
        # beat_to_decimal は使わず、Beatオブジェクトを直接使う
        # --- 修正ここまで ---

        initial_offset = 0; audio_filename = 'audio.mp3'
        # 譜面ファイル内の control note (type: 1) を探す
        control_note = next((n for n in mc_data.get('note', []) if n.get('type') == 1 and 'offset' in n and 'sound' in n), None)
        
        if control_note:
            initial_offset = -1 * control_note.get('offset', 0)
            audio_filename = control_note.get('sound', 'audio.mp3')
        elif 'offset' in song_info: # フォールバック
            initial_offset = -1 * song_info.get('offset', 0)
            audio_filename = song_info.get('audio', 'audio.mp3')
        
        background_filename = meta.get('background', song_info.get('background', 'bg.jpg'))
        
        audio_filename = os.path.basename(audio_filename)
        background_filename = os.path.basename(background_filename)

        timing_points_mc = sorted(mc_data.get('time', []), key=lambda p: Beat(*p.get('beat', [0,0,1])))
        if not timing_points_mc: raise ValueError('BPM情報 (timeイベント) が見つかりません。')

        # --- 修正: processed_timing_points (高精度) ---
        processed_timing_points = []; 
        last_beat = Beat(0, 0, 1) # Beat オブジェクトに変更
        current_time = float(initial_offset) # time は float で管理
        
        first_bpm = 120
        for p in timing_points_mc:
            if p.get('bpm', 0) > 0:
                first_bpm = p['bpm']
                break
        last_bpm = first_bpm

        for point in timing_points_mc:
            current_beat_obj = Beat(*point.get('beat', [0,0,1])) # Beat オブジェクト
            
            # Beat オブジェクト同士の引き算
            beat_diff_obj = current_beat_obj - last_beat
            
            # Beat オブジェクトから高精度に時間を計算
            time_diff_float = beat_diff_obj.to_time(60000.0 / last_bpm)
            current_time += time_diff_float
            
            new_bpm = point.get('bpm', last_bpm)
            if new_bpm <= 0: new_bpm = last_bpm 
            
            processed_timing_points.append({'time': current_time, 'bpm': new_bpm, 'beat_obj': current_beat_obj}) # Beat オブジェクトを保持
            last_beat = current_beat_obj
            last_bpm = new_bpm
        # --- 修正ここまで ---

        # --- 修正: calculate_note_time (高精度) ---
        def calculate_note_time(beat_array: List[int]) -> float:
            note_beat_obj = Beat(*beat_array) # Beat オブジェクト
            
            segment = processed_timing_points[0]
            for i in range(len(processed_timing_points) - 1, -1, -1):
                # Beat オブジェクト同士で比較
                if processed_timing_points[i]['beat_obj'] <= note_beat_obj:
                    segment = processed_timing_points[i]
                    break
            
            # Beat オブジェクト同士の引き算
            beat_diff_obj = note_beat_obj - segment['beat_obj']
            
            # Beat オブジェクトから高精度に時間を計算
            time_diff = beat_diff_obj.to_time(60000.0 / segment['bpm'])
            
            return segment['time'] + time_diff
        # --- 修正ここまで ---

        osu_file_name = f"{artist} - {title} ({creator}) [{version}].osu".replace(r'[\\/:*?"<>|]', '_')
        
        osu_sections = {
            "General": [], "Editor": [], "Metadata": [], "Difficulty": [],
            "Events": ["//Background and Video events", f'0,0,"{background_filename}",0,0', "//Break Periods"],
            "TimingPoints": [],
            "HitObjects": []
        }

        for point in processed_timing_points:
            beat_length = 60000.0 / point['bpm']
            # time は float でなければならない
            osu_sections["TimingPoints"].append(f"{point['time']},{beat_length},4,1,0,100,1,0")

        if settings.get('sv', {}).get('enabled', True) and mc_data.get('effect'):
            effects_mc = sorted(mc_data.get('effect', []), key=lambda p: Beat(*p.get('beat', [0,0,1])))
            for effect in effects_mc:
                if 'scroll' in effect:
                    try:
                        # 高精度な calculate_note_time を使用
                        time_sv = round(calculate_note_time(effect['beat']))
                        scroll_speed = float(effect['scroll'])
                        
                        beat_length = 0 # 初期化
                        
                        # --- ↓ 修正箇所 (Malody -> osu! 逆走・停止対応) ↓ ---
                        if scroll_speed == 0.0:
                            # Malody の 0.0 (停止) は osu! の超低速SV (例: 0.0001x)
                            beat_length = -1000000 # 0.0001x SV
                        elif scroll_speed != 0:
                            # 0.01x や 10000x、および逆走 (-1.0x など) も含め
                            # すべてこの計算式で osu! の beat_length に変換
                            beat_length = -100.0 / scroll_speed
                        # --- ↑ 修正箇所 ↑ ---
                        
                        if beat_length != 0: # beat_length が設定された場合のみ追加
                            osu_sections["TimingPoints"].append(f"{time_sv},{beat_length},4,1,0,100,0,0")
                    except (ValueError, TypeError):
                        print(f"Warning: Skipping invalid SV effect: {effect}")

        
        osu_sections["TimingPoints"].sort(key=lambda x: (float(x.split(',')[0]), -float(x.split(',')[1])))

        notes_mc = [n for n in mc_data.get('note', []) if 'column' in n]
        get_x = lambda col: int((512 / 4) * col + (512 / 4 / 2))

        for note in notes_mc:
            try:
                if note['column'] < 0 or note['column'] >= 4: continue
                
                # 高精度な calculate_note_time を使用
                time_note = round(calculate_note_time(note['beat']))
                hit_sample = "0:0:0:0:"
                
                if 'endbeat' in note:
                    end_time = round(calculate_note_time(note['endbeat']))
                    osu_sections["HitObjects"].append(f"{get_x(note['column'])},192,{time_note},128,0,{end_time}:{hit_sample}")
                else:
                    osu_sections["HitObjects"].append(f"{get_x(note['column'])},192,{time_note},1,0,{hit_sample}")
            except (ValueError, TypeError) as e:
                 print(f"Warning: Skipping invalid HitObject: {note} | Error: {e}")

        # 休憩時間 (Breaks) の挿入
        if settings.get('breaks', {}).get('enabled', True):
            all_note_events = []
            for line in osu_sections["HitObjects"]:
                parts = line.split(','); start_time = int(parts[2]); all_note_events.append(start_time)
                if parts[3] == '128': end_time = int(parts[5].split(':')[0]); all_note_events.append(end_time)
            all_note_events.sort()
            break_duration_ms = settings.get('breaks', {}).get('duration', 5) * 1000
            min_osu_break_duration = 3000
            for i in range(len(all_note_events) - 1):
                gap_start = all_note_events[i]; gap_end = all_note_events[i+1]; gap = gap_end - gap_start
                if gap >= break_duration_ms:
                    break_start_time = gap_start + 100; break_end_time = gap_end - 100
                    if break_end_time - break_start_time >= min_osu_break_duration:
                         osu_sections["Events"].append(f"2,{break_start_time},{break_end_time}")

        # --- .osu ファイル文字列の構築 ---
        osu_content_str = ""
        # テンプレートを使って構築
        
        # 休憩時間(Breaks)の文字列を生成
        breaks_str = "\r\n".join(line for line in osu_sections["Events"] if line.startswith("2,"))
        
        # .osu テンプレートへの埋め込み
        osu_content_str = OSU_MANIA_BASE_TEMPLATE.format(
            audio=audio_filename,
            leadin=0,
            preview=preview_time,
            title=title,
            title_unicode=title, # 簡易的に
            artist=artist,
            artist_unicode=artist, # 簡易的に
            creator=creator,
            version=version,
            tags=settings.get('tags', 'malody converted'),
            hp=settings.get('hp', 8),
            key_amount=4,
            od=settings.get('od', 8),
            background=background_filename,
            breaks=breaks_str
        )
        
        # [TimingPoints] セクションの追加
        osu_content_str += "\r\n[TimingPoints]\r\n"
        osu_content_str += "\r\n".join(osu_sections["TimingPoints"]) + "\r\n"
        
        # [HitObjects] セクションの追加
        osu_content_str += "\r\n[HitObjects]\r\n"
        osu_content_str += "\r\n".join(osu_sections["HitObjects"]) + "\r\n"
        
        return {
            "osuContent": osu_content_str.strip(), "osuFileName": osu_file_name,
            "artist": artist, "title": title
        }

    # -----------------------------------------------------------------
    # Discordコマンド
    # -----------------------------------------------------------------
    @app_commands.command(name="convert", description="Malody(.mcz)とosu!(.osz)の4K譜面を相互変換します。")
    @app_commands.describe(
        attachment="譜面ファイル (.mcz または .osz) ※urlとどちらか一方を指定",
        url="譜面ファイルのダウンロードURL ※attachmentとどちらか一方を指定",
        hp_drain="[Malody->osu!] HPドレイン (0-10, デフォルト: 8)",
        overall_difficulty="[Malody->osu!] OD (0-10, デフォルト: 8)",
        tags="[Malody->osu!] 追加するタグ (スペース区切り)",
        apply_breaks="[Malody->osu!] 休憩時間を自動挿入 (デフォルト: True)",
        apply_sv="[Malody->osu!] Malodyのscrollエフェクトをosu!のSVとして反映 (デフォルト: True)"
    )
    async def convert_slash(self, interaction: discord.Interaction,
                         attachment: discord.Attachment = None,
                         url: str = None,
                         hp_drain: app_commands.Range[float, 0, 10] = 8.0,
                         overall_difficulty: app_commands.Range[float, 0, 10] = 8.0,
                         tags: str = None,
                         apply_breaks: bool = True,
                         apply_sv: bool = True):
        
        if attachment and url: await interaction.response.send_message("エラー: `attachment`と`url`同時指定不可。", ephemeral=True); return
        if not attachment and not url: await interaction.response.send_message("エラー: `attachment`か`url`を指定。", ephemeral=True); return

        await interaction.response.defer(thinking=True)
        original_zip_name = ""; attachment_bytes = None
        try:
            if attachment:
                if not (attachment.filename.lower().endswith((".mcz", ".osz", ".zip"))): await interaction.followup.send("エラー: 添付は`.mcz`, `.osz`, `.zip`で。"); return
                original_zip_name = attachment.filename
                if attachment.size > MAX_DOWNLOAD_SIZE * 1.1: await interaction.followup.send(f"エラー: 添付サイズ超過({attachment.size/(1024*1024):.1f}MB)。上限約{MAX_DOWNLOAD_SIZE/(1024*1024):.0f}MB。"); return
                attachment_bytes = await attachment.read()
            elif url:
                await interaction.edit_original_response(content=f"処理中... URLからDL中...\n`{url}`")
                attachment_bytes, original_zip_name = await self._download_file_from_url(url)
                if not (original_zip_name.lower().endswith((".mcz", ".osz", ".zip"))): await interaction.followup.send(f"エラー: DLファイル拡張子不正: {original_zip_name}"); return
        except ValueError as ve: await interaction.followup.send(f"エラー: {ve}"); return
        except Exception as e: await interaction.followup.send(f"エラー: 譜面ファイル取得失敗。\n`{e}`"); return
        
        try:
            await interaction.edit_original_response(content=f"処理中... `{original_zip_name}` 解析中。")
            input_zip_buffer = io.BytesIO(attachment_bytes); output_zip_buffer = io.BytesIO()
            mc_files = []; osu_files = []; asset_files = {}; conversion_direction = None
            try:
                with zipfile.ZipFile(input_zip_buffer, 'r') as in_zip:
                    for item in in_zip.infolist():
                        if item.is_dir(): continue
                        file_path_utf8 = item.filename # デフォルト
                        try: 
                            # UTF-8 でデコード可能か試す
                            item.filename.encode('cp437').decode('utf-8')
                            file_path_utf8 = item.filename
                        except UnicodeDecodeError:
                            # 失敗した場合、Shift-JIS (cp932) でデコードを試す
                            try: 
                                file_path_utf8 = item.filename.encode('cp437').decode('cp932')
                            except UnicodeDecodeError:
                                try:
                                    # それでもダメなら、ファイル名をそのまま（文字化けの可能性あり）使う
                                    file_path_utf8 = item.filename
                                except Exception:
                                    print(f"ZIP内ファイル名のデコードに失敗: {item.filename}")
                                    continue # このファイルはスキップ

                        if file_path_utf8.startswith("__MACOSX/"): continue
                        
                        file_bytes = b""
                        try:
                            file_bytes = in_zip.read(item.filename)
                        except Exception as read_e:
                            print(f"ZIP内ファイル読込エラー (スキップ): {item.filename} | {read_e}")
                            continue
                        
                        # ファイルパスを正規化（重要）
                        normalized_path = os.path.basename(file_path_utf8)
                        
                        if normalized_path.lower().endswith(".mc"):
                            mc_files.append({"path": normalized_path, "bytes": file_bytes}); conversion_direction = "mcz_to_osz"
                        elif normalized_path.lower().endswith(".osu"):
                            osu_files.append({"path": normalized_path, "bytes": file_bytes}); conversion_direction = "osz_to_mcz"
                        else: 
                            # アセットファイル（重複チェック）
                            if normalized_path not in asset_files:
                                asset_files[normalized_path] = file_bytes
            except zipfile.BadZipFile: raise ValueError("添付ファイルは有効なZIP/.mcz/.oszではありません。")
            except Exception as zip_read_e: raise ValueError(f"ZIP/.mcz/.osz読込エラー。\n`{zip_read_e}`")

            if not conversion_direction: raise ValueError("`.mc` または `.osu` 譜面ファイルが見つかりません。")
            
            new_zip_name = ""; converted_files = 0; main_artist = "Unknown"; main_title = "Unknown"
            
            with zipfile.ZipFile(output_zip_buffer, 'w', zipfile.ZIP_DEFLATED) as out_zip:
                for path, bytes_data in asset_files.items():
                    out_zip.writestr(path, bytes_data) # os.path.basename(path) -> path (正規化済みのため)
                
                settings = {"hp": hp_drain, "od": overall_difficulty, "tags": tags or "malody converted", "breaks": {"enabled": apply_breaks, "duration": 5}, "sv": {"enabled": apply_sv}}
                
                if conversion_direction == "mcz_to_osz":
                    await interaction.edit_original_response(content=f"処理中... Malody (.mc) から osu! (.osu) に変換中... ({len(mc_files)}譜面)")
                    for mc_file in mc_files:
                        try:
                            mc_content = mc_file['bytes'].decode('utf-8')
                            result = self._convert_mc_to_osu(mc_content, settings)
                            out_zip.writestr(result["osuFileName"], result["osuContent"].encode('utf-8'))
                            main_artist, main_title = result["artist"], result["title"]; converted_files += 1
                        except Exception as convert_e:
                            await interaction.followup.send(f"警告: 譜面 `{mc_file['path']}` の変換失敗。スキップ。\n`{convert_e}`", ephemeral=True)
                            print(traceback.format_exc())
                            # 変換失敗時は元のファイルを書き戻す
                            out_zip.writestr(mc_file['path'], mc_file['bytes'])
                    new_zip_name = f"{main_artist} - {main_title} (converted).osz"

                elif conversion_direction == "osz_to_mcz":
                    await interaction.edit_original_response(content=f"処理中... osu! (.osu) から Malody (.mc) に変換中... ({len(osu_files)}譜面)")
                    for osu_file in osu_files:
                        try:
                            osu_content = osu_file['bytes'].decode('utf-8')
                            result = self._convert_osu_to_mc(osu_content, settings) # settings を渡す
                            if result:
                                mc_file_path = os.path.splitext(osu_file['path'])[0] + ".mc"
                                out_zip.writestr(mc_file_path, json.dumps(result["mcData"], indent=2).encode('utf-8'))
                                main_artist, main_title = result["artist"], result["title"]; converted_files += 1
                            else:
                                await interaction.followup.send(f"情報: 譜面 `{osu_file['path']}` はosu!mania 4K譜面ではないため、スキップ。", ephemeral=True)
                                # スキップ時も元のファイルを書き戻す
                                out_zip.writestr(osu_file['path'], osu_file['bytes'])
                        except Exception as convert_e:
                            await interaction.followup.send(f"警告: 譜面 `{osu_file['path']}` の変換失敗。スキップ。\n`{convert_e}`", ephemeral=True)
                            print(traceback.format_exc())
                            # 変換失敗時は元のファイルを書き戻す
                            out_zip.writestr(osu_file['path'], osu_file['bytes'])
                    new_zip_name = f"{main_artist} - {main_title} (converted).mcz"

            if converted_files == 0: raise ValueError("処理対象の譜面が0件でした。")

            output_zip_buffer.seek(0); file_bytes = output_zip_buffer.getvalue(); file_size = len(file_bytes)
            safe_zip_name = re.sub(r'[\\/:*?"<>|]', "_", new_zip_name)
            result_msg_prefix = f"変換完了！計 {converted_files} 個の譜面を変換しました。"

            if file_size > DISCORD_FILE_LIMIT:
                await interaction.edit_original_response(content=f"{result_msg_prefix}\nファイルサイズ超過(8MB)、アップロード中...")
                try:
                    loop = self.bot.loop; download_url = await loop.run_in_executor(None, self._upload_to_litterbox, file_bytes, safe_zip_name)
                    embed = discord.Embed(title="譜面パック変換完了（大容量）", description=f"一時DLリンク生成完了。\n**[ここをクリックしてダウンロード]({download_url})**", color=discord.Color.green())
                    embed.add_field(name="ファイル名", value=safe_zip_name); embed.add_field(name="サイズ", value=f"{file_size / (1024*1024):.2f} MB"); embed.set_footer(text="※リンクは24時間後に自動削除されます。")
                    await interaction.edit_original_response(content=None, embed=embed)
                except Exception as upload_e: await interaction.edit_original_response(content=f"エラー: {result_msg_prefix} ファイルサイズ超過({file_size / (1024*1024):.2f} MB)、アップロード失敗。\n`{upload_e}`")
            else:
                await interaction.edit_original_response(content=f"{result_msg_prefix} ファイル送信中...")
                await interaction.followup.send(file=discord.File(io.BytesIO(file_bytes), filename=safe_zip_name))
                await interaction.edit_original_response(content=f"✅ `{original_zip_name}` の変換・送信完了。")

        except ValueError as ve: 
            await interaction.edit_original_response(content=f"エラー: {ve}", view=None)
        except Exception as e:
            print(traceback.format_exc())
            await interaction.edit_original_response(content=f"エラー: 譜面変換中に予期せぬ問題発生。\n`{e}`", view=None)

# Cogセットアップ関数
async def setup(bot: commands.Bot):
    await bot.add_cog(ConverterCog(bot))

