import sys
import random
import re
import time
import math
import json
import os
import requests
import urllib.parse
import subprocess
import base64
import win32gui
import win32process
import win32api
import win32con
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QTextEdit, QPushButton, QLabel, QSlider, QColorDialog, QSpinBox,
    QFontComboBox, QComboBox, QCheckBox, QInputDialog, QMessageBox,
    QDoubleSpinBox, QScrollArea
)
from PyQt5.QtCore import Qt, QTimer, QRectF, QThread, pyqtSignal
from PyQt5.QtGui import (
    QPainter, QColor, QFont, QFontMetrics, QPen, QPainterPath, QTransform
)

# ==================== 打包(冻结)模式适配 ====================
if getattr(sys, 'frozen', False):
    # 无控制台模式下重定向 stdout/stderr，避免 print 崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "lyric_config.json")

# ==================== 默认播放器配置 ====================
DEFAULT_PLAYERS = {
    "网易云音乐": {
        "process": "cloudmusic.exe",
        "pattern": r'^(.+)\s*-\s*(.+?)$'
    },
    "酷狗音乐": {
        "process": "kgmusic.exe",
        "pattern": r'^(.+)\s*-\s*(.+?)$'
    },
    "QQ音乐": {
        "process": "QQMusic.exe",
        "pattern": r'^(.+)\s*-\s*(.+?)$'
    }
}

# ==================== SMTC 系统媒体支持 ====================
SMTC_PLAYER_NAME = "系统媒体 (SMTC)"
try:
    import asyncio
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _SmtcManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _SmtcPlaybackStatus,
    )
    SMTC_AVAILABLE = True
except Exception:
    SMTC_AVAILABLE = False


def smtc_read_once():
    """读取一次系统媒体(SMTC)当前会话，返回 dict 或 None。"""
    if not SMTC_AVAILABLE:
        return None
    try:
        async def _read():
            mgr = await _SmtcManager.request_async()
            sess = mgr.get_current_session()
            if sess is None:
                return None
            info = await sess.try_get_media_properties_async()
            if info is None:
                return None
            tl = sess.get_timeline_properties()
            pb = sess.get_playback_info()
            position_ms = int(tl.position.total_seconds() * 1000) if tl.position else 0
            duration_ms = int(tl.end_time.total_seconds() * 1000) if tl.end_time else 0
            status = pb.playback_status
            return {
                "title": (info.title or "").strip(),
                "artist": (info.artist or "").strip(),
                "album": (info.album_title or "").strip(),
                "position_ms": max(0, position_ms),
                "duration_ms": max(0, duration_ms),
                "playing": status == _SmtcPlaybackStatus.PLAYING,
                "paused": status == _SmtcPlaybackStatus.PAUSED,
                "status": int(status),
            }
        return asyncio.run(_read())
    except Exception as e:
        print(f"SMTC 读取失败: {e}")
        return None


class SmtcMonitor(QThread):
    """后台轮询 SMTC：检测切歌并同步播放状态。"""
    song_changed = pyqtSignal(object)
    state_updated = pyqtSignal(object)
    POLL_INTERVAL = 1.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._last_key = None

    def stop(self):
        self._running = False
        self.wait(3000)

    def run(self):
        if SMTC_AVAILABLE:
            try:
                from winrt.runtime import ApartmentType, init_apartment
                init_apartment(ApartmentType.MTA)
            except Exception:
                pass
        stall = 0
        last_pos = -1
        while self._running:
            media = smtc_read_once()
            if self._running and media:
                key = (media["title"], media["artist"], media["album"])
                playing = media["playing"]
                pos = media["position_ms"]
                dur = media["duration_ms"]
                # 播放中位置一直不前进（如恒为 0）或完全没有时间轴信息 → 时间轴不可靠
                if playing and pos == last_pos:
                    stall += 1
                else:
                    stall = 0
                last_pos = pos
                media["timeline_ok"] = not ((pos == 0 and dur == 0) or (playing and stall >= 2 and pos == 0))
                if key != self._last_key:
                    self._last_key = key
                    stall = 0
                    last_pos = -1
                    self.song_changed.emit(media)
                else:
                    self.state_updated.emit(media)
            elif self._running:
                self._last_key = None
            for _ in range(int(self.POLL_INTERVAL * 10)):
                if not self._running:
                    break
                time.sleep(0.1)

# ==================== 歌词搜索引擎 ====================
class LyricSearchEngine:
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    @staticmethod
    def search_netease(song_name, artist=""):
        try:
            keyword = f"{song_name} {artist}".strip()
            url = f"http://music.163.com/api/search/get?s={urllib.parse.quote(keyword)}&type=1&limit=1"
            resp = requests.get(url, headers=LyricSearchEngine.HEADERS, timeout=5)
            data = resp.json()
            songs = data.get('result', {}).get('songs', [])
            if songs:
                song = songs[0]
                song_id = song['id']
                duration = song.get('duration', 0)
                lyric_url = f"http://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
                lrc_resp = requests.get(lyric_url, headers=LyricSearchEngine.HEADERS, timeout=5)
                lrc_data = lrc_resp.json()
                lrc = lrc_data.get('lrc', {}).get('lyric', '')
                tlyric = lrc_data.get('tlyric', {}).get('lyric', '')
                return lrc, tlyric, duration
            return None, None, 0
        except:
            return None, None, 0

    @staticmethod
    def search_qq(song_name, artist=""):

        try:
            keyword = f"{song_name} {artist}".strip()
            search_url = f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?p=1&n=1&w={urllib.parse.quote(keyword)}&format=json"
            headers = {**LyricSearchEngine.HEADERS, 'Referer': 'https://y.qq.com/'}
            resp = requests.get(search_url, headers=headers, timeout=5)
            data = resp.json()
            songs = data.get('data', {}).get('song', {}).get('list', [])
            if songs:
                song = songs[0]
                songmid = song.get('songmid') or song.get('media_mid')
                duration = song.get('interval', 0) * 1000
                lyric_url = f"https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={songmid}&format=json&nobase64=1"
                lrc_resp = requests.get(lyric_url, headers=headers, timeout=5)
                lrc_data = lrc_resp.json()
                lyric = lrc_data.get('lyric', '')
                # 不需要 base64 解码
                if lyric and lyric.strip() and '[' in lyric:
                    return lyric, None, duration
            return None, None, 0
        except Exception as e:
            print(f"QQ搜索出错: {e}")
            return None, None, 0

    @staticmethod
    def search_kugou(song_name, artist=""):
        try:
            keyword = f"{song_name} {artist}".strip()
            search_url = f"http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword={urllib.parse.quote(keyword)}&page=1&pagesize=1"
            resp = requests.get(search_url, headers=LyricSearchEngine.HEADERS, timeout=5)
            data = resp.json()
            songs = data.get('data', {}).get('info', [])
            if songs:
                song = songs[0]
                song_hash = song.get('hash')
                duration = song.get('duration', 0) * 1000
                lyric_url = f"http://m.kugou.com/app/i/krc.php?cmd=100&hash={song_hash}&timelength=999999"
                lrc_resp = requests.get(lyric_url, headers=LyricSearchEngine.HEADERS, timeout=5)
                lyric = lrc_resp.text
                if lyric and 'krc' not in lyric and lyric.strip():
                    return lyric, None, duration
            return None, None, 0
        except:
            return None, None, 0

    @staticmethod
    def search(song_name, artist="", source="网易云", trans_only=False):
        if source == "网易云":
            lrc, tlyric, duration = LyricSearchEngine.search_netease(song_name, artist)
        elif source == "QQ音乐":
            lrc, tlyric, duration = LyricSearchEngine.search_qq(song_name, artist)
        elif source == "酷狗":
            lrc, tlyric, duration = LyricSearchEngine.search_kugou(song_name, artist)
        else:
            return None, 0

        if trans_only and tlyric and tlyric.strip():
            return tlyric, duration
        if lrc and lrc.strip():
            return lrc, duration
        if tlyric and tlyric.strip():
            return tlyric, duration
        return None, duration

# ==================== 抓取器 ====================
class LyricFetcher:
    @staticmethod
    def get_player_pid(player_name=None, players=None):
        import psutil
        if players is None:
            players = DEFAULT_PLAYERS
        if player_name and player_name in players:
            proc_name = players[player_name]["process"]
        else:
            return None
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] == proc_name:
                return proc.info['pid']
        return None

    @staticmethod
    def get_song_from_title(pid, player_name=None, players=None):
        if players is None:
            players = DEFAULT_PLAYERS
        pattern_str = r'^(.+?)\s*-\s*(.+)$'
        swap = False
        if player_name and player_name in players:
            pattern_str = players[player_name].get("pattern", pattern_str)
            swap = players[player_name].get("swap", False)
        pattern = re.compile(pattern_str)
        result = []
        def callback(hwnd, _):
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
            if window_pid == pid:
                title = win32gui.GetWindowText(hwnd)
                visible = win32gui.IsWindowVisible(hwnd)
                if visible and title:
                    result.append(title)
        win32gui.EnumWindows(callback, None)
        for title in result:
            print(f"DEBUG 标题: {title}")
            match = pattern.match(title)
            if match:
                groups = match.groups()
                if len(groups) >= 2:
                    if swap:
                        return groups[1].strip(), groups[0].strip()
                    return groups[0].strip(), groups[1].strip()
                elif len(groups) == 1:
                    return groups[0].strip(), ""
        return None, None
    

    @staticmethod
    def fetch_and_set(panel):
        panel.status.setText("状态：正在获取当前播放...")
        QApplication.processEvents()
        player_name = panel.player_combo.currentText()
        players = panel.players
        print(f"DEBUG: 播放器={player_name}, 配置={players.get(player_name)}")
        smtc_media = None
        song = None
        artist = None
        if player_name == SMTC_PLAYER_NAME:
            smtc_media = smtc_read_once()
            if smtc_media and smtc_media["title"]:
                song = smtc_media["title"]
                artist = smtc_media["artist"]
                print(f"DEBUG: SMTC 歌曲={song} 歌手={artist} 位置={smtc_media['position_ms']}ms")
        else:
            pid = LyricFetcher.get_player_pid(player_name, players)
            print(f"DEBUG: PID={pid}")
            if pid:
                song, artist = LyricFetcher.get_song_from_title(pid, player_name, players)
        if not song:
            text, ok = QInputDialog.getText(
                panel, "手动输入",
                "未能自动获取歌曲信息\n请输入 歌名 - 歌手：",
                text="歌名 - 歌手"
            )
            if ok and text.strip():
                parts = text.strip().split(' - ', 1)
                if len(parts) == 2:
                    song, artist = parts[0].strip(), parts[1].strip()
                else:
                    song = parts[0].strip()
                    artist = ""
            else:
                panel.status.setText("状态：已取消")
                return
        source = panel.source_combo.currentText()
        trans_only = panel.trans_check.isChecked()
        panel.status.setText(f"状态：从{source}搜索「{song}」...")
        QApplication.processEvents()
        lyric, duration = LyricSearchEngine.search(song, artist, source, trans_only)
        if lyric:
            panel.text_input.setPlainText(lyric)
            panel.lyric_window.song_duration = (smtc_media["duration_ms"] if smtc_media and smtc_media["duration_ms"] else duration)
            panel.status.setText(f"状态：已获取「{song}」的歌词 ")
            if panel.auto_switch_check.isChecked() or (smtc_media and panel.smtc_sync_check.isChecked()):
                panel.start(ignore_delay=bool(smtc_media))
                if smtc_media:
                    panel.lyric_window.sync_position(
                        smtc_media["position_ms"], smtc_media["playing"],
                        smtc_media["duration_ms"], smtc_media.get("timeline_ok", True))

# ==================== 默认预设 ====================
DEFAULT_PRESETS = {
    "通用": {'text': '#fffeef', 'stroke': '#d8a523', 'glow': '#d8a523'},
    "心碎": {'text': '#b223cb', 'stroke': '#991eaf', 'glow': '#b223cb'},
    "指令": {'text': '#00ffff', 'stroke': '#00aaff', 'glow': '#00aaff'}
}

# ==================== 配置读写 ====================
def load_all_config():
    if not os.path.exists(CONFIG_FILE):
        return {'settings': {}, 'presets': dict(DEFAULT_PRESETS), 'players': dict(DEFAULT_PLAYERS)}
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    for key in ['presets', 'players', 'settings']:
        if key not in data:
            data[key] = {}
    if not data['presets']:
        data['presets'] = dict(DEFAULT_PRESETS)
    if not data['players']:
        data['players'] = dict(DEFAULT_PLAYERS)
    return data

def save_all_config(panel, presets, players):
    data = {
        'settings': {
            'text_color': panel.current_color.name(),
            'stroke_color': panel.current_stroke_color.name(),
            'glow_color': panel.current_glow_color.name(),
            'glow_enabled': panel.glow_check.isChecked(),
            'glow_size': panel.glow_size_slider.value(),
            'glow_alpha': panel.glow_alpha_slider.value(),
            'loop': panel.loop_check.isChecked(),
            'trans_only': panel.trans_check.isChecked(),
            'mode': panel.mode_combo.currentData(),
            'font_family': panel.font_combo.currentFont().family(),
            'font_size': panel.font_size.value(),
            'stroke_width': panel.stroke_spin.value(),
            'spacing': panel.spacing_spin.value(),
            'shake_intensity': panel.shake_intensity_slider.value(),
            'shake_speed': panel.shake_speed_slider.value(),
            'fade_speed': panel.fade_speed_slider.value(),
            'rise_speed': panel.rise_speed_slider.value(),
            'margin_time': panel.margin_spin.value(),
            'max_interval': panel.max_interval_spin.value(),
            'max_duration': panel.max_duration_spin.value(),
            'angle_min': panel.angle_min.value(),
            'angle_max': panel.angle_max.value(),
            'player': panel.player_combo.currentText(),
            'source': panel.source_combo.currentText(),
            'delay': panel.delay_combo.currentIndex(),
            'perspective_enabled': panel.perspective_check.isChecked(),
            'persp_x_strength': panel.persp_x_slider.value(),
            'persp_y_strength': panel.persp_y_slider.value(),
            'persp_compensation': panel.persp_comp_slider.value(),
            'pos_x_min': panel.pos_x_min_s.value(),
            'pos_x_max': panel.pos_x_max_s.value(),
            'pos_y_min': panel.pos_y_min_s.value(),
            'pos_y_max': panel.pos_y_max_s.value(),
            'auto_switch': panel.auto_switch_check.isChecked(),
            'smtc_sync': panel.smtc_sync_check.isChecked(),
        },
        'presets': presets,
        'players': players
    }
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ==================== 解析 LRC ====================
def parse_lrc(lrc_text):
    lines = lrc_text.strip().split('\n')
    result = []
    pattern = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)')
    for line in lines:
        match = pattern.match(line.strip())
        if match:
            m = int(match.group(1))
            s = float(match.group(2))
            text = match.group(3).strip()
            if text:
                result.append((int((m*60+s)*1000), text))
    result.sort(key=lambda x: x[0])
    return result

# ==================== 上一句残留 ====================
class FadingLine:
    def __init__(self, text, font, x, y, angle, color, stroke_color,
                 stroke_width, mode, spacing, shake_intensity, fade_speed, rise_speed,
                 glow, glow_color, glow_size, glow_alpha, persp_transform=None):
        self.text = text
        self.font = font
        self.x = x
        self.y = y
        self.angle = angle
        self.color = QColor(color)
        self.stroke_color = QColor(stroke_color)
        self.stroke_width = stroke_width
        self.mode = mode
        self.spacing = spacing
        self.shake_intensity = shake_intensity
        self.fade_speed = fade_speed
        self.rise_speed = rise_speed
        self.glow = glow
        self.glow_color = QColor(glow_color)
        self.glow_size = glow_size
        self.glow_alpha = glow_alpha
        if persp_transform is None or isinstance(persp_transform, int):
            self.persp_transform = QTransform()
        else:
            self.persp_transform = persp_transform
        self.alpha = 255
        self.char_shakes = [{'x': 0, 'y': 0, 'target_x': 0, 'target_y': 0} for _ in text]

    def update(self):
        self.alpha = max(0, self.alpha - self.fade_speed)
        self.y -= self.rise_speed
        for s in self.char_shakes:
            s['target_x'] = random.randint(-self.shake_intensity, self.shake_intensity)
            s['target_y'] = random.randint(-self.shake_intensity, self.shake_intensity)
            s['x'] += (s['target_x'] - s['x']) * 0.3
            s['y'] += (s['target_y'] - s['y']) * 0.3
        return self.alpha > 0

    def draw(self, painter):
        if self.alpha <= 0:
            return
        painter.save()
        if self.persp_transform and not self.persp_transform.isIdentity():
            painter.setTransform(self.persp_transform, True)
        painter.translate(self.x, self.y)
        font = QFont(self.font)
        fm = QFontMetrics(font)
        th = fm.height()
        angle_rad = math.radians(self.angle)
        shadow_c = QColor(self.stroke_color)
        shadow_c.setAlpha(self.alpha)
        text_c = QColor(self.color)
        text_c.setAlpha(self.alpha)

        cursor = 0
        for ch in self.text:
            cw = fm.horizontalAdvance(ch)
            ox = cursor * math.cos(angle_rad)
            oy = cursor * math.sin(angle_rad)

            # 发光
            if self.glow:
                glow_c = QColor(self.glow_color)
                glow_c.setAlpha(int(self.alpha * self.glow_alpha / 255))
                path_glow = QPainterPath()
                path_glow.addText(ox, oy + th/3, font, ch)
                pen = QPen()
                pen.setColor(glow_c)
                pen.setWidthF(self.glow_size)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawPath(path_glow)

            # 阴影
            if self.mode == 'chinese':
                # 中文阴影
                path = QPainterPath()
                path.addText(ox + 3, oy + 3 + th/3, font, ch)
                painter.setPen(Qt.NoPen)
                painter.setBrush(shadow_c)
                painter.drawPath(path)
                path = QPainterPath()
                path.addText(ox, oy + th/3, font, ch)
                painter.setPen(Qt.NoPen)
                painter.setBrush(text_c)
                painter.drawPath(path)
            else:
                # 英文描边淡出：描边保持，填充变透明
                fill_c = QColor(text_c)
                fill_c.setAlpha(max(0, self.alpha - 150))
                path = QPainterPath()
                path.addText(ox, oy + th/3, font, ch)
                pen = QPen()
                pen.setColor(shadow_c)
                pen.setWidthF(self.stroke_width * 2)
                painter.setPen(pen)
                painter.setBrush(fill_c)
                painter.drawPath(path)
            cursor += cw + self.spacing
        painter.restore()

# ==================== 歌词悬浮窗 ====================
class LyricWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("歌词悬浮窗")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self.font = QFont("Microsoft YaHei", 28, QFont.Bold)
        self.text_color = QColor("#fffeef"); self.stroke_color = QColor("#d8a523")
        self.stroke_width = 0.5; self.angle_min = -10; self.angle_max = 10
        self.margin_time = 4000; self.max_interval = 16000; self.max_duration = 5000
        self.mode = 'chinese'; self.spacing = 5.0
        self.shake_intensity = 2; self.shake_speed = 143
        self.fade_speed = 12; self.rise_speed = 1
        self.glow = True; self.glow_color = QColor("#d8a523")
        self.glow_size = 4; self.glow_alpha = 82
        self.loop = True; self.song_duration = 0; self.start_delay = 0
        self.full_text = ""; self.char_index = 0
        self.x = 500; self.y = 300; self.angle = 0
        self.char_timer = QTimer(self); self.char_timer.timeout.connect(self.show_next_char)
        self.shake_timer = QTimer(self); self.shake_timer.timeout.connect(self.update_shake)
        self.lyric_timeline = []; self.current_line = 0
        self.line_timer = QTimer(self); self.line_timer.timeout.connect(self.check_lyric_time)
        self.start_time = 0; self.char_shakes = []
        self.fading_lines = []
        self.fade_timer = QTimer(self); self.fade_timer.timeout.connect(self.update_fading)
        self.fade_timer.start(30)
        self.perspective_enabled = True
        self.persp_x_strength = 0.00005
        self.persp_y_strength = 0.0003
        self.persp_compensation = 0.03
        self.persp_transform = QTransform()
        self.screen_w = screen.width()
        self.screen_h = screen.height()
        self.pos_x_min = 0
        self.pos_x_max = 100
        self.pos_y_min = 0
        self.pos_y_max = 100
        self.auto_switch_enabled = False    # 是否启用自动切换
        self.auto_switch_delay = 1          # 播完后等待秒数
        self.auto_switch_callback = None    # 回调：通知控制面板获取歌词
        self._skip_seconds = 0              # 新歌词需要跳过的秒数
        self._song_finished = False         # 是否已触发结束等待
        self._song_end_time = 0             # 歌曲结束的时间点
        self._ext_paused_elapsed = None     # 外部暂停时的本地进度（恢复/不可靠时间轴用）      
        

    def init_char_shakes(self):
        self.char_shakes = [{'x': 0, 'y': 0, 'target_x': 0, 'target_y': 0} for _ in self.full_text]

    def start_lyric(self, text, font, color, stroke_color, stroke_width,
                    angle_min, angle_max, margin_time, max_interval, max_duration,
                    mode, spacing, shake_intensity, shake_speed,
                    fade_speed, rise_speed, glow, glow_color, glow_size, glow_alpha,
                    start_delay=0):
        self.start_delay = start_delay
        if self.start_delay > 0:
            self.full_text = ""; self.char_index = 0
            self.lyric_timeline = []; self.update()
            QTimer.singleShot(int(self.start_delay * 1000),
                lambda: self._actually_start(text, font, color, stroke_color, stroke_width,
                    angle_min, angle_max, margin_time, max_interval, max_duration,
                    mode, spacing, shake_intensity, shake_speed,
                    fade_speed, rise_speed, glow, glow_color, glow_size, glow_alpha))
            return
        self._actually_start(text, font, color, stroke_color, stroke_width,
            angle_min, angle_max, margin_time, max_interval, max_duration,
            mode, spacing, shake_intensity, shake_speed,
            fade_speed, rise_speed, glow, glow_color, glow_size, glow_alpha)

    def _actually_start(self, text, font, color, stroke_color, stroke_width,
                        angle_min, angle_max, margin_time, max_interval, max_duration,
                        mode, spacing, shake_intensity, shake_speed,
                        fade_speed, rise_speed, glow, glow_color, glow_size, glow_alpha):
        self.font = font
        self.text_color = color
        self.stroke_color = stroke_color
        self.stroke_width = stroke_width
        self.angle_min = angle_min
        self.angle_max = angle_max
        self.margin_time = margin_time
        self.max_interval = max_interval
        self.max_duration = max_duration
        self.mode = mode
        self.spacing = spacing
        self.shake_intensity = shake_intensity
        self.shake_speed = shake_speed
        self.fade_speed = fade_speed
        self.rise_speed = rise_speed
        self.glow = glow
        self.glow_color = glow_color
        self.glow_size = glow_size
        self.glow_alpha = glow_alpha

        self.lyric_timeline = parse_lrc(text)
        self.fading_lines = []

        # 自动切换：跳过前 x 秒的歌词
        if self._skip_seconds > 0 and self.lyric_timeline:
            skip_ms = self._skip_seconds * 1000
            for i, (t, txt) in enumerate(self.lyric_timeline):
                if t >= skip_ms:
                    self.lyric_timeline = self.lyric_timeline[i:]
                    break

        if not self.lyric_timeline:
            self.full_text = text
            self.char_index = 0
            self.init_char_shakes()
            self.place_randomly()
            self.char_timer.start(50)
            self.shake_timer.start(self.shake_speed)
            return

        self.current_line = 0
        self.char_index = 0
        self.full_text = ""
        self._ext_paused_elapsed = None
        self.update()

        # 根据跳过量调整起始时间
        if self._skip_seconds > 0:
            real_elapsed = time.time() - self._song_end_time
            self.start_time = time.time() * 1000 - real_elapsed * 1000
            self._skip_seconds = 0
        else:
            self.start_time = 0

        self.line_timer.start(50)
    def compute_perspective(self):
        if not self.perspective_enabled:
            self.persp_transform = QTransform()
            return
        rel_x = (self.x - self.screen_w / 2) / (self.screen_w / 2)
        rel_y = (self.y - self.screen_h / 2) / (self.screen_h / 2)
        persp_x = self.persp_x_strength * rel_x
        persp_y = self.persp_y_strength * rel_y
        scale_x = 1.0 + self.persp_compensation * max(0, rel_x)
        self.persp_transform = QTransform()
        self.persp_transform.setMatrix(scale_x, 0, persp_x,
                                       0, 1, persp_y,
                                       0, 0, 1)
    def place_randomly(self):
        sw = self.width()
        sh = self.height()
        px_min = self.pos_x_min
        px_max = self.pos_x_max
        py_min = self.pos_y_min
        py_max = self.pos_y_max
        self.x = random.randint(int(sw * px_min / 100), int(sw * px_max / 100))
        self.y = random.randint(int(sh * py_min / 100), int(sh * py_max / 100))
        self.angle = random.randint(self.angle_min, self.angle_max)
        self.compute_perspective()

    def check_lyric_time(self):
        if self.start_time == 0: self.start_time = time.time() * 1000
        elapsed = time.time() * 1000 - self.start_time
        while (self.current_line < len(self.lyric_timeline) and
               self.lyric_timeline[self.current_line][0] <= elapsed):
            if self.full_text and self.char_index > 0:
                fading = FadingLine(self.full_text, self.font, self.x, self.y, self.angle,
                    self.text_color, self.stroke_color, self.stroke_width,
                    self.mode, self.spacing, self.shake_intensity,
                    self.fade_speed, self.rise_speed,
                    self.glow, self.glow_color, self.glow_size, self.glow_alpha,
    self.persp_transform)
                self.fading_lines.append(fading)
            line_text = self.lyric_timeline[self.current_line][1]
            self.full_text = line_text; self.char_index = 0
            self.init_char_shakes(); self.place_randomly()
            if self.current_line + 1 < len(self.lyric_timeline):
                next_time = self.lyric_timeline[self.current_line + 1][0]
                current_time = self.lyric_timeline[self.current_line][0]
                interval = next_time - current_time
                if interval > self.max_interval:
                    char_count = len(line_text)
                    calc_speed = max(10, int(self.max_duration / char_count)) if char_count > 0 else 50
                else:
                    interval -= self.margin_time
                    char_count = len(line_text)
                    calc_speed = max(10, int(interval / char_count)) if char_count > 0 else 50
            else:
                calc_speed = 10
            self.char_timer.start(calc_speed)
            self.shake_timer.start(self.shake_speed)
            self.current_line += 1
        if self.current_line >= len(self.lyric_timeline):
            elapsed_total = time.time() * 1000 - self.start_time

            # 歌曲结束后的等待检测
            if self._song_finished and self.auto_switch_enabled:
                waited = time.time() - self._song_end_time
                if waited >= self.auto_switch_delay:
                    self._skip_seconds = self.auto_switch_delay
                    self._song_finished = False
                    if self.auto_switch_callback:
                        self.auto_switch_callback()
                    self._skip_seconds = 0

            # 单曲循环
            if self.loop and self.song_duration > 0:
                if elapsed_total >= self.song_duration:
                    self.current_line = 0
                    self.char_index = 0
                    self.full_text = ""
                    self.start_time = time.time() * 1000
                    self.fading_lines = []
                    self.update()

            # 自动切换
            elif self.auto_switch_enabled and self.song_duration > 0:
                if elapsed_total >= self.song_duration and not self._song_finished:
                    self._song_finished = True
                    self._song_end_time = time.time()
                    self.start_time = time.time() * 1000
                    self.current_line = 0
                    self.char_index = 0
                    self.full_text = ""
                    self.lyric_timeline = []
                    print(f"歌曲实际播完，等待 {self.auto_switch_delay} 秒...")

            # 都不开，停止
            elif not self.loop and not self.auto_switch_enabled:
                self.line_timer.stop()
            

    def show_next_char(self):
        if self.char_index < len(self.full_text):
            self.char_index += 1; self.update()
        else:
            self.char_timer.stop()

    def update_shake(self):
        if not self.full_text or self.char_index == 0: return
        for s in self.char_shakes:
            s['target_x'] = random.randint(-self.shake_intensity, self.shake_intensity)
            s['target_y'] = random.randint(-self.shake_intensity, self.shake_intensity)
            s['x'] += (s['target_x'] - s['x']) * 0.3
            s['y'] += (s['target_y'] - s['y']) * 0.3
        self.update()

    def update_fading(self):
        if not self.fading_lines: return
        self.fading_lines = [f for f in self.fading_lines if f.update()]
        self.update()

    def stop_lyric(self):
        self._ext_paused_elapsed = None
        self.char_timer.stop(); self.shake_timer.stop(); self.line_timer.stop()
        self.full_text = ""; self.char_index = 0
        self.lyric_timeline = []; self.current_line = 0
        self.fading_lines = []; self.char_shakes = []
        self.update()

    def sync_position(self, position_ms, playing, duration_ms=0, timeline_ok=True):
        """按外部(SMTC)播放位置与状态同步时间轴；时间轴不可靠时退回本地计时。"""
        position_ms = max(0, position_ms)
        if duration_ms and duration_ms > 0:
            self.song_duration = duration_ms
        now_ms = time.time() * 1000
        trusted = timeline_ok and (position_ms > 0 or duration_ms > 0)
        if trusted:
            self.start_time = now_ms - position_ms
            self._ext_paused_elapsed = None
        if playing:
            if not trusted and self._ext_paused_elapsed is not None:
                # 暂停后恢复且无外部进度：从暂停点继续本地计时
                self.start_time = now_ms - self._ext_paused_elapsed
                self._ext_paused_elapsed = None
            if not self.line_timer.isActive():
                self.line_timer.start(50)
            if self.full_text and not self.char_timer.isActive() and self.char_index < len(self.full_text):
                self.char_timer.start(50)
            if self.full_text and not self.shake_timer.isActive():
                self.shake_timer.start(self.shake_speed)
        else:
            if self._ext_paused_elapsed is None:
                if trusted:
                    self._ext_paused_elapsed = position_ms
                else:
                    self._ext_paused_elapsed = (now_ms - self.start_time) if self.start_time else 0
            self.line_timer.stop()
            self.char_timer.stop()
            self.shake_timer.stop()
        if not self.lyric_timeline:
            return
        if trusted:
            # 定位到当前位置对应的行（仅在有可靠外部进度时重排，支持快进/快退/切歌）
            target = 0
            for i, (t, _txt) in enumerate(self.lyric_timeline):
                if t <= position_ms:
                    target = i
                else:
                    break
            if target != self.current_line:
                self.current_line = target
                self.full_text = self.lyric_timeline[target][1] if self.lyric_timeline else ""
                self.char_index = len(self.full_text)
                self.init_char_shakes()
                self.place_randomly()
                self.char_timer.stop()
                if playing:
                    self.shake_timer.start(self.shake_speed)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        for f in self.fading_lines: f.draw(painter)
        if self.char_index == 0 or not self.full_text: return
        draw_text = self.full_text[:self.char_index]
        font = QFont(self.font); fm = QFontMetrics(font)
        th = fm.height(); angle_rad = math.radians(self.angle)
        painter.save()
        if self.perspective_enabled:
            painter.setTransform(self.persp_transform, True)
        painter.translate(self.x, self.y)
        if self.mode == 'chinese':
            shadow_c = QColor(self.stroke_color); text_c = QColor(self.text_color)
        else:
            stroke_c = QColor(self.stroke_color); fill_c = QColor(self.text_color)
        cursor = 0
        for i, ch in enumerate(draw_text):
            sx = self.char_shakes[i]['x'] if i < len(self.char_shakes) else 0
            sy = self.char_shakes[i]['y'] if i < len(self.char_shakes) else 0
            cw = fm.horizontalAdvance(ch)
            ox = cursor * math.cos(angle_rad); oy = cursor * math.sin(angle_rad)
            if self.glow:
                glow_c = QColor(self.glow_color); gs = self.glow_size
                gc = QColor(glow_c); gc.setAlpha(self.glow_alpha)
                path_glow = QPainterPath()
                path_glow.addText(ox + sx, oy + sy + th/3, font, ch)
                pen_g = QPen(QColor(gc), gs)
                painter.setPen(pen_g); painter.setBrush(Qt.NoBrush)
                painter.drawPath(path_glow)
            if self.mode == 'chinese':
                path_shadow = QPainterPath()
                path_shadow.addText(ox + sx + 3, oy + sy + 3 + th/3, font, ch)
                painter.setPen(Qt.NoPen); painter.setBrush(shadow_c)
                painter.drawPath(path_shadow)
                path_text = QPainterPath()
                path_text.addText(ox + sx, oy + sy + th/3, font, ch)
                painter.setPen(Qt.NoPen); painter.setBrush(text_c)
                painter.drawPath(path_text)
            else:
                path = QPainterPath()
                path.addText(ox + sx, oy + sy + th/3, font, ch)
                pen = QPen(stroke_c, self.stroke_width * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                painter.setPen(pen); painter.setBrush(fill_c)
                painter.drawPath(path)
            cursor += cw + self.spacing
        painter.restore()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape: QApplication.quit()

# ==================== 控制面板 ====================
PANEL_QSS = """
QWidget#panelBase {
    background-color: #14161c;
}
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QWidget {
    color: #e8e6e0;
    font-family: "Microsoft YaHei";
    font-size: 13px;
}
QLabel { color: #cfcdc7; background: transparent; }
QWidget#header {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1b1e27, stop:1 #20232e);
    border: 1px solid #2a2e3a;
    border-bottom: 2px solid #d8a523;
    border-radius: 8px;
}
QLabel#appTitle { color: #fffeef; font-size: 22px; font-weight: bold; letter-spacing: 2px; }
QLabel#appSubtitle { color: #d8a523; font-size: 11px; }
QGroupBox {
    background-color: #1a1d26;
    border: 1px solid #2a2e3a;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 6px;
    padding-bottom: 4px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #d8a523;
    font-weight: bold;
    background: transparent;
}
QPushButton {
    background-color: #262b37;
    color: #e8e6e0;
    border: 1px solid #333a48;
    border-radius: 6px;
    padding: 5px 12px;
}
QPushButton:hover { background-color: #2f3645; border-color: #d8a523; }
QPushButton:pressed { background-color: #1f242f; }
QPushButton#fetchBtn {
    background-color: #d8a523;
    color: #14161c;
    font-weight: bold;
    border: none;
    padding: 9px;
    border-radius: 7px;
    font-size: 14px;
}
QPushButton#fetchBtn:hover { background-color: #e5b53a; }
QPushButton#startBtn {
    background-color: #2e9e5b;
    color: white;
    font-weight: bold;
    border: none;
    padding: 10px;
    border-radius: 8px;
    font-size: 14px;
}
QPushButton#startBtn:hover { background-color: #35b569; }
QPushButton#stopBtn {
    background-color: #d64545;
    color: white;
    font-weight: bold;
    border: none;
    padding: 10px;
    border-radius: 8px;
    font-size: 14px;
}
QPushButton#stopBtn:hover { background-color: #e55656; }
QPushButton#debugBtn {
    background-color: #262b37;
    border: 1px solid #4a5264;
    border-radius: 8px;
    padding: 10px 16px;
}
QPushButton#debugBtn:hover { border-color: #d8a523; }
QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QFontComboBox {
    background-color: #12141a;
    color: #e8e6e0;
    border: 1px solid #333a48;
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: #d8a523;
    selection-color: #14161c;
}
QTextEdit { padding: 6px; }
QComboBox::drop-down, QFontComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow {
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #d8a523;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background-color: #1a1d26;
    color: #e8e6e0;
    border: 1px solid #333a48;
    selection-background-color: #d8a523;
    selection-color: #14161c;
}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    background: #262b37; border: none; width: 16px;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid #d8a523;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #d8a523;
}
QCheckBox { spacing: 6px; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid #4a5264; border-radius: 4px; background: #12141a;
}
QCheckBox::indicator:hover { border-color: #d8a523; }
QCheckBox::indicator:checked { background: #d8a523; border-color: #d8a523; }
QCheckBox::indicator:disabled { background: #1a1d26; border-color: #333a48; }
QSlider::groove:horizontal { height: 5px; background: #2a2e3a; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #d8a523; border-radius: 3px; }
QSlider::handle:horizontal {
    width: 14px; height: 14px; margin: -5px 0;
    background: #d8a523; border-radius: 7px;
}
QSlider::handle:horizontal:hover { background: #e5b53a; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #333a48; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #d8a523; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QLabel#statusLabel {
    background: #12141a;
    border: 1px solid #2a2e3a;
    border-radius: 8px;
    padding: 8px;
    color: #e8e6e0;
}
QLabel#licenseLabel { color: #5a6272; font-size: 11px; }
"""


class ControlPanel(QWidget):
    def on_loop_changed(self, state):
        is_checked = state == Qt.Checked
        self.lyric_window.loop = is_checked
        if is_checked:
            self.auto_switch_check.blockSignals(True)
            self.auto_switch_check.setChecked(False)
            self.auto_switch_check.blockSignals(False)
            self.lyric_window.auto_switch_enabled = False

    def on_auto_switch_changed(self, state):
        is_checked = state == Qt.Checked
        self.lyric_window.auto_switch_enabled = is_checked
        if is_checked:
            self.loop_check.blockSignals(True)
            self.loop_check.setChecked(False)
            self.loop_check.blockSignals(False)
            self.lyric_window.loop = False

    def on_auto_switch(self):
        self.status.setText("状态：检测到切歌，自动获取...")
        QApplication.processEvents()
        # 清掉旧歌词，强制重新获取
        self.text_input.clear()
        self.lyric_window.stop_lyric()
        LyricFetcher.fetch_and_set(self)

    def on_smtc_sync_changed(self, state):
        is_checked = state == Qt.Checked
        if is_checked:
            # SMTC 自动同步取代“自动切换歌曲”
            self.auto_switch_check.blockSignals(True)
            self.auto_switch_check.setChecked(False)
            self.auto_switch_check.blockSignals(False)
            self.lyric_window.auto_switch_enabled = False
            if self.smtc_monitor is None:
                self.smtc_monitor = SmtcMonitor(self)
                self.smtc_monitor.song_changed.connect(self.on_smtc_song_changed)
                self.smtc_monitor.state_updated.connect(self.on_smtc_state_updated)
                self.smtc_monitor.start()
            self.status.setText("状态：SMTC 自动同步已开启，正在监听媒体播放...")
        else:
            if self.smtc_monitor is not None:
                self.smtc_monitor.stop()
                self.smtc_monitor = None
            self.status.setText("状态：SMTC 自动同步已关闭")

    def on_smtc_song_changed(self, media):
        title = (media or {}).get("title", "")
        if not title:
            return
        self.status.setText(f"状态：SMTC 检测到「{title}」...")
        QApplication.processEvents()
        self.text_input.clear()
        self.lyric_window.stop_lyric()
        source = self.source_combo.currentText()
        trans_only = self.trans_check.isChecked()
        self.status.setText(f"状态：从{source}搜索「{title}」...")
        QApplication.processEvents()
        lyric, duration = LyricSearchEngine.search(title, media.get("artist", ""), source, trans_only)
        if lyric:
            self.text_input.setPlainText(lyric)
            self.lyric_window.song_duration = media.get("duration_ms") or duration
            self.start(ignore_delay=True)
            self.lyric_window.sync_position(
                media.get("position_ms", 0), media.get("playing", True),
                media.get("duration_ms", 0), media.get("timeline_ok", True))
            self.status.setText(f"状态：已获取并同步「{title}」")
        else:
            self.status.setText(f"状态：未找到「{title}」的歌词")

    def on_smtc_state_updated(self, media):
        if media is None:
            return
        self.lyric_window.sync_position(
            media.get("position_ms", 0), media.get("playing", True),
            media.get("duration_ms", 0), media.get("timeline_ok", True))
    def debug_info(self):
        w = self.lyric_window
        info = f"""
        === Debug Info ===
        auto_switch_enabled: {w.auto_switch_enabled}
        auto_switch_delay:   {w.auto_switch_delay}
        _song_finished:      {w._song_finished}
        _song_end_time:      {w._song_end_time}
        _skip_seconds:       {w._skip_seconds}
        song_duration:       {w.song_duration}
        loop:                {w.loop}
        current_line:        {w.current_line}
        timeline 长度:       {len(w.lyric_timeline)}
        start_time:          {w.start_time}
        elapsed:             {time.time() * 1000 - w.start_time if w.start_time > 0 else 0:.0f} ms
        ==================
        """
        print(info)
        self.status.setText("Debug 信息已输出到控制台")    

    def __init__(self):
        super().__init__()
        self.setWindowTitle("歌词字幕器 - 控制面板")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setObjectName("panelBase")
        self.setStyleSheet(PANEL_QSS)
        self.current_color = QColor("#fffeef"); self.current_stroke_color = QColor("#d8a523")
        self.current_glow_color = QColor("#d8a523")
        all_data = load_all_config()
        self.presets = all_data['presets']; self.players = all_data.get('players', dict(DEFAULT_PLAYERS))
        settings = all_data['settings']
        self.lyric_window = LyricWindow(); self.lyric_window.show()
        self.lyric_window.auto_switch_callback = self.on_auto_switch
        self.smtc_monitor = None
        outer_layout = QVBoxLayout(self); outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content); layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        # ============ 顶部横幅 ============
        header = QWidget(); header.setObjectName("header")
        header_layout = QVBoxLayout(header); header_layout.setContentsMargins(14, 12, 14, 12)
        header_layout.setSpacing(2)
        title = QLabel("歌词字幕器"); title.setObjectName("appTitle")
        header_layout.addWidget(title)
        subtitle = QLabel("Limbus-Like Lyric Simulator · 悬浮歌词渲染 · 支持 SMTC 同步")
        subtitle.setObjectName("appSubtitle")
        header_layout.addWidget(subtitle)
        layout.addWidget(header)

        # 界面缩放
        zoom_layout = QHBoxLayout()
        zoom_layout.addWidget(QLabel("界面缩放："))
        self.zoom_slider = QSlider(Qt.Horizontal); self.zoom_slider.setRange(70, 150)
        self.zoom_slider.setValue(100); self.zoom_label = QLabel("100%")
        self.zoom_slider.valueChanged.connect(lambda v: self.zoom_label.setText(f"{v}%"))
        self.zoom_slider.sliderReleased.connect(self.apply_zoom)
        zoom_layout.addWidget(self.zoom_slider); zoom_layout.addWidget(self.zoom_label)
        layout.addLayout(zoom_layout)

        # ============ 媒体与歌词 ============
        media_box = QGroupBox("媒体与歌词")
        media_layout = QVBoxLayout(media_box); media_layout.setSpacing(8)
        player_layout = QHBoxLayout()
        player_layout.addWidget(QLabel("播放器："))
        self.player_combo = QComboBox(); self.refresh_player_list()
        player_layout.addWidget(self.player_combo)
        btn_add_p = QPushButton("+"); btn_add_p.setMaximumWidth(34)
        btn_add_p.setToolTip("添加自定义播放器")
        btn_add_p.clicked.connect(self.add_custom_player); player_layout.addWidget(btn_add_p)
        btn_del_p = QPushButton("-"); btn_del_p.setMaximumWidth(34)
        btn_del_p.setToolTip("删除播放器")
        btn_del_p.clicked.connect(self.delete_player); player_layout.addWidget(btn_del_p)
        player_layout.addStretch()
        media_layout.addLayout(player_layout)
        source_layout = QHBoxLayout()
        source_layout.addWidget(QLabel("歌词源："))
        self.source_combo = QComboBox()
        self.source_combo.addItems(["网易云", "QQ音乐", "酷狗"])
        source_layout.addWidget(self.source_combo)
        source_layout.addStretch()
        media_layout.addLayout(source_layout)
        media_layout.addWidget(QLabel("歌词（粘贴LRC格式）："))
        self.text_input = QTextEdit(); self.text_input.setMinimumHeight(120)
        media_layout.addWidget(self.text_input)
        fetch_btn = QPushButton("🎵 从播放器获取当前歌词")
        fetch_btn.setObjectName("fetchBtn")
        fetch_btn.clicked.connect(self.fetch_lyric); media_layout.addWidget(fetch_btn)
        layout.addWidget(media_box)

        # ============ 播放选项 ============
        play_box = QGroupBox("播放选项")
        play_layout = QVBoxLayout(play_box); play_layout.setSpacing(6)
        options_row = QHBoxLayout()
        self.trans_check = QCheckBox("翻译歌词"); self.trans_check.setToolTip("仅支持网易云词源")
        self.trans_check.setChecked(False)
        options_row.addWidget(self.trans_check)
        self.loop_check = QCheckBox("单曲循环"); self.loop_check.setChecked(True)
        self.loop_check.stateChanged.connect(self.on_loop_changed)
        options_row.addWidget(self.loop_check)
        self.auto_switch_check = QCheckBox("自动切换歌曲"); self.auto_switch_check.setChecked(False)
        self.auto_switch_check.stateChanged.connect(self.on_auto_switch_changed)
        options_row.addWidget(self.auto_switch_check)
        options_row.addStretch()
        play_layout.addLayout(options_row)
        opts2 = QHBoxLayout()
        self.smtc_sync_check = QCheckBox("SMTC 自动同步")
        self.smtc_sync_check.setEnabled(SMTC_AVAILABLE)
        if not SMTC_AVAILABLE:
            self.smtc_sync_check.setToolTip("需要安装 winrt-Windows.Media.Control 才能使用 SMTC")
        self.smtc_sync_check.stateChanged.connect(self.on_smtc_sync_changed)
        opts2.addWidget(self.smtc_sync_check)
        delay_layout = QHBoxLayout(); delay_layout.addWidget(QLabel("启动延时："))
        self.delay_combo = QComboBox()
        self.delay_combo.addItems(["0s", "1s", "2s", "3s", "5s"])
        delay_layout.addWidget(self.delay_combo)
        opts2.addLayout(delay_layout)
        opts2.addStretch()
        play_layout.addLayout(opts2)
        layout.addWidget(play_box)

        # ============ 渲染设置 ============
        render_box = QGroupBox("渲染设置")
        render_layout = QVBoxLayout(render_box); render_layout.setSpacing(6)
        self.perspective_check = QCheckBox("启用 3D 透视"); self.perspective_check.setChecked(True)
        self.perspective_check.stateChanged.connect(
            lambda state: setattr(self.lyric_window, 'perspective_enabled', state == Qt.Checked))
        render_layout.addWidget(self.perspective_check)
        px_layout = QHBoxLayout()
        px_layout.addWidget(QLabel("透视X："))
        self.persp_x_slider = QSlider(Qt.Horizontal)
        self.persp_x_slider.setRange(0, 100)
        self.persp_x_slider.setValue(5)
        self.persp_x_label = QLabel("0.00005")
        self.persp_x_slider.valueChanged.connect(
            lambda v: (setattr(self.lyric_window, 'persp_x_strength', v/1000000),
                       self.persp_x_label.setText(f"{v/1000000:.6f}")))
        px_layout.addWidget(self.persp_x_slider)
        px_layout.addWidget(self.persp_x_label)
        render_layout.addLayout(px_layout)
        py_layout = QHBoxLayout()
        py_layout.addWidget(QLabel("透视Y："))
        self.persp_y_slider = QSlider(Qt.Horizontal)
        self.persp_y_slider.setRange(0, 100)
        self.persp_y_slider.setValue(30)
        self.persp_y_label = QLabel("0.00030")
        self.persp_y_slider.valueChanged.connect(
            lambda v: (setattr(self.lyric_window, 'persp_y_strength', v/100000),
                       self.persp_y_label.setText(f"{v/100000:.5f}")))
        py_layout.addWidget(self.persp_y_slider)
        py_layout.addWidget(self.persp_y_label)
        render_layout.addLayout(py_layout)
        comp_layout = QHBoxLayout()
        comp_layout.addWidget(QLabel("水平补偿："))
        self.persp_comp_slider = QSlider(Qt.Horizontal)
        self.persp_comp_slider.setRange(0, 100)
        self.persp_comp_slider.setValue(3)
        self.persp_comp_label = QLabel("0.03")
        self.persp_comp_slider.valueChanged.connect(
            lambda v: (setattr(self.lyric_window, 'persp_compensation', v/100),
                       self.persp_comp_label.setText(f"{v/100:.2f}")))
        comp_layout.addWidget(self.persp_comp_slider)
        comp_layout.addWidget(self.persp_comp_label)
        render_layout.addLayout(comp_layout)
        fl = QHBoxLayout(); fl.addWidget(QLabel("字体："))
        self.font_combo = QFontComboBox(); self.font_combo.setCurrentFont(QFont("Microsoft YaHei"))
        fl.addWidget(self.font_combo); fl.addWidget(QLabel("大小："))
        self.font_size = QSpinBox(); self.font_size.setRange(10, 100); self.font_size.setValue(28)
        fl.addWidget(self.font_size); render_layout.addLayout(fl)
        fl_auto = QHBoxLayout()
        btn_auto_font = QPushButton("推荐字体")
        btn_auto_font.clicked.connect(self.auto_select_font)
        fl_auto.addWidget(btn_auto_font)
        fl_auto.addStretch()
        render_layout.addLayout(fl_auto)
        cl = QHBoxLayout(); cl.addWidget(QLabel("文字："))
        self.color_btn = QPushButton(); self.color_btn.setFixedSize(30, 30)
        self.color_btn.setObjectName("colorBtn")
        self.color_btn.setStyleSheet(f"background-color:{self.current_color.name()};border-radius:6px;border:1px solid #4a5264;")
        self.color_btn.clicked.connect(self.pick_color); cl.addWidget(self.color_btn)
        cl.addWidget(QLabel("阴影："))
        self.stroke_btn = QPushButton(); self.stroke_btn.setFixedSize(30, 30)
        self.stroke_btn.setObjectName("colorBtn")
        self.stroke_btn.setStyleSheet(f"background-color:{self.current_stroke_color.name()};border-radius:6px;border:1px solid #4a5264;")
        self.stroke_btn.clicked.connect(self.pick_stroke); cl.addWidget(self.stroke_btn)
        cl.addStretch(); render_layout.addLayout(cl)
        swl = QHBoxLayout(); swl.addWidget(QLabel("描边粗细："))
        self.stroke_spin = QDoubleSpinBox(); self.stroke_spin.setRange(0.0, 10.0)
        self.stroke_spin.setSingleStep(0.1); self.stroke_spin.setDecimals(1); self.stroke_spin.setValue(0.5)
        swl.addWidget(self.stroke_spin); swl.addWidget(QLabel("px")); render_layout.addLayout(swl)
        ssl = QHBoxLayout(); ssl.addWidget(QLabel("字间距："))
        self.spacing_spin = QDoubleSpinBox(); self.spacing_spin.setRange(-10.0, 30.0)
        self.spacing_spin.setSingleStep(0.5); self.spacing_spin.setDecimals(1); self.spacing_spin.setValue(5.0)
        ssl.addWidget(self.spacing_spin); ssl.addWidget(QLabel("px")); render_layout.addLayout(ssl)
        glow_layout = QHBoxLayout()
        self.glow_check = QCheckBox("发光"); self.glow_check.setChecked(True)
        glow_layout.addWidget(self.glow_check)
        glow_layout.addWidget(QLabel("光色："))
        self.glow_color_btn = QPushButton(); self.glow_color_btn.setFixedSize(30, 30)
        self.glow_color_btn.setObjectName("colorBtn")
        self.glow_color_btn.setStyleSheet(f"background-color:{self.current_glow_color.name()};border-radius:6px;border:1px solid #4a5264;")
        self.glow_color_btn.clicked.connect(self.pick_glow_color)
        glow_layout.addWidget(self.glow_color_btn); glow_layout.addStretch()
        render_layout.addLayout(glow_layout)
        gsl = QHBoxLayout(); gsl.addWidget(QLabel("光晕粗细："))
        self.glow_size_slider = QSlider(Qt.Horizontal); self.glow_size_slider.setRange(4, 30)
        self.glow_size_slider.setValue(4); self.glow_size_label = QLabel("4")
        self.glow_size_slider.valueChanged.connect(lambda v: self.glow_size_label.setText(str(v)))
        gsl.addWidget(self.glow_size_slider); gsl.addWidget(self.glow_size_label)
        render_layout.addLayout(gsl)
        gal = QHBoxLayout(); gal.addWidget(QLabel("光晕透明度："))
        self.glow_alpha_slider = QSlider(Qt.Horizontal); self.glow_alpha_slider.setRange(10, 120)
        self.glow_alpha_slider.setValue(82); self.glow_alpha_label = QLabel("82")
        self.glow_alpha_slider.valueChanged.connect(lambda v: self.glow_alpha_label.setText(str(v)))
        gal.addWidget(self.glow_alpha_slider); gal.addWidget(self.glow_alpha_label)
        render_layout.addLayout(gal)
        layout.addWidget(render_box)

        # ============ 动态效果 ============
        fx_box = QGroupBox("动态效果")
        fx_layout = QVBoxLayout(fx_box); fx_layout.setSpacing(6)
        shl = QHBoxLayout(); shl.addWidget(QLabel("颤强："))
        self.shake_intensity_slider = QSlider(Qt.Horizontal); self.shake_intensity_slider.setRange(0, 10)
        self.shake_intensity_slider.setValue(2); self.shake_intensity_label = QLabel("2")
        self.shake_intensity_slider.valueChanged.connect(lambda v: self.shake_intensity_label.setText(str(v)))
        shl.addWidget(self.shake_intensity_slider); shl.addWidget(self.shake_intensity_label)
        fx_layout.addLayout(shl)
        shvl = QHBoxLayout(); shvl.addWidget(QLabel("颤速："))
        self.shake_speed_slider = QSlider(Qt.Horizontal); self.shake_speed_slider.setRange(10, 200)
        self.shake_speed_slider.setValue(143); self.shake_speed_label = QLabel("143 ms")
        self.shake_speed_slider.valueChanged.connect(lambda v: self.shake_speed_label.setText(f"{v} ms"))
        shvl.addWidget(self.shake_speed_slider); shvl.addWidget(self.shake_speed_label)
        fx_layout.addLayout(shvl)
        fsl = QHBoxLayout(); fsl.addWidget(QLabel("淡出速度："))
        self.fade_speed_slider = QSlider(Qt.Horizontal); self.fade_speed_slider.setRange(1, 15)
        self.fade_speed_slider.setValue(12); self.fade_speed_label = QLabel("12")
        self.fade_speed_slider.valueChanged.connect(lambda v: self.fade_speed_label.setText(str(v)))
        fsl.addWidget(self.fade_speed_slider); fsl.addWidget(self.fade_speed_label)
        fx_layout.addLayout(fsl)
        rsl = QHBoxLayout(); rsl.addWidget(QLabel("上升速度："))
        self.rise_speed_slider = QSlider(Qt.Horizontal); self.rise_speed_slider.setRange(0, 5)
        self.rise_speed_slider.setValue(1); self.rise_speed_label = QLabel("1")
        self.rise_speed_slider.valueChanged.connect(lambda v: self.rise_speed_label.setText(str(v)))
        rsl.addWidget(self.rise_speed_slider); rsl.addWidget(self.rise_speed_label)
        fx_layout.addLayout(rsl)
        ml = QHBoxLayout(); ml.addWidget(QLabel("留白："))
        self.margin_spin = QSpinBox(); self.margin_spin.setRange(0, 5000)
        self.margin_spin.setValue(4000); self.margin_spin.setSingleStep(100)
        ml.addWidget(self.margin_spin); ml.addWidget(QLabel("ms")); ml.addStretch()
        fx_layout.addLayout(ml)
        mxl = QHBoxLayout(); mxl.addWidget(QLabel("最大间隔："))
        self.max_interval_spin = QSpinBox(); self.max_interval_spin.setRange(1000, 60000)
        self.max_interval_spin.setValue(16000); self.max_interval_spin.setSingleStep(500)
        mxl.addWidget(self.max_interval_spin); mxl.addWidget(QLabel("ms")); mxl.addStretch()
        fx_layout.addLayout(mxl)
        mdl = QHBoxLayout(); mdl.addWidget(QLabel("最大时长："))
        self.max_duration_spin = QSpinBox(); self.max_duration_spin.setRange(500, 30000)
        self.max_duration_spin.setValue(5000); self.max_duration_spin.setSingleStep(500)
        mdl.addWidget(self.max_duration_spin); mdl.addWidget(QLabel("ms")); mdl.addStretch()
        fx_layout.addLayout(mdl)
        al = QHBoxLayout(); al.addWidget(QLabel("角度："))
        self.angle_min = QSpinBox(); self.angle_min.setRange(-90, 90); self.angle_min.setValue(-10)
        al.addWidget(self.angle_min); al.addWidget(QLabel("~"))
        self.angle_max = QSpinBox(); self.angle_max.setRange(-90, 90); self.angle_max.setValue(10)
        al.addWidget(self.angle_max); al.addStretch(); fx_layout.addLayout(al)
        layout.addWidget(fx_box)

        # ============ 位置 ============
        pos_box = QGroupBox("歌词位置")
        pos_layout2 = QVBoxLayout(pos_box); pos_layout2.setSpacing(6)
        pos_layout = QHBoxLayout()
        pos_layout.addWidget(QLabel("X范围："))
        self.pos_x_min_s = QSlider(Qt.Horizontal)
        self.pos_x_min_s.setRange(0, 50); self.pos_x_min_s.setValue(5)
        self.pos_x_lbl = QLabel("5%")
        self.pos_x_min_s.valueChanged.connect(lambda v: (setattr(self.lyric_window, 'pos_x_min', v), self.pos_x_lbl.setText(f"{v}%")))
        pos_layout.addWidget(self.pos_x_min_s); pos_layout.addWidget(self.pos_x_lbl)
        pos_layout.addWidget(QLabel("~"))
        self.pos_x_max_s = QSlider(Qt.Horizontal)
        self.pos_x_max_s.setRange(50, 100); self.pos_x_max_s.setValue(85)
        self.pos_x_max_lbl = QLabel("85%")
        self.pos_x_max_s.valueChanged.connect(lambda v: (setattr(self.lyric_window, 'pos_x_max', v), self.pos_x_max_lbl.setText(f"{v}%")))
        pos_layout.addWidget(self.pos_x_max_s); pos_layout.addWidget(self.pos_x_max_lbl)
        pos_layout2.addLayout(pos_layout)
        pos_y_layout = QHBoxLayout()
        pos_y_layout.addWidget(QLabel("Y范围："))
        self.pos_y_min_s = QSlider(Qt.Horizontal)
        self.pos_y_min_s.setRange(0, 50); self.pos_y_min_s.setValue(5)
        self.pos_y_lbl = QLabel("5%")
        self.pos_y_min_s.valueChanged.connect(lambda v: (setattr(self.lyric_window, 'pos_y_min', v), self.pos_y_lbl.setText(f"{v}%")))
        pos_y_layout.addWidget(self.pos_y_min_s); pos_y_layout.addWidget(self.pos_y_lbl)
        pos_y_layout.addWidget(QLabel("~"))
        self.pos_y_max_s = QSlider(Qt.Horizontal)
        self.pos_y_max_s.setRange(50, 100); self.pos_y_max_s.setValue(75)
        self.pos_y_max_lbl = QLabel("75%")
        self.pos_y_max_s.valueChanged.connect(lambda v: (setattr(self.lyric_window, 'pos_y_max', v), self.pos_y_max_lbl.setText(f"{v}%")))
        pos_y_layout.addWidget(self.pos_y_max_s); pos_y_layout.addWidget(self.pos_y_max_lbl)
        pos_layout2.addLayout(pos_y_layout)
        layout.addWidget(pos_box)

        # ============ 预设与模式 ============
        preset_box = QGroupBox("预设与模式")
        preset_layout = QVBoxLayout(preset_box); preset_layout.setSpacing(6)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("预设："))
        self.preset_combo = QComboBox(); self.preset_combo.setMinimumWidth(120)
        self.refresh_preset_list(); self.preset_combo.currentTextChanged.connect(self.load_preset)
        top_row.addWidget(self.preset_combo)
        btn_new = QPushButton("+"); btn_new.setMaximumWidth(34); btn_new.setToolTip("新建预设")
        btn_new.clicked.connect(self.new_preset)
        top_row.addWidget(btn_new)
        btn_del = QPushButton("-"); btn_del.setMaximumWidth(34); btn_del.setToolTip("删除预设")
        btn_del.clicked.connect(self.delete_preset)
        top_row.addWidget(btn_del)
        top_row.addStretch()
        preset_layout.addLayout(top_row)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("模式："))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("中文", "chinese"); self.mode_combo.addItem("英文", "english")
        self.mode_combo.setMaximumWidth(120); mode_row.addWidget(self.mode_combo)
        mode_row.addStretch()
        preset_layout.addLayout(mode_row)
        layout.addWidget(preset_box)

        # ============ 播放控制 ============
        ctrl_box = QGroupBox("播放控制")
        ctrl_layout = QVBoxLayout(ctrl_box); ctrl_layout.setSpacing(8)
        bl = QHBoxLayout(); bl.setSpacing(10)
        self.start_btn = QPushButton("▶ 开始"); self.start_btn.clicked.connect(self.start)
        self.start_btn.setObjectName("startBtn")
        bl.addWidget(self.start_btn)
        self.stop_btn = QPushButton("■ 停止"); self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setObjectName("stopBtn")
        bl.addWidget(self.stop_btn)
        self.debug_btn = QPushButton("Debug"); self.debug_btn.clicked.connect(self.debug_info)
        self.debug_btn.setObjectName("debugBtn")
        bl.addWidget(self.debug_btn)
        bl.addStretch()
        ctrl_layout.addLayout(bl)
        self.status = QLabel("状态：就绪"); self.status.setAlignment(Qt.AlignCenter)
        self.status.setObjectName("statusLabel")
        ctrl_layout.addWidget(self.status)
        layout.addWidget(ctrl_box)

        license_lbl = QLabel("MIT License"); license_lbl.setObjectName("licenseLabel")
        license_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(license_lbl)

        scroll.setWidget(content); outer_layout.addWidget(scroll)

        screen = QApplication.primaryScreen().geometry()
        screen_h = screen.height()
        self.setFixedSize(560, 720) if screen_h <= 1080 else self.setFixedSize(580, 920)

        

        # 加载设置
        if settings:
            try:
                self.current_color = QColor(settings.get('text_color', '#fffeef'))
                self.current_stroke_color = QColor(settings.get('stroke_color', '#d8a523'))
                self.current_glow_color = QColor(settings.get('glow_color', '#d8a523'))
                self.glow_check.setChecked(settings.get('glow_enabled', True))
                self.glow_size_slider.setValue(settings.get('glow_size', 4))
                self.glow_alpha_slider.setValue(settings.get('glow_alpha', 82))
                self.loop_check.setChecked(settings.get('loop', True))
                self.perspective_check.setChecked(settings.get('perspective_enabled', True))
                self.persp_x_slider.setValue(settings.get('persp_x_strength', 5))
                self.persp_y_slider.setValue(settings.get('persp_y_strength', 30))
                self.persp_comp_slider.setValue(settings.get('persp_compensation', 3))
                self.trans_check.setChecked(settings.get('trans_only', False))
                idx = self.mode_combo.findData(settings.get('mode', 'chinese'))
                if idx >= 0: self.mode_combo.setCurrentIndex(idx)
                self.font_combo.setCurrentFont(QFont(settings.get('font_family', 'Microsoft YaHei')))
                self.font_size.setValue(settings.get('font_size', 28))
                self.stroke_spin.setValue(settings.get('stroke_width', 0.5))
                self.spacing_spin.setValue(settings.get('spacing', 5.0))
                self.shake_intensity_slider.setValue(settings.get('shake_intensity', 2))
                self.shake_speed_slider.setValue(settings.get('shake_speed', 143))
                self.fade_speed_slider.setValue(settings.get('fade_speed', 12))
                self.rise_speed_slider.setValue(settings.get('rise_speed', 1))
                self.margin_spin.setValue(settings.get('margin_time', 4000))
                self.max_interval_spin.setValue(settings.get('max_interval', 16000))
                self.max_duration_spin.setValue(settings.get('max_duration', 5000))
                self.angle_min.setValue(settings.get('angle_min', -10))
                self.angle_max.setValue(settings.get('angle_max', 10))
                self.pos_x_min_s.setValue(settings.get('pos_x_min', 5))
                self.pos_x_max_s.setValue(settings.get('pos_x_max', 85))
                self.pos_y_min_s.setValue(settings.get('pos_y_min', 5))
                self.pos_y_max_s.setValue(settings.get('pos_y_max', 75))
                player_name = settings.get('player', '网易云音乐')
                idx = self.player_combo.findText(player_name)
                if idx >= 0: self.player_combo.setCurrentIndex(idx)
                source_name = settings.get('source', '网易云')
                idx = self.source_combo.findText(source_name)
                if idx >= 0: self.source_combo.setCurrentIndex(idx)
                delay_idx = settings.get('delay', 0)
                self.delay_combo.setCurrentIndex(delay_idx)
                self.color_btn.setStyleSheet(f"background-color:{self.current_color.name()};border-radius:6px;border:1px solid #4a5264;")
                self.stroke_btn.setStyleSheet(f"background-color:{self.current_stroke_color.name()};border-radius:6px;border:1px solid #4a5264;")
                self.glow_color_btn.setStyleSheet(f"background-color:{self.current_glow_color.name()};border-radius:6px;border:1px solid #4a5264;")
                self.auto_switch_check.setChecked(settings.get('auto_switch', False))
                self.smtc_sync_check.setChecked(settings.get('smtc_sync', False))
            except: pass
        

    def refresh_player_list(self):
        self.player_combo.blockSignals(True); self.player_combo.clear()
        self.player_combo.addItems(list(self.players.keys()))
        if SMTC_AVAILABLE:
            self.player_combo.addItem(SMTC_PLAYER_NAME)
        self.player_combo.blockSignals(False)

    def add_custom_player(self):
        name, ok = QInputDialog.getText(self, "自定义播放器", "输入播放器名称(成功添加播放器后，将以该名称显示在列表内)：")
        if ok and name.strip():
            name = name.strip()
            if name in self.players: QMessageBox.warning(self, "重复", "该播放器已存在！"); return
            proc, ok2 = QInputDialog.getText(self, "进程名", "输入进程名(打开任务管理器查看，快捷键：Shift+Ctrl+Esc)：")
            if ok2 and proc.strip():
                # 选择标题格式
                format_choice, ok3 = QInputDialog.getItem(
                    self, "窗口标题格式", "选择窗口标题格式(将鼠标保持在任务栏内的播放器程序上端，观察窗口名\n如显示“SAIKAI - Mili”，则选第一个)：",
                    ["歌名 - 歌手", "歌手 - 歌名", "自定义正则"], 0, False)
                if ok3:
                    if format_choice == "自定义正则":
                        pattern, ok4 = QInputDialog.getText(
                            self, "标题正则", "输入标题匹配正则：", text=r'^(.+?)\s*-\s*(.+)$')
                        if not ok4 or not pattern.strip():
                            return
                        pattern = pattern.strip()
                        swap = False
                    elif format_choice == "歌手 - 歌名":
                        pattern = r'^(.+?)\s*-\s*(.+)$'
                        swap = True
                    else:
                        pattern = r'^(.+?)\s*-\s*(.+)$'
                        swap = False
                    self.players[name] = {
                        "process": proc.strip(),
                        "pattern": pattern,
                        "swap": swap
                    }
                    self.refresh_player_list()
                    self.player_combo.setCurrentText(name)

    def delete_player(self):
        name = self.player_combo.currentText()
        if not name: return
        if len(self.players) <= 1: QMessageBox.warning(self, "不能删除", "至少保留一个播放器！"); return
        if QMessageBox.question(self, "删除播放器", f"确定删除「{name}」吗？") == QMessageBox.Yes:
            del self.players[name]; self.refresh_player_list()
            self.status.setText(f"状态：已删除播放器 {name}")

    def apply_zoom(self):
        scale = self.zoom_slider.value() / 100.0
        screen = QApplication.primaryScreen().geometry()
        base_w = 560 if screen.height() <= 1080 else 580
        base_h = 720 if screen.height() <= 1080 else 920
        self.setFixedSize(int(base_w * scale), int(base_h * scale))

    def fetch_lyric(self): LyricFetcher.fetch_and_set(self)

    def refresh_preset_list(self):
        self.preset_combo.blockSignals(True); self.preset_combo.clear()
        self.preset_combo.addItems(list(self.presets.keys()))
        self.preset_combo.blockSignals(False)

    def new_preset(self):
        name, ok = QInputDialog.getText(self, "新建预设", "输入预设名称：")
        if ok and name.strip():
            name = name.strip()
            if name in self.presets: QMessageBox.warning(self, "重复", "该预设名称已存在！"); return
            self.presets[name] = {'text': self.current_color.name(), 'stroke': self.current_stroke_color.name(), 'glow': self.current_glow_color.name()}
            self.refresh_preset_list(); self.preset_combo.setCurrentText(name)
            self.status.setText(f"状态：已创建预设 {name}")

    def delete_preset(self):
        name = self.preset_combo.currentText()
        if not name: return
        if len(self.presets) <= 1: QMessageBox.warning(self, "不能删除", "至少保留一个预设！"); return
        if QMessageBox.question(self, "删除预设", f"确定删除「{name}」吗？") == QMessageBox.Yes:
            del self.presets[name]; self.refresh_preset_list()
            self.status.setText(f"状态：已删除预设 {name}")

    def pick_glow_color(self):
        c = QColorDialog.getColor(self.current_glow_color, self, "发光颜色")
        if c.isValid(): self.current_glow_color = c; self.glow_color_btn.setStyleSheet(f"background-color:{c.name()};border-radius:6px;border:1px solid #4a5264;")

    def load_preset(self, name):
        if name in self.presets:
            c = self.presets[name]
            self.current_color = QColor(c['text']); self.current_stroke_color = QColor(c['stroke'])
            self.current_glow_color = QColor(c.get('glow', '#ffffff'))
            self.color_btn.setStyleSheet(f"background-color:{self.current_color.name()};")
            self.stroke_btn.setStyleSheet(f"background-color:{self.current_stroke_color.name()};")
            self.glow_color_btn.setStyleSheet(f"background-color:{self.current_glow_color.name()};")
            self.status.setText(f"状态：已加载 {name}")
    def auto_select_font(self):
        from PyQt5.QtGui import QFontDatabase
        recommended = ["Mikodacs", "思源黑体 Bold"]
        available = [f for f in recommended if f in QFontDatabase().families()]
        if not available:
            self.status.setText("状态：未找到推荐字体")
            return
        current = self.font_combo.currentFont().family()
        # 找当前字体在列表里的位置，选下一个
        try:
            idx = available.index(current)
            next_idx = (idx + 1) % len(available)
        except ValueError:
            next_idx = 0
        chosen = available[next_idx]
        self.font_combo.setCurrentFont(QFont(chosen))
        self.status.setText(f"状态：已切换字体 {chosen}")
    def pick_color(self):
        c = QColorDialog.getColor(self.current_color, self, "文字颜色")
        if c.isValid(): self.current_color = c; self.color_btn.setStyleSheet(f"background-color:{c.name()};border-radius:6px;border:1px solid #4a5264;")

    def pick_stroke(self):
        c = QColorDialog.getColor(self.current_stroke_color, self, "阴影/描边颜色")
        if c.isValid(): self.current_stroke_color = c; self.stroke_btn.setStyleSheet(f"background-color:{c.name()};border-radius:6px;border:1px solid #4a5264;")

    def start(self, ignore_delay=False):
        text = self.text_input.toPlainText().strip()
        if not text: self.status.setText("状态：请先输入歌词！"); return
        font = QFont(self.font_combo.currentFont().family(), self.font_size.value(), QFont.Bold)
        mode = self.mode_combo.currentData()
        self.lyric_window.loop = self.loop_check.isChecked()
        delay = 0 if ignore_delay else int(self.delay_combo.currentText().replace('s', ''))
        self.lyric_window.start_lyric(
            text, font, self.current_color, self.current_stroke_color,
            self.stroke_spin.value(), self.angle_min.value(), self.angle_max.value(),
            self.margin_spin.value(), self.max_interval_spin.value(), self.max_duration_spin.value(),
            mode, self.spacing_spin.value(), self.shake_intensity_slider.value(),
            self.shake_speed_slider.value(), self.fade_speed_slider.value(),
            self.rise_speed_slider.value(), self.glow_check.isChecked(),
            self.current_glow_color, self.glow_size_slider.value(),
            self.glow_alpha_slider.value(), start_delay=delay
        )
        self.status.setText(f"状态：正在播放... 模式：{mode}")

    def stop(self): self.lyric_window.stop_lyric(); self.status.setText("状态：已停止")
    def closeEvent(self, event):
        if self.smtc_monitor is not None:
            self.smtc_monitor.stop()
            self.smtc_monitor = None
        save_all_config(self, self.presets, self.players)
        self.lyric_window.close(); event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    panel = ControlPanel()
    screen = app.primaryScreen().geometry()
    x = (screen.width() - panel.width()) // 2
    y = max(0, (screen.height() - panel.height()) // 2)
    panel.move(x, y); panel.show()
    panel.raise_(); panel.activateWindow()
    app.exec_()