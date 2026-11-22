# -*- coding: utf-8 -*-
"""HD2 大厅搜索器 V23

游戏外进程用 Steam 的公共大厅列表（ISteamMatchmaking）列出《绝地潜兵2》的公开房间，
点「加入」通过 steam://joinlobby 让正在运行的游戏进房。

不读写游戏进程、不注入、不碰 EAC —— 只和 Steam 客户端说话。
依赖：Windows + Python 3.8+（只用标准库 ctypes / http.server）。
"""
import ctypes as C
import ctypes.wintypes as W
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urljoin

APP_ID = 553850
VERSION = 23
STEAMID_MIN = 76561197960265728
STEAMID_MAX = 76561202255233023

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))     # exe 所在目录：可写文件放这
    RES = getattr(sys, "_MEIPASS", HERE)                        # 打包进去的只读数据
else:
    HERE = RES = os.path.dirname(os.path.abspath(__file__))
PLANETS = []
PLANETS_ZH = {}
FACTIONS = {}
for name, target in (("planets.json", "PLANETS"), ("planets_zh.json", "PLANETS_ZH"),
                     ("factions.json", "FACTIONS")):
    try:
        with open(os.path.join(RES, name), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if target == "PLANETS":
            PLANETS = data
        elif target == "PLANETS_ZH":
            PLANETS_ZH = {int(k): v for k, v in data.items()}
        else:
            FACTIONS = {int(k): v.get("f") for k, v in data.items()}
    except Exception:
        pass
SECTORS_ZH = {}
try:
    with open(os.path.join(RES, "sectors_zh.json"), "r", encoding="utf-8") as fh:
        SECTORS_ZH = json.load(fh)
except Exception:
    SECTORS_ZH = {}

GITHUB_REPO = "QLongYan/HD2LobbyBrowser"
GITHUB_PAGE = "https://github.com/" + GITHUB_REPO
GITHUB_API = "https://api.github.com/repos/" + GITHUB_REPO + "/releases/latest"
GITHUB_DL = GITHUB_PAGE + "/releases/latest/download/HD2LobbyBrowser.exe"
GITHUB_MIRRORS = ("https://ghproxy.net/", "https://gh-proxy.com/")   # 国内直连不通时兜底

# checked: 本次启动是否真的问过服务器；latest: 服务器上的最新版本号；error: 失败原因（空 = 正常）
VER_INFO = {"latest": None, "url": GITHUB_PAGE, "file": "", "checked": False,
            "error": "", "source": ""}
VER_DONE = threading.Event()


def _ver_json(url, timeout=6):
    """读自定义版本 JSON：{"latest":N,"url":"公告页","file":"exe 直链"}。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "HD2LobbyBrowser", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def custom_update_source():
    """update_url.txt 里若配了 JSON 地址，返回它的内容；否则 None。"""
    try:
        with open(os.path.join(HERE, "update_url.txt"), "r", encoding="utf-8") as fh:
            lines = [x.strip() for x in fh.read().splitlines()]
    except Exception:
        return None
    urls = [x for x in lines if x and not x.startswith("#")]
    if not urls:
        return None
    try:
        data = _ver_json(urls[0])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def fetch_github_release():
    """读 GitHub 最新 Release。返回 (最新版本号, exe 直链, 发布页)；没有 Release 时返回 (None, '', 仓库页)。"""
    req = urllib.request.Request(GITHUB_API, headers={
        "User-Agent": "HD2LobbyBrowser", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = str(data.get("tag_name") or "")
    m = re.match(r"[vV]?\s*(\d+)", tag)          # 只认开头的版本号：v21 / 21 / V21.1
    latest = int(m.group(1)) if m else None
    # 附件优先取 zip（整包带数据文件），没有 zip 再退回单个 exe
    asset = ""
    for want in (".zip", ".exe"):
        for item in data.get("assets") or []:
            if str(item.get("name", "")).lower().endswith(want):
                asset = item.get("browser_download_url") or ""
                break
        if asset:
            break
    return latest, asset, data.get("html_url") or GITHUB_PAGE


def check_update():
    """启动时检查最新版：优先 update_url.txt 自定义源，其次 GitHub Release。"""
    custom = custom_update_source()
    if custom:
        try:
            latest = int(custom.get("latest"))
        except Exception:
            latest = None
        VER_INFO.update({"latest": latest, "checked": True, "error": "",
                         "url": custom.get("url") or GITHUB_PAGE,
                         "file": custom.get("file") or "", "source": "custom"})
        VER_DONE.set()
        return
    try:
        latest, asset, page = fetch_github_release()
        VER_INFO.update({"latest": latest, "url": page, "file": asset,
                         "checked": True, "error": "", "source": "github"})
    except urllib.error.HTTPError as exc:
        # 404 = 仓库还没有 Release：当作"已是最新"，不吓用户
        VER_INFO.update({"latest": None, "checked": True,
                         "error": "" if exc.code == 404 else "HTTP %s" % exc.code,
                         "source": "github"})
    except Exception as exc:
        VER_INFO.update({"latest": None, "checked": True, "error": str(exc)[:120],
                         "source": "github"})
    finally:
        VER_DONE.set()


MISSION_HASH = {}
try:
    with open(os.path.join(RES, "mission_names.json"), "r", encoding="utf-8") as fh:
        MISSION_HASH = {str(k).lower(): v for k, v in json.load(fh).items()}
except Exception:
    MISSION_HASH = {}

# mission_hash -> 英文任务名（界面切到 EN 时用）
MISSION_HASH_EN = {}
try:
    with open(os.path.join(RES, "mission_names_en.json"), "r", encoding="utf-8") as fh:
        MISSION_HASH_EN = {str(k).lower(): v for k, v in json.load(fh).items()}
except Exception:
    MISSION_HASH_EN = {}

PLANET_NAME = {p["i"]: p["n"] for p in PLANETS}
PLANET_SECTOR = {p["i"]: p["s"] for p in PLANETS}

FACTION_SHORT = {"Terminids": "虫族", "Automatons": "机器人", "Illuminate": "光能族"}
FACTION_SHORT_EN = {"Terminids": "Terminids", "Automatons": "Automatons", "Illuminate": "Illuminate"}
DIFF_CN = {
    1: "1 轻松", 2: "2 简单", 3: "3 中等", 4: "4 有挑战性", 5: "5 困难",
    6: "6 极端", 7: "7 自杀式任务", 8: "8 不可能", 9: "9 绝地潜兵", 10: "10 超级绝地潜兵",
}
DIFF_EN = {
    1: "1 Trivial", 2: "2 Easy", 3: "3 Medium", 4: "4 Challenging", 5: "5 Hard",
    6: "6 Extreme", 7: "7 Suicide Mission", 8: "8 Impossible", 9: "9 Helldive",
    10: "10 Super Helldive",
}
REGION_CN = {"AS": "亚洲", "NA": "北美", "EU": "欧洲", "SA": "南美", "OC": "大洋洲",
             "AF": "非洲", "ME": "中东"}
REGION_EN = {"AS": "Asia", "NA": "N. America", "EU": "Europe", "SA": "S. America",
             "OC": "Oceania", "AF": "Africa", "ME": "Middle East"}

KEY_PLANET = b"12"
KEY_DIFF = b"17"        # 难度 1–10（游戏自己的难度筛选键，旧 mod 也用它）；键 9 不是难度
KEY_REGION = b"21"
KEY_KIND = b"1"
KEY_PRIV = b"19"        # 匹配隐私：0=公开 1=好友 2=私密（实测可作 Steam 过滤器）
KEY_PACKET = b"10"      # 任务数据包（f3fb/f3fc… 十六进制）：含阵营/难度/mission_hash
KIND_STEAM = "f8c9037f96047f7a"   # 跨平台关闭（Steam 直连），此时键 3 是房主 SteamID
PACKET_FACTION = (20, 22)   # 包里字符下标（半开区间）：阵营 2/3/4
PACKET_DIFF = (22, 24)      # 难度 1–10
PACKET_HASH = (52, 60)      # mission_hash，8 位十六进制


def parse_packet(packet):
    """解析大厅键 10 的任务数据包 → (阵营, 难度, mission_hash)。

    非任务包 / 字段非法时对应项返回 None 或 ""，调用方自己兜底。
    """
    if not packet or len(packet) < 64 or not packet.startswith("f3"):
        return None, None, ""
    try:
        fac = int(packet[PACKET_FACTION[0]:PACKET_FACTION[1]], 16)
        dif = int(packet[PACKET_DIFF[0]:PACKET_DIFF[1]], 16)
    except ValueError:
        return None, None, ""
    h = packet[PACKET_HASH[0]:PACKET_HASH[1]].lower()
    if not 2 <= fac <= 4:
        fac = None
    if not 1 <= dif <= 10:
        dif = None
    if len(h) != 8 or any(c not in "0123456789abcdef" for c in h):
        h = ""
    return fac, dif, h


# ---------------------------------------------------------------- 界面文案
I18N = {
    "zh": {
        "title": "HD2 大厅搜索器",
        "state_ready": "准备中…",
        "state_query": "查询中…",
        "state_fail": "失败",
        "state_rooms": "个房间",
        "state_joined": "已进入房间",
        "mine": "你在房间 ",
        "no_mine": "游戏不在房间",
        "planet": "全部星球",
        "diff": "全部难度",
        "diff0": "未选任务",
        "front": "战线",
        "region": "地区",
        "plat": "平台",
        "refresh": "刷新",
        "save_preset": "保存预设",
        "upgrade": "升级",
        "preset_saved": "预设已保存",
        "preset_fail": "预设保存失败",
        "upgrading": "正在下载更新包…",
        "restarting": "升级完成，正在重启…",
        "ok_got_it": "知道了",
        "f_term": "东线 虫族",
        "f_auto": "西线 机器人",
        "f_illu": "南线 光能族",
        "r_as": "亚洲", "r_na": "北美", "r_eu": "欧洲", "r_oc": "大洋洲",
        "r_sa": "南美", "r_me": "中东", "r_af": "非洲",
        "p_private": "私服",
        "p_pc": "PC", "p_ps": "PS", "p_xbox": "Xbox",
        "th_platform": "平台",
        "os_tip": "房主平台（Steam / PlayStation / Xbox）",
        "prog_platform": "查询房主平台 {a} / {b}",
        "c_nosession": "隐藏无会话房",
        "c_hidefull": "隐藏满员",
        "c_hidefail": "隐藏进不去的房",
        "c_onlyname": "只看有房主名的房",
        "c_public": "只看公开房",
        "c_sos": "只看 SOS 房",
        "th_sos": "SOS",
        "newver": "有新版 V%s",
        "up_to_date": "已是最新版",
        "check_failed": "检查更新失败",
        "upgrade_tip": "点击升级到 V%s",
        "mission_btn": "任务",
        "m_search": "搜索任务名…",
        "m_all": "全部任务",
        "m_sel": "已选 {n} 项",
        "m_empty": "没有匹配的任务名",
        "m_select_all": "全选",
        "m_clear": "清空",
        "m_foot": "只列出当前结果里出现过的任务；筛选在本地完成，不影响难度。",
        "mis_hash_tip": "任务哈希 {h}",
        "mis_unknown": "未识别任务",
        "mis_unknown_tip": "任务哈希 {h} 不在本地名称表里（游戏新增任务），等版本更新即可",
        "diff_guess_tip": "无任务数据包，难度取自大厅键 17",
        "pv_public": "公开", "pv_friend": "好友", "pv_private": "私密", "pv_unknown": "—",
        "th_planet": "星球", "th_front": "战线", "th_diff": "难度",
        "th_members": "人数", "th_priv": "隐私", "th_ping": "延迟", "th_mission": "任务", "th_region": "地区",
        "th_host": "房主", "th_age": "建立", "th_action": "",
        "join": "加入",
        "joining": "已发送…",
        "joined": "已进入",
        "you_here": "你在这里",
        "cross": "跨平台",
        "empty": "没有符合条件的房间，放宽筛选或换个星球",
        "loading": "正在查询…",
        "ago_s": " 秒前", "ago_m": " 分钟前", "ago_h": " 小时前",
        "tip_delay": "由于 steam 端和箭头服务器之间有延迟，刷新后请等 4–5 秒再点「加入」，避免进错房间",
        "note": "延迟 = 本机到该房间所在区域 Steam 中继的实测值（不是到房主）。房主昵称只有私服房能取到（约 3%）。",
        "note_scan": "按勾选条件扫了 {n} 个星球、抓到 {got} 个房间，符合条件 {kept} 个。",
        "no_planet": "未选星球",
        "err_join": "游戏没有响应（12 秒），这个房间已标记为「进不去的房」，下次刷新会隐藏。常见原因：房主把「匹配隐私」设成好友、房间已关闭、或你在任务中。换一个房间再试。",
        "lang_btn": "EN",
        "quit": "退出",
        "prog_start": "开始扫描…",
        "prog_list": "正在扫描星球 {a} / {b}",
        "prog_details": "读取房间详情…",
        "prog_names": "获取房主昵称…",
    },
    "en": {
        "title": "HD2 Lobby Browser",
        "state_ready": "Starting…",
        "state_query": "Searching…",
        "state_fail": "Failed",
        "state_rooms": "lobbies",
        "state_joined": "Joined lobby",
        "mine": "You are in ",
        "no_mine": "Not in a lobby",
        "planet": "All planets",
        "diff": "All difficulties",
        "diff0": "No mission",
        "front": "Front",
        "region": "Region",
        "plat": "Platform",
        "refresh": "Refresh",
        "save_preset": "Save preset",
        "upgrade": "Update",
        "preset_saved": "Preset saved",
        "preset_fail": "Could not save preset",
        "upgrading": "Downloading update…",
        "restarting": "Updated, restarting…",
        "ok_got_it": "OK",
        "f_term": "East · Terminids",
        "f_auto": "West · Automatons",
        "f_illu": "South · Illuminate",
        "r_as": "Asia", "r_na": "N. America", "r_eu": "Europe", "r_oc": "Oceania",
        "r_sa": "S. America", "r_me": "Middle East", "r_af": "Africa",
        "p_private": "Private",
        "p_pc": "PC", "p_ps": "PS", "p_xbox": "Xbox",
        "th_platform": "Platform",
        "os_tip": "Host platform (Steam / PlayStation / Xbox)",
        "prog_platform": "Host platform {a} / {b}",
        "c_nosession": "Hide no-session",
        "c_hidefull": "Hide full",
        "c_hidefail": "Hide unjoinable",
        "c_onlyname": "Named host only",
        "c_public": "Public only",
        "c_sos": "SOS only",
        "th_sos": "SOS",
        "newver": "V%s available",
        "up_to_date": "Up to date",
        "check_failed": "Update check failed",
        "upgrade_tip": "Update to V%s",
        "mission_btn": "Mission",
        "m_search": "Search missions…",
        "m_all": "All missions",
        "m_sel": "{n} selected",
        "m_empty": "No matching mission",
        "m_select_all": "Select all",
        "m_clear": "Clear",
        "m_foot": "Lists only missions found in the current results; filtering is local and does not change difficulty.",
        "mis_hash_tip": "Mission hash {h}",
        "mis_unknown": "Unknown mission",
        "mis_unknown_tip": "Mission hash {h} is not in the local name table (new mission), update the app",
        "diff_guess_tip": "No mission packet — difficulty taken from lobby key 17",
        "pv_public": "Public", "pv_friend": "Friends", "pv_private": "Private", "pv_unknown": "—",
        "th_planet": "Planet", "th_front": "Front", "th_diff": "Difficulty",
        "th_members": "Players", "th_priv": "Privacy", "th_ping": "Ping", "th_mission": "Mission", "th_region": "Region",
        "th_host": "Host", "th_age": "Age", "th_action": "",
        "join": "Join",
        "joining": "Sent…",
        "joined": "Joined",
        "you_here": "You are here",
        "cross": "Crossplay",
        "empty": "No lobby matches — relax the filters or pick a planet",
        "loading": "Searching…",
        "ago_s": "s ago", "ago_m": "m ago", "ago_h": "h ago",
        "tip_delay": "Steam and Arrowhead servers sync with a delay — wait 4–5 seconds after refreshing before clicking Join.",
        "note": "Ping = measured from this PC to the Steam relay of that lobby's region (not to the host). Host names are only visible for Steam-direct lobbies (crossplay off).",
        "note_scan": "Scanned {n} planet(s) per your filters, {got} lobbies found, {kept} matched.",
        "no_planet": "No planet",
        "err_join": "The game did not respond in 12s. This lobby is now marked unjoinable and will be hidden. Common causes: host set Matchmaking Privacy to Friends, lobby closed, or you are in a mission.",
        "lang_btn": "中",
        "quit": "Quit",
        "prog_start": "Starting…",
        "prog_list": "Scanning planets {a} / {b}",
        "prog_details": "Reading lobby details…",
        "prog_names": "Fetching host names…",
    },
}


def system_lang():
    """系统 UI 语言是中文就返回 zh，否则 en。"""
    try:
        lang = C.windll.kernel32.GetUserDefaultUILanguage()
        if (lang & 0x3FF) == 0x04:      # LANG_CHINESE
            return "zh"
    except Exception:
        pass
    try:
        import locale
        loc = (locale.getdefaultlocale()[0] or "").lower()
        if loc.startswith("zh"):
            return "zh"
    except Exception:
        pass
    return "en"


DEFAULT_LANG = (os.environ.get("HD2_LANG") or system_lang()).lower()[:2]
if DEFAULT_LANG not in ("zh", "en"):
    DEFAULT_LANG = system_lang()


# ---------------------------------------------------------------- ICMP 延迟
class Icmp:
    """IcmpSendEcho：不需要管理员权限，返回毫秒或 None。"""

    def __init__(self):
        self.ok = False
        try:
            self.api = C.WinDLL("iphlpapi")
            self.api.IcmpCreateFile.restype = C.c_void_p
            self.api.IcmpSendEcho.argtypes = [C.c_void_p, W.ULONG, C.c_char_p, W.USHORT,
                                              C.c_void_p, C.c_void_p, W.DWORD, W.DWORD]
            self.api.IcmpSendEcho.restype = W.DWORD
            self.api.IcmpCloseHandle.argtypes = [C.c_void_p]
            self.handle = self.api.IcmpCreateFile()
            self.ok = bool(self.handle)
        except Exception:
            self.ok = False

    def ping(self, ip, timeout_ms=900):
        if not self.ok:
            return None
        try:
            addr = struct.unpack("<I", bytes(int(x) for x in ip.split(".")))[0]
        except Exception:
            return None
        payload = b"hd2lobbybrowser1"
        buf = C.create_string_buffer(64)
        n = self.api.IcmpSendEcho(self.handle, addr, payload, len(payload),
                                  None, buf, 64, timeout_ms)
        if n == 0:
            return None
        status, rtt = struct.unpack_from("<II", buf.raw, 4)
        return rtt if status == 0 else None


class Latency:
    """按区域测到 Steam 中继的真实网络延迟（该区域最优节点），缓存 5 分钟。"""

    TTL = 300

    def __init__(self):
        self.icmp = Icmp()
        self.pops = {}
        try:
            with open(os.path.join(RES, "sdr_pops.json"), "r", encoding="utf-8") as fh:
                self.pops = json.load(fh)
        except Exception:
            self.pops = {}
        self.cache = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8)

    def _probe_one(self, ip):
        vals = [v for v in (self.icmp.ping(ip) for _ in range(2)) if v is not None]
        return min(vals) if vals else None

    def region_ping(self, region):
        if not region or region not in self.pops:
            return None
        now = time.time()
        hit = self.cache.get(region)
        if hit and now - hit[0] < self.TTL:
            return hit[1]
        nodes = self.pops.get(region) or []
        if not nodes:
            return None
        vals = list(self.pool.map(self._probe_one, [n["ip"] for n in nodes]))
        vals = [v for v in vals if v is not None]
        ms = min(vals) if vals else None
        with self.lock:
            self.cache[region] = (now, ms)
        return ms

    def warm(self):
        def run():
            for reg in ("AS", "NA", "EU", "SA", "OC"):
                if reg in self.pops:
                    self.region_ping(reg)
        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- 星球在线人数
class PlanetStats:
    URL = "https://api.helldivers2.dev/api/v1/planets"
    TTL = 180

    def __init__(self):
        self.counts = {}
        self.facs = {}
        self.owners = {}
        self.at = 0
        self.lock = threading.Lock()
        self.fetching = False

    def get(self, block=False):
        with self.lock:
            if self.counts and time.time() - self.at < self.TTL:
                return self.counts
            if self.fetching and not block:
                return self.counts
            self.fetching = True
        try:
            req = urllib.request.Request(self.URL, headers={
                "X-Super-Client": "hd2-lobby-browser", "X-Super-Contact": "local"})
            data = json.load(urllib.request.urlopen(req, timeout=8))
            counts = {}
            facs = {}
            owners = {}
            for p in data:
                st = p.get("statistics") or {}
                counts[p["index"]] = st.get("playerCount") or 0
                owner = p.get("currentOwner") or "Humans"
                owners[p["index"]] = owner
                if owner in ("Terminid", "Terminids"):
                    facs[p["index"]] = "Terminids"
                elif owner in ("Automaton", "Automatons"):
                    facs[p["index"]] = "Automatons"
                elif owner == "Illuminate":
                    facs[p["index"]] = "Illuminate"
                else:
                    kills = {"Terminids": st.get("terminidKills") or 0,
                             "Automatons": st.get("automatonKills") or 0,
                             "Illuminate": st.get("illuminateKills") or 0}
                    best = max(kills, key=kills.get)
                    facs[p["index"]] = best if kills[best] > 0 else None
            facs[0] = None          # 超级地球：人类母星，没有战线
            with self.lock:
                self.counts, self.facs, self.owners, self.at = counts, facs, owners, time.time()
        except Exception:
            pass
        finally:
            with self.lock:
                self.fetching = False
        return self.counts

    def factions(self):
        with self.lock:
            return dict(self.facs)

    def owner_map(self):
        with self.lock:
            return dict(self.owners), self.at

    def warm(self):
        threading.Thread(target=self.get, daemon=True).start()


# ---------------------------------------------------------------- Steam 路径
def find_steam_api():
    override = os.environ.get("HD2_STEAM_API")
    if override and os.path.isfile(override):
        return override
    cands = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
    except Exception:
        steam = None
    libs = []
    if steam:
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        try:
            text = open(vdf, "r", encoding="utf-8", errors="replace").read()
            import re
            libs = [m.replace("\\\\", "\\") for m in re.findall(r'"path"\s*"([^"]+)"', text)]
        except Exception:
            pass
        libs.insert(0, steam)
    for root in libs:
        cands.append(os.path.join(root, "steamapps", "common", "Helldivers 2", "bin", "steam_api64.dll"))
    for drive in "CDEFGH":
        cands.append(r"%s:\Steam\steamapps\common\Helldivers 2\bin\steam_api64.dll" % drive)
        cands.append(r"%s:\SteamLibrary\steamapps\common\Helldivers 2\bin\steam_api64.dll" % drive)
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


# ---------------------------------------------------------------- Steam 大厅
class SteamLobbies:
    MAX_QUERIES = 12
    PER_FRONT = 2          # 每条战线扫几个活跃星球
    PER_FRONT_DIFF = 3     # 指定难度时多扫一个（过滤后每星球命中少）
    MAX_ROOMS = 400        # 安全上限

    def __init__(self):
        self.lock = threading.Lock()
        self.ok = False
        self.err = ""
        self.self_id = 0
        self.mm = None
        self.fr = None
        self.us = None
        self.ut = None
        self.dll_path = ""
        self.names = {}
        self.rooms = {}
        self._init()

    def _init(self):
        dll = find_steam_api()
        if not dll:
            self.err = "找不到 steam_api64.dll：请确认已安装《绝地潜兵2》，或用环境变量 HD2_STEAM_API 指定路径。"
            return
        self.dll_path = dll
        os.environ["SteamAppId"] = str(APP_ID)
        os.environ["SteamGameId"] = str(APP_ID)
        try:
            os.add_dll_directory(os.path.dirname(dll))
        except Exception:
            pass
        try:
            s = C.CDLL(dll)
        except Exception as exc:
            self.err = "加载 steam_api64.dll 失败：%s" % exc
            return
        self.s = s
        s.SteamAPI_InitFlat.restype = C.c_int
        s.SteamAPI_InitFlat.argtypes = [C.c_char_p]
        s.SteamAPI_Shutdown.restype = None
        s.SteamAPI_RunCallbacks.restype = None
        s.SteamAPI_SteamMatchmaking_v009.restype = C.c_void_p
        s.SteamAPI_SteamFriends_v018.restype = C.c_void_p
        s.SteamAPI_SteamUser_v023.restype = C.c_void_p
        s.SteamAPI_SteamUtils_v010.restype = C.c_void_p
        for name, at in (
            ("SteamAPI_ISteamMatchmaking_AddRequestLobbyListDistanceFilter", [C.c_void_p, C.c_int]),
            ("SteamAPI_ISteamMatchmaking_AddRequestLobbyListResultCountFilter", [C.c_void_p, C.c_int]),
            ("SteamAPI_ISteamMatchmaking_AddRequestLobbyListNumericalFilter",
             [C.c_void_p, C.c_char_p, C.c_int, C.c_int]),
        ):
            f = getattr(s, name)
            f.restype = None
            f.argtypes = at
        for name, rt, at in (
            ("SteamAPI_ISteamMatchmaking_RequestLobbyList", C.c_uint64, [C.c_void_p]),
            ("SteamAPI_ISteamMatchmaking_GetLobbyByIndex", C.c_uint64, [C.c_void_p, C.c_int]),
            ("SteamAPI_ISteamMatchmaking_GetNumLobbyMembers", C.c_int, [C.c_void_p, C.c_uint64]),
            ("SteamAPI_ISteamMatchmaking_GetLobbyMemberLimit", C.c_int, [C.c_void_p, C.c_uint64]),
            ("SteamAPI_ISteamMatchmaking_RequestLobbyData", C.c_bool, [C.c_void_p, C.c_uint64]),
            ("SteamAPI_ISteamMatchmaking_GetLobbyData", C.c_char_p, [C.c_void_p, C.c_uint64, C.c_char_p]),
            ("SteamAPI_ISteamUser_GetSteamID", C.c_uint64, [C.c_void_p]),
            ("SteamAPI_ISteamFriends_GetFriendPersonaName", C.c_char_p, [C.c_void_p, C.c_uint64]),
            ("SteamAPI_ISteamFriends_RequestUserInformation", C.c_bool, [C.c_void_p, C.c_uint64, C.c_bool]),
            ("SteamAPI_ISteamFriends_GetFriendRichPresence", C.c_char_p,
             [C.c_void_p, C.c_uint64, C.c_char_p]),
            ("SteamAPI_ISteamUtils_IsAPICallCompleted", C.c_bool,
             [C.c_void_p, C.c_uint64, C.POINTER(C.c_bool)]),
            ("SteamAPI_ISteamUtils_GetAPICallResult", C.c_bool,
             [C.c_void_p, C.c_uint64, C.c_void_p, C.c_int, C.c_int, C.POINTER(C.c_bool)]),
            ("SteamAPI_ISteamUser_GetAuthSessionTicket", C.c_uint32,
             [C.c_void_p, C.c_void_p, C.c_int, C.POINTER(C.c_uint32), C.c_void_p]),
        ):
            f = getattr(s, name)
            f.restype = rt
            f.argtypes = at
        err = C.create_string_buffer(1024)
        rc = s.SteamAPI_InitFlat(err)
        if rc != 0:
            msg = err.value.decode("utf-8", "replace") or "rc=%d" % rc
            self.err = ("Steam 初始化失败（%s）。请确认 Steam 客户端已启动并已登录。" % msg)
            return
        self.mm = s.SteamAPI_SteamMatchmaking_v009()
        self.fr = s.SteamAPI_SteamFriends_v018()
        self.us = s.SteamAPI_SteamUser_v023()
        self.ut = s.SteamAPI_SteamUtils_v010()
        self.self_id = s.SteamAPI_ISteamUser_GetSteamID(self.us)
        self.ok = True

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.s.SteamAPI_RunCallbacks()
            time.sleep(0.05)

    def _data(self, lid, key):
        raw = self.s.SteamAPI_ISteamMatchmaking_GetLobbyData(self.mm, lid, key)
        return raw.decode("utf-8", "replace") if raw else ""

    def persona(self, steamid):
        key = str(steamid)
        if key in self.names:
            return self.names[key]
        try:
            raw = self.s.SteamAPI_ISteamFriends_GetFriendPersonaName(self.fr, int(steamid))
            name = raw.decode("utf-8", "replace") if raw else ""
        except Exception:
            name = ""
        if name and name != "[unknown]":
            self.names[key] = name
            return name
        return ""

    def fetch_names(self, steamids, wait=1.6):
        pending = [i for i in steamids if str(i) not in self.names]
        if not pending:
            return
        for sid in pending:
            try:
                self.s.SteamAPI_ISteamFriends_RequestUserInformation(self.fr, int(sid), False)
            except Exception:
                pass
        self._pump(wait)
        for sid in pending:
            self.persona(sid)

    def _query(self, planet=None, diff=None, public_only=True):
        s, mm = self.s, self.mm
        s.SteamAPI_ISteamMatchmaking_AddRequestLobbyListDistanceFilter(mm, 3)
        s.SteamAPI_ISteamMatchmaking_AddRequestLobbyListResultCountFilter(mm, 100)
        if planet is not None:
            s.SteamAPI_ISteamMatchmaking_AddRequestLobbyListNumericalFilter(mm, KEY_PLANET, int(planet), 0)
        if diff is not None:
            s.SteamAPI_ISteamMatchmaking_AddRequestLobbyListNumericalFilter(mm, KEY_DIFF, int(diff), 0)
        if public_only:
            s.SteamAPI_ISteamMatchmaking_AddRequestLobbyListNumericalFilter(mm, KEY_PRIV, 0, 0)
        call = s.SteamAPI_ISteamMatchmaking_RequestLobbyList(mm)
        count = None
        if call:
            deadline = time.time() + 4.0
            completed = False
            while time.time() < deadline:
                s.SteamAPI_RunCallbacks()
                time.sleep(0.05)
                failed = C.c_bool(False)
                if s.SteamAPI_ISteamUtils_IsAPICallCompleted(self.ut, call, C.byref(failed)):
                    completed = True
                    break
            if completed:
                buf = C.create_string_buffer(8)
                failed = C.c_bool(False)
                if s.SteamAPI_ISteamUtils_GetAPICallResult(self.ut, call, buf, 4, 510, C.byref(failed)) \
                        and not failed.value:
                    count = struct.unpack("<I", buf.raw[:4])[0]
        else:
            self._pump(1.2)
        ids = []
        for i in range(120):
            lid = s.SteamAPI_ISteamMatchmaking_GetLobbyByIndex(mm, i)
            if not lid:
                break
            ids.append(lid)
            if count is not None and len(ids) >= count:
                break
        return ids

    @staticmethod
    def _match(room, fronts, regions, plats, hide_nosession, hide_full):
        if hide_nosession and not room["session"]:
            return False
        if hide_full and room["members"] >= (room["limit"] or 4):
            return False
        if fronts and room["faction"] not in fronts:
            return False
        if regions and room["region"] not in regions:
            return False
        if plats and room["kind"] not in plats:
            return False
        return True

    def pick_planets(self, fronts, diff):
        """按战线平均分配搜索预算：每条勾选的线各取在线人数最高的 N 个星球。"""
        counts = STATS.get(block=True) if STATS else {}
        live = STATS.factions() if STATS else {}
        per = self.PER_FRONT_DIFF if diff is not None else self.PER_FRONT
        lines = fronts or ["Terminids", "Automatons", "Illuminate"]
        out = []
        for f in lines:
            cand = [p["i"] for p in PLANETS
                    if (live.get(p["i"]) or FACTIONS.get(p["i"])) == f and counts.get(p["i"], 0) > 0]
            cand.sort(key=lambda i: -counts.get(i, 0))
            out.extend(cand[:per])
        return out[:self.MAX_QUERIES]

    def search(self, planets=None, fronts=None, regions=None, plats=None, diff=None,
               hide_nosession=True, hide_full=False, target=None, public_only=True,
               platforms=None):
        """按战线平均分配、固定扫多个星球（Steam 每次最多 50 条），最后按条件筛。"""
        if not self.ok:
            return {"ok": False, "error": self.err or "Steam 未就绪"}
        if not self.lock.acquire(timeout=60):
            return {"ok": False, "error": "上一次查询还没结束"}
        try:
            if planets:
                targets = [int(p) for p in planets][:self.MAX_QUERIES]
            else:
                targets = self.pick_planets(fronts, diff)
            if not targets:
                targets = [None]
            PROGRESS.update({"running": True, "scanned": 0, "total": len(targets),
                             "phase": "list", "started": time.time()})
            seen = set()
            ids = []
            for planet in targets:
                for lid in self._query(planet, diff, public_only):
                    if lid in seen:
                        continue
                    seen.add(lid)
                    ids.append(lid)
                PROGRESS["scanned"] += 1
                if len(ids) >= self.MAX_ROOMS:
                    break
            PROGRESS["phase"] = "details"
            if ids:
                for lid in ids:
                    self.s.SteamAPI_ISteamMatchmaking_RequestLobbyData(self.mm, lid)
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    self.s.SteamAPI_RunCallbacks()
                    time.sleep(0.05)
                    if self._data(ids[0], KEY_PLANET) or self._data(ids[0], KEY_KIND):
                        break
            now = int(time.time())
            rooms = [self._room(lid, now) for lid in ids]
            matched = [r for r in rooms
                       if self._match(r, fronts, regions, plats, hide_nosession, hide_full)]
            if diff is not None:                       # 用任务包/键17 复核一次，挡掉公布值过期的房
                matched = [r for r in matched if r["diff"] == int(diff)]
            hosts = [r["host"] for r in matched if r["host"]]
            if hosts:
                PROGRESS["phase"] = "names"
                self.fetch_names(hosts)
                for r in matched:
                    if r["host"]:
                        r["host_name"] = self.persona(r["host"])
            if PLATFORM and matched:                   # 补房主平台 + 跨平台房主名
                PROGRESS.update({"phase": "platform", "scanned": 0, "total": len(matched)})
                PLATFORM.enrich(matched, progress=PROGRESS)
            if platforms:                              # 只保留勾选的平台
                matched = [r for r in matched if r["platform"] in platforms]
            matched.sort(key=lambda r: (-r["members"], r["planet"], r["diff"]))
            self.rooms = {r["lobby"]: r for r in matched}
            return {"ok": True, "rooms": matched, "count": len(matched), "self": str(self.self_id),
                    "queried": PROGRESS["scanned"], "scanned_ids": len(ids),
                    "planets": [p for p in targets if p is not None]}
        except Exception:
            return {"ok": False, "error": traceback.format_exc(limit=3)}
        finally:
            PROGRESS.update({"running": False, "phase": "idle"})
            self.lock.release()

    def _room(self, lid, now):
        s, mm = self.s, self.mm
        d = self._data(lid, KEY_PLANET)
        planet_idx = int(d) if d.lstrip("-").isdigit() else -1
        region = self._data(lid, KEY_REGION)
        kind = self._data(lid, KEY_KIND)
        host = self._data(lid, b"3")
        created = self._data(lid, b"18")
        packet = self._data(lid, KEY_PACKET)
        p_fac, p_diff, mhash = parse_packet(packet)
        sos = self._data(lid, b"8") == "1"
        fac = self._data(lid, b"16")
        live_fac = {"2": "Terminids", "3": "Automatons", "4": "Illuminate"}.get(fac)
        live_fac = {2: "Terminids", 3: "Automatons", 4: "Illuminate"}.get(p_fac) or live_fac
        # 难度：任务包是权威值；键 17 与包内一致，但可能未发布（0）
        d = self._data(lid, KEY_DIFF)
        diff17 = int(d) if d.isdigit() and 1 <= int(d) <= 10 else 0
        diff = p_diff or diff17
        mname = MISSION_HASH.get(mhash, "") if mhash else ""
        priv = self._data(lid, KEY_PRIV)
        privacy = int(priv) if priv.isdigit() else -1
        age = None
        if created.isdigit() and int(created) > 1500000000:
            age = now - int(created)
            if age < 0 or age > 172800:
                age = None
        crossplay = self._data(lid, b"20")
        platform, host_entity = "", ""
        if crossplay == "1":
            is_steam = True
            platform = "steam"                       # 跨平台关 = 只有 Steam，无需查询
        elif crossplay == "0":
            is_steam = False
            if host.isdigit() and len(host) >= 17:   # 键 3 = 房主 PlayFab 实体 id（十进制）
                host_entity = "%016X" % int(host)
        else:
            is_steam = kind == KIND_STEAM and host.isdigit() and STEAMID_MIN <= int(host) <= STEAMID_MAX
            if is_steam:
                platform = "steam"
        return {
            "lobby": str(lid),
            "planet": planet_idx,
            "planet_name": PLANET_NAME.get(planet_idx, ""),
            "planet_name_zh": PLANETS_ZH.get(planet_idx, ""),
            "sector": PLANET_SECTOR.get(planet_idx, ""),
            "sector_zh": SECTORS_ZH.get(PLANET_SECTOR.get(planet_idx, ""), ""),
            "faction": live_fac or FACTIONS.get(planet_idx) or "",
            "diff": diff,
            "diff_guess": bool(diff and not p_diff),      # 没有任务包，只有键 17
            "region": region,
            "kind": "steam" if is_steam else "cross",
            "host": host if is_steam else "",
            "host_name": "",
            "platform": platform,
            "host_entity": host_entity,
            "session": bool(packet),
            "sos": sos,
            "privacy": privacy,
            "level": self._data(lid, b"13"),
            "mission_hash": mhash,
            "mission_name": mname,
            "mission_name_en": MISSION_HASH_EN.get(mhash, ""),
            "ping": LATENCY.region_ping(region) if (LATENCY and region) else None,
            "members": s.SteamAPI_ISteamMatchmaking_GetNumLobbyMembers(mm, lid),
            "limit": s.SteamAPI_ISteamMatchmaking_GetLobbyMemberLimit(mm, lid),
            "age": age,
        }

    def join_url(self, lobby, host=""):
        who = host or str(self.self_id or 0)
        return "steam://joinlobby/%d/%s/%s" % (APP_ID, lobby, who)

    def self_lobby(self):
        if not self.ok:
            return {}
        try:
            raw = self.s.SteamAPI_ISteamFriends_GetFriendRichPresence(self.fr, self.self_id, b"connect")
            connect = raw.decode("utf-8", "replace") if raw else ""
            lobby = ""
            parts = connect.split()
            if len(parts) >= 2 and parts[0] == "+connect_lobby":
                lobby = parts[1]
            return {"lobby": lobby, "connect": connect}
        except Exception:
            return {}

    def shutdown(self):
        if self.ok:
            try:
                self.s.SteamAPI_Shutdown()
            except Exception:
                pass
            self.ok = False


class PlatformIndex:
    """查房主的平台：Steam / PS / Xbox。

    数据来源与旧 mod（Lobby-Browser）完全一致——都是 PlayFab 的公开资料对象
    `testament_backing_platform`（0=PSN / 1=Steam / 4=Xbox）。
    区别只在取数方式：旧 mod 注入游戏偷用游戏自己的会话；这里用本机 Steam 票据
    自己登录一次 PlayFab（标题 974A9，和游戏同一个标题），再按房主的实体 id 读同一个
    公开对象，不读游戏内存、不注入。

    只在用户勾了 PC/PS/Xbox 时才调用，结果按实体 id 缓存 30 分钟。
    """
    TITLE = "974A9"
    BASE = "https://974a9.playfabapi.com"
    NAMES = {0: "psn", 1: "steam", 4: "xbox"}
    TTL_OK = 1800
    TTL_BAD = 300
    WORKERS = 24

    def __init__(self, steam):
        self.steam = steam
        self.lock = threading.Lock()
        self.cache = {}          # entity -> (ts, info|None)
        self.token = ""
        self.token_exp = 0
        self.dead_until = 0
        self.err = ""
        self.calls = 0

    # ---------------- PlayFab ----------------
    def _post(self, path, body, auth=None, timeout=8):
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["X-EntityToken"] = auth
        req = urllib.request.Request(self.BASE + path, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _ticket(self):
        s = self.steam
        buf = C.create_string_buffer(4096)
        size = C.c_uint32(0)
        handle = s.s.SteamAPI_ISteamUser_GetAuthSessionTicket(s.us, buf, 4096, C.byref(size), None)
        if not handle or not size.value:
            raise RuntimeError("取 Steam 票据失败")
        return buf.raw[:size.value].hex().upper()

    def login(self, force=False):
        # 登录必须串行：并发登录会被 PlayFab 拒绝，且线程间共用同一票据
        with self.lock:
            now = time.time()
            if not force and self.token and now < self.token_exp - 60:
                return self.token
            if not force and now < self.dead_until:
                return ""                    # 刚失败过，冷却期内不再重试
            try:
                d = self._post("/Client/LoginWithSteam", {
                    "TitleId": self.TITLE, "SteamTicket": self._ticket(), "CreateAccount": False,
                    "InfoRequestParameters": {"GetUserAccountInfo": False}})
                et = ((d.get("data") or {}).get("EntityToken") or {})
                token = et.get("EntityToken") or ""
                exp = et.get("TokenExpiration") or et.get("Expiration") or ""
                ts = now + 3600
                if isinstance(exp, str) and len(exp) >= 19:
                    try:
                        ts = time.mktime(time.strptime(exp[:19], "%Y-%m-%dT%H:%M:%S")) + time.timezone
                    except Exception:
                        pass
                self.token, self.token_exp, self.err = token, ts, ""
                return token
            except Exception as exc:
                self.token, self.err = "", str(exc)[:160]
                self.dead_until = time.time() + 60
                return ""

    def _profile(self, token, entity):
        return self._post("/Profile/GetProfile", {
            "Entity": {"Id": entity, "Type": "title_player_account"},
            "IncludeStatistics": False, "DataAsObject": True}, auth=token)

    def lookup(self, entity):
        """返回 {"p": steam|psn|xbox, "name": 房主名} 或 None。"""
        if not entity:
            return None
        now = time.time()
        with self.lock:
            hit = self.cache.get(entity)
        if hit and now - hit[0] < (self.TTL_OK if hit[1] else self.TTL_BAD):
            return hit[1]
        token = self.login()
        if not token:
            return None                    # 登录失败属于临时错误，不写缓存
        d, code = None, 0
        try:
            d = self._profile(token, entity)
        except urllib.error.HTTPError as exc:
            code = exc.code
            if exc.code in (401, 403):
                token = self.login(force=True)
                if token:
                    try:
                        d = self._profile(token, entity)
                        code = 0
                    except urllib.error.HTTPError as exc2:
                        code = exc2.code
                    except Exception:
                        code = -1
        except Exception:
            code = -1
        if d is None and code not in (404,):
            return None                    # 网络抖动/限流：不缓存，下次重试
        info = None
        if d:
            objs = (((d.get("data") or {}).get("Profile") or {}).get("Objects") or {})
            plat = (objs.get("testament_backing_platform") or {}).get("DataObject")
            name = (objs.get("testament_platform_user_name") or {}).get("DataObject")
            if isinstance(plat, int) and plat in self.NAMES:
                info = {"p": self.NAMES[plat], "name": name if isinstance(name, str) else ""}
        with self.lock:
            self.cache[entity] = (now, info)
            self.calls += 1
        return info

    def enrich(self, rooms, limit=400, progress=None):
        """给房间补 platform / host_name（只补还没查过的）。返回命中数。"""
        todo = [r for r in rooms if r.get("host_entity") and not r.get("platform")][:limit]
        if not todo:
            return 0

        def one(room):
            info = self.lookup(room["host_entity"])
            if info:
                room["platform"] = info["p"]
                if info.get("name") and not room.get("host_name"):
                    room["host_name"] = info["name"]
            if progress is not None:
                progress["scanned"] = progress.get("scanned", 0) + 1

        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            list(pool.map(one, todo))
        return sum(1 for r in todo if r.get("platform"))


class JoinFailures:
    TTL = 7200
    FILE = "join_fail.json"
    LOG = "join_fail.log"

    def __init__(self):
        try:
            with open(os.path.join(HERE, self.FILE), "r", encoding="utf-8") as fh:
                self.items = json.load(fh)
        except Exception:
            self.items = {}
        self.lock = threading.Lock()

    def mark(self, lobby, room=None, reason=""):
        with self.lock:
            self.items[str(lobby)] = int(time.time())
            try:
                with open(os.path.join(HERE, self.FILE), "w", encoding="utf-8") as fh:
                    json.dump(self.items, fh)
            except Exception:
                pass
            try:
                with open(os.path.join(HERE, self.LOG), "a", encoding="utf-8") as fh:
                    r = room or {}
                    fh.write("%s\tlobby=%s planet=%s diff=%s members=%s/%s region=%s kind=%s "
                             "session=%s privacy=%s host=%s reason=%s\n" % (
                                 time.strftime("%Y-%m-%d %H:%M:%S"), lobby,
                                 r.get("planet"), r.get("diff"), r.get("members"), r.get("limit"),
                                 r.get("region"), r.get("kind"), r.get("session"),
                                 r.get("privacy"), r.get("host"), reason))
            except Exception:
                pass

    def failed(self, lobby):
        t = self.items.get(str(lobby))
        return bool(t and time.time() - t < self.TTL)


STEAM = None
LATENCY = None
STATS = None
FAILS = None
PROGRESS = {"running": False, "scanned": 0, "total": 0, "phase": "idle", "started": 0}
PLATFORM = None       # PlatformIndex，由入口脚本初始化


# ---------------------------------------------------------------- HTTP 服务
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>HD2 大厅搜索器</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#1d2530;color:#f2f6fb;font:14px/1.5 "Microsoft YaHei",system-ui,sans-serif}
header{padding:10px 18px 8px;background:#27313e;border-bottom:1px solid #3a4759;position:sticky;top:0;z-index:5}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.5px}
.tilde{display:flex;align-items:baseline;gap:8px}
.ver{font-size:11px;font-weight:700;color:#8fa0b4;letter-spacing:.5px}
.newver{font-size:11px;font-weight:600;color:#8fa0b4;letter-spacing:.3px;
  text-decoration:none;cursor:default;white-space:nowrap}
.newver.hot{font-weight:800;color:#0a1018;background:linear-gradient(180deg,#ffe27a,#e0a51e);
  padding:1px 7px;border-radius:9px;cursor:pointer;
  box-shadow:0 0 0 1px rgba(255,226,122,.35),0 0 10px rgba(224,165,30,.4)}
.newver.hot:hover{background:linear-gradient(180deg,#fff0a8,#f0b830)}
.newver.bad{color:#93a0b1;opacity:.85}
#upgrade[disabled]{opacity:.42;cursor:not-allowed;border-color:#3a4757;color:#95a1b2}
#upgrade.hot{background:linear-gradient(180deg,#ffe27a,#e0a51e);border-color:#fff0b8;
  color:#1a1206;font-weight:800;box-shadow:0 0 0 1px rgba(255,226,122,.4),0 0 13px rgba(224,165,30,.5);
  animation:upglow 1.9s ease-in-out infinite}
#upgrade.hot:hover{background:linear-gradient(180deg,#fff3b4,#eab52a);border-color:#fff7d6}
@keyframes upglow{
  0%,100%{box-shadow:0 0 0 1px rgba(255,226,122,.32),0 0 9px rgba(224,165,30,.35)}
  50%{box-shadow:0 0 0 2px rgba(255,226,122,.6),0 0 20px rgba(224,165,30,.62)}}
.tag{font-size:12px;color:#aeb9c8}
select,button{background:#2a3442;color:#eef3f9;border:1px solid #435166;border-radius:6px;padding:5px 9px;font-size:13px;font-family:inherit}
select:hover,button:hover{border-color:#6d839f}
button:active{background:#3a4a5e;border-color:#7591b3;color:#ffffff;transform:translateY(1px);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.16)}
button.primary{background:#c8a13a;border-color:#c8a13a;color:#1a1a1a;font-weight:600}
button.primary:hover{background:#d9b24b}
button.primary:active{background:#f4dc92;border-color:#f4dc92;color:#12161c;transform:translateY(1px);
  box-shadow:0 0 12px rgba(244,220,146,.55),inset 0 1px 0 rgba(255,255,255,.55)}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;color:#cfd9e6;
  border:1px solid #4a5a70;border-radius:12px;padding:2.5px 10px;cursor:pointer;user-select:none;background:#2b3644;
  transition:background .12s,border-color .12s,color .12s}
.chip:hover{border-color:#7288a3;color:#ffffff}
.chip:active{background:#3a4a60;border-color:#7fa6cf;color:#ffffff;transform:translateY(1px)}
.chip.on{background:#2f6fb5;border-color:#7db4ea;color:#ffffff;font-weight:700;
  box-shadow:0 0 0 1px rgba(125,180,234,.45),0 0 12px rgba(64,138,214,.35)}
.chip.on::before{content:"\2713";font-size:11px;line-height:1;opacity:.95}
.chip.on:active{background:#3b83d0;border-color:#a8d2f5;color:#ffffff}
.chip.on.f-term{background:#8a6a1f;border-color:#e8bf5a;color:#fff8e3;
  box-shadow:0 0 0 1px rgba(232,191,90,.4),0 0 12px rgba(196,150,40,.35)}
.chip.on.f-auto{background:#8f3232;border-color:#ef8a8a;color:#ffecec;
  box-shadow:0 0 0 1px rgba(239,138,138,.4),0 0 12px rgba(200,70,70,.35)}
.chip.on.f-illu{background:#2f6293;border-color:#8cc4f5;color:#f0f9ff;
  box-shadow:0 0 0 1px rgba(140,196,245,.4),0 0 12px rgba(60,130,200,.35)}
.grp{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
main{padding:10px 18px 40px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#a9b5c6;font-weight:500;padding:8px 10px;border-bottom:1px solid #33404f;white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:#ffffff}
th .arrow{color:#c8a13a;font-size:11px}
td{padding:8px 10px;border-bottom:1px solid #303a49;vertical-align:top}
tr:hover td{background:#232c39}
.planet{font-weight:600;color:#e9eef6}
.sub{font-size:11px;color:#8f9aab}
.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.diff{display:inline-block;padding:1px 7px;border-radius:10px;background:#2c3746;font-size:12px;color:#c3cddb;white-space:nowrap}
.d1,.d2{background:#23402f;color:#a6e6b6}.d3,.d4{background:#29425c;color:#b7d6f5}
.d5,.d6{background:#47401f;color:#f0dda0}
.d7{background:#5a3636;color:#ffc2b2}.d8{background:#63382f;color:#ffc2b2}
.d9{background:#6d3a2c;color:#ffd6c2}.d10{background:#7d3524;color:#ffdfc9}
.fbadge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;white-space:nowrap}
.fbadge.f-term{background:#4a3d1c;color:#ffe6a8}.fbadge.f-auto{background:#4d2323;color:#ffc6c6}
.fbadge.f-illu{background:#243b57;color:#bcdcf7}.fbadge.f-none{background:#2c3746;color:#a3aebe}
.bar{display:inline-block;width:44px;height:6px;background:#303a48;border-radius:3px;vertical-align:middle;margin-right:6px;overflow:hidden}
.bar>i{display:block;height:100%;background:#5c9ded}
.ping{font-variant-numeric:tabular-nums}
.good{color:#67c07a}.mid{color:#e0cd8a}.bad{color:#ff9d8a}
.empty{padding:60px;text-align:center;color:#8f9aab}
.err{margin:10px 18px;padding:10px 14px;border:1px solid #6b2a1c;background:#2a1a17;color:#ffb4a2;border-radius:8px;white-space:pre-wrap;font-size:12px}
.ok{color:#67c07a}
.tipbar{margin:8px 0 2px;padding:7px 12px;border-radius:6px;font-size:13px;font-weight:600;
  color:#ffe6a8;background:rgba(120,86,20,.28);border:1px solid rgba(255,196,80,.5)}
.note{margin:0 18px 8px;color:#8f9aab;font-size:12px}
th:last-child,td:last-child{width:1%;text-align:right;white-space:nowrap}
.misb{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;
  background:#2a3546;color:#b6cbe2;font-variant-numeric:tabular-nums;white-space:nowrap}
.misb.guess{opacity:.78;font-style:italic}
.misb.none{background:#242d39;color:#93a0b1}
.diff.guess{opacity:.72;font-style:italic}
.sosb{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:800;
  letter-spacing:.5px;color:#1a1010;background:linear-gradient(180deg,#ffb4a2,#ff6b57);
  box-shadow:0 0 12px rgba(255,110,90,.45)}
.pvb{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;white-space:nowrap}
.pv-pub{background:#23402f;color:#a6e6b6}.pv-fri{background:#47401f;color:#f0dda0}
.pv-pri{background:#5a3636;color:#ffc2b2}.pv-unk{background:#2c3746;color:#a3aebe}
.host{max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#lang{min-width:44px}
.pdd{position:relative}
.pddpanel{position:absolute;top:calc(100% + 6px);right:0;z-index:40;display:none;background:#232c39;
  border:1px solid #435166;border-radius:8px;box-shadow:0 14px 36px rgba(0,0,0,.7)}
.pddpanel.open{display:flex}
.pcol{width:200px;max-height:430px;overflow:auto;padding:6px 0;border-right:1px solid #33404f}
.pcol:last-child{border-right:0}
.pcol:empty{display:none}
.pitem{display:flex;justify-content:space-between;gap:10px;padding:6px 12px;font-size:12.5px;
  color:#c2ccd9;cursor:pointer;white-space:nowrap}
.pitem:hover{background:#2c3746;color:#ffffff}
.pitem.on{background:#2f6fb5;color:#ffffff;font-weight:600}
.pitem .pc{color:#8b97a8;font-variant-numeric:tabular-nums}
.pitem .pc.warm{color:#ffcf5a}
.osb{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;white-space:nowrap;font-weight:600}
.os-steam{background:#1f3b57;color:#a9d3f2}
.os-psn{background:#22325c;color:#b9c8f8}
.os-xbox{background:#1e3a26;color:#a7e3b6}
.chip.on.os-pc{background:#2f6fb5;border-color:#7db4ea;color:#fff}
.chip.on.os-ps{background:#2f4f9e;border-color:#93aef2;color:#fff}
.chip.on.os-xbox{background:#2f7a45;border-color:#8fd9a4;color:#fff}
.mdd{position:relative}
.mpanel{position:fixed;top:104px;right:18px;z-index:45;display:none;
  width:min(1020px,calc(100vw - 36px));max-height:calc(100vh - 130px);background:#232c39;
  border:1px solid #4a5a70;border-radius:10px;box-shadow:0 18px 48px rgba(0,0,0,.72);
  flex-direction:column;overflow:hidden}
.mpanel.open{display:flex}
.mhead{display:flex;align-items:center;gap:10px;padding:10px 12px;background:#28323f;
  border-bottom:1px solid #3a4759}
.mhead input{flex:1;min-width:160px;background:#1d2530;border:1px solid #435166;border-radius:6px;
  color:#eef3f9;padding:6px 10px;font-size:13px;font-family:inherit}
.mhead input::placeholder{color:#7d8899}
.mstat{font-size:12px;color:#b6c1cf;white-space:nowrap}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;
  padding:12px;overflow:auto;flex:1;align-content:start}
.mitem{display:flex;align-items:center;gap:8px;padding:7px 10px;border:1px solid #3c4a5e;
  border-radius:8px;background:#27313f;color:#cfd9e6;font-size:12.5px;cursor:pointer;
  user-select:none;min-width:0}
.mitem:hover{border-color:#6d839f;background:#2e3949;color:#ffffff}
.mitem input{accent-color:#4a9bea;width:14px;height:14px;flex:none;margin:0}
.mitem .mname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mitem .mc{font-size:11px;color:#8b97a8;font-variant-numeric:tabular-nums;flex:none}
.mitem.on{background:#2f6fb5;border-color:#7db4ea;color:#ffffff;font-weight:600}
.mitem.on .mc{color:#dceafd}
.mempty{grid-column:1/-1;padding:24px;text-align:center;color:#8f9aab}
.mfoot{padding:8px 12px;border-top:1px solid #3a4759;color:#96a1b1;font-size:12px;background:#232c39}
#mbtn.on{border-color:#7db4ea;background:#2f6fb5;color:#ffffff;font-weight:700}
#mbtn .mcount:not(:empty){margin-left:6px;font-size:11px;font-weight:700;background:#0f1722;
  color:#bfe0ff;border-radius:9px;padding:0 6px}
.byline{margin-left:auto;font-size:12px;color:#586373;letter-spacing:.6px;user-select:none;
  padding-left:14px;white-space:nowrap}
.byline b{color:#7b8b9e;font-weight:600}
#prog{margin:0 18px 10px;display:none}
.pbar{height:9px;background:#232a35;border-radius:5px;overflow:hidden;border:1px solid #2e3644}
.pbar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#c8a13a,#f0d07a);transition:width .25s}
.ptxt{margin-top:5px;font-size:13px;color:#e8c46a;font-weight:600;letter-spacing:.3px}
.modal{position:fixed;inset:0;display:none;align-items:center;justify-content:center;
  background:rgba(4,8,14,.74);z-index:90}
.modal.open{display:flex}
.mbox{width:min(720px,calc(100vw - 64px));background:#27313e;border:1px solid #4a5a70;
  border-radius:12px;padding:20px 22px 16px;box-shadow:0 24px 60px rgba(0,0,0,.7)}
.mtext{font-size:15px;line-height:2.0;color:#eef3f9;margin:0 0 14px}
.mtext a{color:#7fc4ff;margin-left:4px}
.mfoot{display:flex;justify-content:flex-end}
</style></head>
<body>
<header>
  <div class="row">
    <div class="tilde"><h1 data-i18n="title">HD2 大厅搜索器</h1>
      <span class="ver">__VER__</span><a class="newver" id="newver" rel="noopener"
        style="display:none"></a></div>
    <span class="tag" id="state" data-i18n="state_ready">准备中…</span>
    <span class="tag" id="mine"></span>
    <span style="flex:1"></span>
    <div class="mdd" id="mdd">
      <button id="mbtn"><span data-i18n="mission_btn">任务</span><b class="mcount" id="mcount"></b> ▾</button>
      <div class="mpanel" id="mpanel">
        <div class="mhead">
          <input id="msearch" type="search" autocomplete="off"
                 placeholder="搜索任务名…" data-i18n-ph="m_search">
          <span class="mstat" id="mstat"></span>
          <button id="mselectall" data-i18n="m_select_all">全选</button>
          <button id="mclear" data-i18n="m_clear">清空</button>
        </div>
        <div class="mgrid" id="mgrid"></div>
        <div class="mfoot" data-i18n="m_foot">列表只包含当前查询结果里出现过的任务；选中的任务参与客户端筛选。</div>
      </div>
    </div>
    <div class="pdd" id="pdd">
      <button id="pbtn"><span data-i18n="planet">全部星球</span> ▾</button>
      <div class="pddpanel" id="ppanel">
        <div class="pcol" id="pc1"></div>
        <div class="pcol" id="pc2"></div>
        <div class="pcol" id="pc3"></div>
      </div>
    </div>
    <select id="diff"><option value="" data-i18n="diff">全部难度</option><option value="0" data-i18n="diff0">未选任务</option></select>
    <button id="savepreset" data-i18n="save_preset">保存预设</button>
    <button class="primary" id="refresh" data-i18n="refresh">刷新</button>
    <button id="upgrade" data-i18n="upgrade" disabled>升级</button>
    <button id="lang" title="language">EN</button>
    <button id="quit" data-i18n="quit">退出</button>
  </div>
  <div class="row">
    <span class="tag" data-i18n="front">战线</span>
    <div class="grp" id="fronts"></div>
    <span class="tag" data-i18n="region" style="margin-left:8px">地区</span>
    <div class="grp" id="regions"></div>
    <span class="tag" data-i18n="plat" style="margin-left:8px">平台</span>
    <div class="grp" id="plats"></div>
    <div class="grp" id="osgrp" style="margin-left:2px"></div>
    <span class="byline">By <b>QLongYan</b></span>
  </div>
  <div class="row">
    <label class="chip" id="c-nosession" data-i18n="c_nosession">隐藏无会话房</label>
    <label class="chip" id="c-hidefull" data-i18n="c_hidefull">隐藏满员</label>
    <label class="chip" id="c-hidefail" data-i18n="c_hidefail">隐藏进不去的房</label>
    <label class="chip" id="c-sos" data-i18n="c_sos">只看 SOS 房</label>
    <label class="chip on" id="c-public" data-i18n="c_public">只看公开房</label>
  </div>
</header>
<div id="prog"><div class="pbar"><i id="pfill"></i></div><div class="ptxt" id="ptxt"></div></div>
<div id="err"></div>
<div class="note" id="note"></div>
  <div class="tipbar" data-i18n="tip_delay">由于 steam 端和箭头服务器之间有延迟，刷新后请等 4–5 秒再点「加入」，避免进错房间</div>
<main><div id="list" class="empty" data-i18n="loading">正在查询…</div></main>
<div class="modal" id="umodal">
  <div class="mbox">
    <p class="mtext" id="mtext">下载失败 请到B站：<a
      href="https://space.bilibili.com/38739761?spm_id_from=333.788.0.0" target="_blank"
      rel="noopener">https://space.bilibili.com/38739761?spm_id_from=333.788.0.0</a><br>
      或 A网：<a href="https://ayakamods.com/mods/hd2lobbybrowser.6133/" target="_blank"
      rel="noopener">https://ayakamods.com/mods/hd2lobbybrowser.6133/</a>下载最新版<br>
      或 <a href="https://github.com/QLongYan/HD2LobbyBrowser/releases" target="_blank"
      rel="noopener">https://github.com/QLongYan/HD2LobbyBrowser/releases</a></p>
    <div class="mfoot"><button id="mok" data-i18n="ok_got_it">知道了</button></div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
const REGION_RANK={AS:1,OC:2,ME:3,EU:4,NA:5,SA:6,AF:7};
const DIFFCN=__DIFFCN__, DIFFEN=__DIFFEN__;
const REGIONCN=__REGIONCN__, REGIONEN=__REGIONEN__;
let LANG=localStorage.getItem('hd2lb_lang')||__LANG__;
const T=k=>(I18N[LANG]&&I18N[LANG][k])||I18N.zh[k]||k;
let planets=[], selfLobby='', rooms=[];
let sortKey='ping', sortDir=1;
const fronts=new Set(), regions=new Set(), plats=new Set(), osSel=new Set(), missionSel=new Set();
const FRONTS=[['Terminids','f_term','f-term'],['Automatons','f_auto','f-auto'],['Illuminate','f_illu','f-illu']];
const REGIONS=[['AS','r_as'],['NA','r_na'],['EU','r_eu'],['OC','r_oc'],['SA','r_sa'],['ME','r_me'],['AF','r_af']];
const PLATS=[['private','p_private']];
const OSS=[['steam','p_pc','os-pc'],['psn','p_ps','os-ps'],['xbox','p_xbox','os-xbox']];
function applyI18n(){
  document.querySelectorAll('[data-i18n]').forEach(el=>{ el.textContent=T(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{ el.placeholder=T(el.dataset.i18nPh); });
  document.title=T('title');
  document.documentElement.lang = LANG==='zh'?'zh-CN':'en';
  $('#lang').textContent=T('lang_btn');
  syncMission();
}
function chips(box, list, set){
  box.innerHTML='';
  list.forEach(([v,key,c])=>{
    const el=document.createElement('label');
    el.id='c-'+v;
    el.className='chip'+(c?' '+c:'')+(set.has(v)?' on':'');
    el.textContent=T(key);
    el.onclick=e=>{
      e.preventDefault();
      if(set.has(v)) set.delete(v); else set.add(v);
      el.classList.toggle('on', set.has(v));
      load();
    };
    box.appendChild(el);
  });
}
function buildChips(){
  chips($('#fronts'), FRONTS, fronts);
  chips($('#regions'), REGIONS, regions);
  chips($('#plats'), PLATS, plats);
  chips($('#osgrp'), OSS, osSel);
}
['c-nosession','c-hidefull','c-hidefail','c-public','c-sos'].forEach(id=>{
  if(!document.getElementById(id)) return;
  const el=document.getElementById(id);
  el.onclick=e=>{ e.preventDefault(); el.classList.toggle('on'); load(); };
});
function chipOn(id){ const e=document.getElementById(id); return !!(e && e.classList.contains('on')); }
const CHIP_IDS=['c-nosession','c-hidefull','c-hidefail','c-public','c-sos'];
let PRESET=null;
function collectPreset(){
  const chips={};
  CHIP_IDS.forEach(id=>{ chips[id]=chipOn(id); });
  return {
    planets: planetSel==='' ? '' : String(planetSel),
    fronts: [...fronts], regions: [...regions], plats: [...plats], os: [...osSel],
    missions: [...missionSel], diff: $('#diff').value, chips: chips,
    sort: {key: sortKey, dir: sortDir}
  };
}
async function savePreset(){
  const btn=$('#savepreset'); const label=btn.textContent;
  btn.disabled=true;
  try{
    const r=await fetch('/api/preset',{method:'POST',headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify(collectPreset())});
    const d=await r.json();
    btn.textContent=d.ok ? T('preset_saved') : T('preset_fail');
  }catch(e){ btn.textContent=T('preset_fail'); }
  setTimeout(()=>{ btn.textContent=label; btn.disabled=false; }, 1600);
}
function applyPreset(p){
  if(!p || typeof p!=='object') return false;
  try{
    planetSel=(p.planets==null) ? '' : String(p.planets);
    fronts.clear();  (p.fronts||[]).forEach(v=>fronts.add(v));
    regions.clear(); (p.regions||[]).forEach(v=>regions.add(v));
    plats.clear();   (p.plats||[]).forEach(v=>plats.add(v));
    osSel.clear();   (p.os||[]).forEach(v=>osSel.add(v));
    missionSel.clear(); (p.missions||[]).forEach(v=>missionSel.add(v));
    const chips=p.chips||{};
    CHIP_IDS.forEach(id=>{ const el=document.getElementById(id); if(el) el.classList.toggle('on', !!chips[id]); });
    if(p.sort && p.sort.key){ sortKey=p.sort.key; sortDir=(p.sort.dir===-1)?-1:1; }
    buildChips(); fillPlanets(); syncMission();
    return true;
  }catch(e){ return false; }
}
function showUpgradeFail(){ $('#umodal').classList.add('open'); }
$('#savepreset').onclick=e=>{ e.preventDefault(); savePreset(); };
$('#mok').onclick=()=>$('#umodal').classList.remove('open');
$('#umodal').addEventListener('click',e=>{ if(e.target===$('#umodal')) $('#umodal').classList.remove('open'); });
$('#upgrade').onclick=async()=>{
  const btn=$('#upgrade'); const label=btn.textContent;
  btn.disabled=true; btn.textContent=T('upgrading');
  // 45 秒还没下完：提示下载缓慢，给出三个手动下载入口（弹窗文案与失败一致）
  const slow=setTimeout(()=>{ showUpgradeFail(); }, 45000);
  try{
    const d=await (await fetch('/api/upgrade')).json();
    clearTimeout(slow);
    if(d && d.ok){
      btn.textContent=T('restarting');
      try{ await fetch('/api/restart'); }catch(e){}
      return;
    }
    showUpgradeFail();
  }catch(e){ clearTimeout(slow); showUpgradeFail(); }
  btn.disabled=false; btn.textContent=label;
};
let missionSig='';
function missionCounts(){
  const c={};
  rooms.forEach(r=>{ if(r.mission_name) c[r.mission_name]=(c[r.mission_name]||0)+1; });
  missionSel.forEach(n=>{ if(!(n in c)) c[n]=0; });
  return c;
}
function syncMission(){
  const btn=document.getElementById('mbtn');
  if(!btn) return;
  const on=missionSel.size>0;
  btn.classList.toggle('on', on);
  document.getElementById('mcount').textContent = on ? missionSel.size : '';
  document.getElementById('mstat').textContent = on ? T('m_sel').replace('{n}', missionSel.size) : T('m_all');
}
function buildMissionPanel(force){
  const c=missionCounts();
  const list=Object.keys(c).map(n=>({n:n, c:c[n], label:missionLabel(n)}))
    .sort((a,b)=> b.c-a.c || a.label.localeCompare(b.label, LANG==='en'?'en':'zh'));
  const sig=list.map(o=>o.n+': '+o.c).join('|');
  if(!force && sig===missionSig){ syncMission(); return; }
  missionSig=sig;
  const box=$('#mgrid');
  const q=($('#msearch').value||'').trim().toLowerCase();
  box.innerHTML='';
  let shown=0;
  list.forEach(o=>{
    if(q && o.label.toLowerCase().indexOf(q)<0 && o.n.toLowerCase().indexOf(q)<0) return;
    shown++;
    const el=document.createElement('label');
    el.className='mitem'+(missionSel.has(o.n)?' on':'');
    const cb=document.createElement('input');
    cb.type='checkbox'; cb.checked=missionSel.has(o.n);
    cb.onchange=()=>{
      if(cb.checked) missionSel.add(o.n); else missionSel.delete(o.n);
      el.classList.toggle('on', cb.checked);
      syncMission(); render();
    };
    const nm=document.createElement('span'); nm.className='mname'; nm.textContent=o.label; nm.title=o.label;
    const cnt=document.createElement('span'); cnt.className='mc'; cnt.textContent=o.c;
    el.appendChild(cb); el.appendChild(nm); el.appendChild(cnt);
    box.appendChild(el);
  });
  if(!shown){
    const e=document.createElement('div');
    e.className='mempty'; e.textContent=T('m_empty'); box.appendChild(e);
  }
  syncMission();
}
function openMissionPanel(){
  buildMissionPanel(true);
  const head=document.querySelector('header');
  const p=$('#mpanel');
  const bottom=head.getBoundingClientRect().bottom;
  p.style.top=Math.round(bottom+8)+'px';
  p.style.maxHeight=Math.max(260, Math.round(window.innerHeight-bottom-26))+'px';
  p.classList.add('open');
}
$('#mbtn').onclick=e=>{
  e.stopPropagation();
  const p=$('#mpanel');
  if(p.classList.contains('open')) p.classList.remove('open'); else openMissionPanel();
};
$('#msearch').oninput=()=>buildMissionPanel(true);
$('#mselectall').onclick=e=>{
  e.stopPropagation();
  Object.keys(missionCounts()).forEach(n=>missionSel.add(n));
  buildMissionPanel(true); render();
};
$('#mclear').onclick=e=>{
  e.stopPropagation();
  missionSel.clear(); buildMissionPanel(true); render();
};
document.addEventListener('click', e=>{
  const mdd=document.getElementById('mdd');
  if(mdd && !mdd.contains(e.target)) $('#mpanel').classList.remove('open');
});
document.addEventListener('keydown', e=>{ if(e.key==='Escape') $('#mpanel').classList.remove('open'); });
let planetSel='', curFront='', curSector='';
function pName(p){ return (LANG==='zh' && p.z) ? p.z : p.n; }
function sName(en, zh){ return (LANG==='zh' && zh) ? zh : en; }
function fmt(n){ return (n||0).toLocaleString(); }
function updatePbtn(){
  const b=$('#pbtn');
  if(planetSel===''){ b.innerHTML='<span>'+T('planet')+'</span> ▾'; return; }
  const p=planets.find(x=>String(x.i)===String(planetSel));
  b.innerHTML='<span>'+T('planet')+'：'+(p?pName(p):planetSel)+'</span> ▾';
}
function closePPanel(){ $('#ppanel').classList.remove('open'); }
function fillPlanets(){
  const c1=$('#pc1'); c1.innerHTML='';
  const all=document.createElement('div');
  all.className='pitem'+(planetSel===''?' on':'');
  all.innerHTML='<span>'+T('planet')+'</span><span class="pc">'+planets.length+'</span>';
  all.onclick=()=>{ planetSel=''; updatePbtn(); closePPanel(); fillPlanets(); load(); };
  c1.appendChild(all);

  const lines=[['Terminids',T('f_term')],['Automatons',T('f_auto')],['Illuminate',T('f_illu')],['',T('other')]];
  lines.forEach(([k,label])=>{
    const ps=planets.filter(p=> k ? p.f===k : !p.f);
    if(!ps.length) return;
    const tot=ps.reduce((a,b)=>a+(b.pc||0),0);
    const d=document.createElement('div');
    d.className='pitem'+(curFront===k?' on':'');
    d.innerHTML='<span>'+label+'</span><span class="pc'+(tot>0?' warm':'')+'">'+fmt(tot)+'</span>';
    d.onclick=(e)=>{ e.stopPropagation(); curFront=k; curSector=''; buildSectorCol(); buildPlanetCol(); };
    c1.appendChild(d);
  });
  if(curFront!=='') buildSectorCol(); else { $('#pc2').innerHTML=''; $('#pc3').innerHTML=''; }
  updatePbtn();
}
function buildSectorCol(){
  const c2=$('#pc2'); c2.innerHTML='';
  const ps=planets.filter(p=> curFront ? p.f===curFront : !p.f);
  const by={};
  ps.forEach(p=>{ const k=p.s||''; (by[k]=by[k]||[]).push(p); });
  const list=Object.keys(by).map(k=>({k, tot: by[k].reduce((a,b)=>a+(b.pc||0),0), n: by[k].length}));
  list.sort((a,b)=> b.tot-a.tot || a.k.localeCompare(b.k));
  list.forEach(o=>{
    const sample=by[o.k][0];
    const d=document.createElement('div');
    d.className='pitem'+(curSector===o.k?' on':'');
    d.innerHTML='<span>'+sName(o.k, sample.sz)+'</span><span class="pc'+(o.tot>0?' warm':'')+'">'+fmt(o.tot)+'</span>';
    d.onclick=(e)=>{ e.stopPropagation(); curSector=o.k; buildPlanetCol(); };
    c2.appendChild(d);
  });
}
function buildPlanetCol(){
  const c3=$('#pc3'); c3.innerHTML='';
  if(!curSector) return;
  const ps=planets.filter(p=>(p.s||'')===curSector).sort((a,b)=> (b.pc||0)-(a.pc||0));
  ps.forEach(p=>{
    const d=document.createElement('div');
    d.className='pitem'+(String(planetSel)===String(p.i)?' on':'');
    const own = p.o==='Humans'?'人类':(p.o==='Terminids'?'虫族':(p.o==='Automaton'||p.o==='Automatons'?'机器人':(p.o==='Illuminate'?'光能族':'')));
    d.innerHTML='<span>'+pName(p)+(own?'<span style="color:#5f6a78"> · '+own+'</span>':'')+'</span>'
               +'<span class="pc'+(p.pc>0?' warm':'')+'">'+fmt(p.pc)+'</span>';
    d.onclick=(e)=>{ e.stopPropagation(); planetSel=p.i; updatePbtn(); closePPanel(); fillPlanets(); load(); };
    c3.appendChild(d);
  });
}
function fillDiff(){
  const ds=$('#diff'); const curd=ds.value;
  ds.innerHTML='';
  const d0=document.createElement('option'); d0.value=''; d0.textContent=T('diff'); ds.appendChild(d0);
  const d1=document.createElement('option'); d1.value='0'; d1.textContent=T('diff0'); ds.appendChild(d1);
  const names = LANG==='zh'?DIFFCN:DIFFEN;
  for(let i=1;i<=10;i++){
    const o=document.createElement('option'); o.value=i; o.textContent=names[i]; ds.appendChild(o);
  }
  ds.value=curd;
}
function ago(s){
  if(s==null) return '-';
  if(s<60) return s+T('ago_s');
  if(s<3600) return Math.floor(s/60)+T('ago_m');
  return Math.floor(s/3600)+T('ago_h');
}
function pingCell(v){
  if(v==null) return '<span class="tag">—</span>';
  const c = v<80?'good':(v<160?'mid':'bad');
  return '<span class="ping '+c+'">'+v+' ms</span>';
}
async function selfInfo(){
  try{
    const d=await (await fetch('/api/self')).json();
    selfLobby=d.lobby||'';
    $('#mine').textContent = selfLobby ? (T('mine')+selfLobby.slice(-6)) : T('no_mine');
  }catch(e){}
}
let pollTimer=null;
function startProgress(){
  $('#prog').style.display='block';
  $('#pfill').style.width='4%';
  $('#ptxt').textContent=T('prog_start');
  if(pollTimer) clearInterval(pollTimer);
  pollTimer=setInterval(async()=>{
    try{
      const p=await (await fetch('/api/progress')).json();
      const pct = p.total ? Math.min(95, Math.round(p.scanned/p.total*100)) : 8;
      $('#pfill').style.width=Math.max(pct,6)+'%';
      const key = {list:'prog_list',details:'prog_details',names:'prog_names',platform:'prog_platform'}[p.phase] || 'prog_list';
      $('#ptxt').textContent = T(key).replace('{a}',p.scanned).replace('{b}',p.total);
    }catch(e){}
  },250);
}
function stopProgress(){
  if(pollTimer){ clearInterval(pollTimer); pollTimer=null; }
  $('#pfill').style.width='100%';
  setTimeout(()=>{ $('#prog').style.display='none'; $('#pfill').style.width='0'; }, 500);
}
async function load(){
  const q=new URLSearchParams();
  if(planetSel!=='') q.set('planets', planetSel);
  if(fronts.size) q.set('fronts', [...fronts].join(','));
  if(regions.size) q.set('regions', [...regions].join(','));
  if($('#diff').value!=='') q.set('diff',$('#diff').value);
  q.set('nosession', chipOn('c-nosession')?'1':'0');
  q.set('public', chipOn('c-public')?'1':'0');
  if(osSel.size) q.set('plat', [...osSel].join(','));
  q.set('hidefull', chipOn('c-hidefull')?'1':'0');
  $('#state').textContent=T('state_query'); $('#err').innerHTML='';
  startProgress();
  await selfInfo();
  try{
    const r=await fetch('/api/lobbies?'+q.toString());
    const d=await r.json();
    stopProgress();
    if(!d.ok){ $('#err').innerHTML='<div class="err">'+d.error+'</div>'; $('#state').textContent=T('state_fail'); return; }
    rooms=d.rooms;
    render();
    $('#state').innerHTML='<span class="ok">'+d.count+' '+T('state_rooms')+'</span> · '+new Date().toLocaleTimeString();
    $('#note').textContent = T('note_scan').replace('{n}',d.queried||1)
        .replace('{got}',d.scanned_ids||d.count).replace('{kept}',d.count) + ' ' + T('note');
  }catch(e){ stopProgress(); $('#err').innerHTML='<div class="err">'+e+'</div>'; }
}
function pvTxt(v){ return v===0?T('pv_public'):(v===1?T('pv_friend'):(v===2?T('pv_private'):T('pv_unknown'))); }
function pvCls(v){ return v===0?'pv-pub':(v===1?'pv-fri':(v===2?'pv-pri':'pv-unk')); }
const KEYVAL={
  planet:r=>r.planet, sos:r=>(r.sos?0:1), mission:r=>(missionLabel(r.mission_name)||r.mission_hash||'zzz'), privacy:r=>(r.privacy==null?9:r.privacy), front:r=>r.faction||'zzz', diff:r=>r.diff, members:r=>r.members,
  ping:r=>(r.ping==null?9999:r.ping), region:r=>(REGION_RANK[r.region]||9),
  age:r=>(r.age==null?-1:r.age), host:r=>(r.host_name||'zzz'),
  platform:r=>({steam:0,psn:1,xbox:2}[r.platform]||9)
};
function osCell(r){
  const n={steam:'PC',psn:'PS',xbox:'Xbox'}[r.platform];
  if(!n) return '<span class="tag">—</span>';
  const tip=(r.host_name?r.host_name+' · ':'')+T('os_tip');
  return '<span class="osb os-'+r.platform+'" title="'+tip+'">'+n+'</span>';
}
function missionLabel(zh){
  if(LANG!=='en' || !zh) return zh;
  const r=rooms.find(x=>x.mission_name===zh);
  return (r && r.mission_name_en) || zh;
}
function misCell(r){
  const h = r.mission_hash || '';
  const name = (LANG==='en' && r.mission_name_en) ? r.mission_name_en : r.mission_name;
  if(name){
    const tip = h ? T('mis_hash_tip').replace('{h}', h) : '';
    return '<span class="misb" title="'+tip+'">'+name+'</span>';
  }
  if(h) return '<span class="misb none" title="'+T('mis_unknown_tip').replace('{h}',h)+'">'+T('mis_unknown')+'</span>';
  return '<span class="misb none">—</span>';
}
function render(){
  if($('#mpanel').classList.contains('open')) buildMissionPanel(false);
  const PRIVATE_ON = chipOn('c-private');
  let rows=rooms.slice();
  if(PRIVATE_ON) rows=rows.filter(r=>r.host_name);
  if(chipOn('c-sos')) rows=rows.filter(r=>r.sos);
  if(missionSel.size) rows=rows.filter(r=>r.mission_name && missionSel.has(r.mission_name));
  const f=KEYVAL[sortKey]||KEYVAL.ping;
  rows.sort((a,b)=>{
    const x=f(a), y=f(b);
    if(x===y) return b.members-a.members;
    return (x<y?-1:1)*sortDir;
  });
  const arrow=k=>sortKey===k?(' <span class="arrow">'+(sortDir>0?'▲':'▼')+'</span>'):'';
  if(!rows.length){ $('#list').className='empty'; $('#list').textContent=T('empty'); return; }
  $('#list').className='';
  const diffs = LANG==='zh'?DIFFCN:DIFFEN;
  const regs = LANG==='zh'?REGIONCN:REGIONEN;
  const fname={'Terminids':T('f_term'),'Automatons':T('f_auto'),'Illuminate':T('f_illu')};
  let h='<table><thead><tr>'
    +'<th data-k="planet">'+T('th_planet')+arrow('planet')+'</th>'
    +'<th data-k="front">'+T('th_front')+arrow('front')+'</th>'
    +'<th data-k="diff">'+T('th_diff')+arrow('diff')+'</th>'
    +'<th data-k="members">'+T('th_members')+arrow('members')+'</th>'
    +'<th data-k="mission">'+T('th_mission')+arrow('mission')+'</th>'
    +'<th data-k="platform">'+T('th_platform')+arrow('platform')+'</th>'
    +'<th data-k="privacy">'+T('th_priv')+arrow('privacy')+'</th>'
    +'<th data-k="ping">'+T('th_ping')+arrow('ping')+'</th>'
    +'<th data-k="region">'+T('th_region')+arrow('region')+'</th>'
    +'<th data-k="host">'+T('th_host')+arrow('host')+'</th>'
    +'<th data-k="sos">'+T('th_sos')+arrow('sos')+'</th>'
    +'<th data-k="age">'+T('th_age')+arrow('age')+'</th>'
    +'<th></th></tr></thead><tbody>';
  rows.forEach(r=>{
    const pct=Math.min(100, r.members/(r.limit||4)*100);
    const mine=(r.lobby===selfLobby);
    const btn = mine ? '<span class="tag">'+T('you_here')+'</span>'
      : '<button class="primary" onclick="join(\''+r.lobby+'\',\''+r.host+'\',this)">'+T('join')+'</button>';
    const fcls={'Terminids':'f-term','Automatons':'f-auto','Illuminate':'f-illu'}[r.faction]||'f-none';
    const ftxt=fname[r.faction]||'—';
    const host=r.host_name?r.host_name:(r.kind==='steam'?'<span class="tag">—</span>':'<span class="tag">'+T('cross')+'</span>');
    const pname = (LANG==='zh' ? (r.planet_name_zh || r.planet_name) : r.planet_name) || T('no_planet');
    const sname = (LANG==='zh' ? (r.sector_zh || r.sector) : r.sector) || '';
    h+='<tr><td><div class="planet">'+pname+'</div><div class="sub">'+sname+'</div></td>'
      +'<td><span class="fbadge '+fcls+'">'+ftxt+'</span></td>'
      +'<td><span class="diff d'+r.diff+(r.diff_guess?' guess':'')+'" title="'+(r.diff_guess?T('diff_guess_tip'):'')+'">'+(r.diff?diffs[r.diff]:T('diff0'))+(r.diff_guess?' *':'')+'</span></td>'
      +'<td class="num"><span class="bar"><i style="width:'+pct+'%"></i></span>'+r.members+' / '+(r.limit||4)+'</td>'
      +'<td>'+misCell(r)+'</td>'
      +'<td>'+osCell(r)+'</td>'
      +'<td><span class="pvb '+pvCls(r.privacy)+'">'+pvTxt(r.privacy)+'</span></td>'
      +'<td>'+pingCell(r.ping)+'</td>'
      +'<td>'+(regs[r.region]||r.region||'—')+'</td>'
      +'<td class="host">'+(r.host_name||'—')+'</td>'
      +'<td>'+(r.sos?'<span class="sosb">SOS</span>':'')+'</td>'
      +'<td class="num">'+ago(r.age)+'</td>'
      +'<td>'+btn+'</td></tr>';
  });
  $('#list').innerHTML=h+'</tbody></table>';
  document.querySelectorAll('th[data-k]').forEach(th=>{
    th.onclick=()=>{
      const k=th.dataset.k;
      if(sortKey===k) sortDir=-sortDir; else { sortKey=k; sortDir=1; }
      render();
    };
  });
}
async function join(lobby,host,btn){
  btn.textContent=T('joining'); btn.disabled=true;
  const d=await (await fetch('/api/join?lobby='+lobby+'&host='+host)).json();
  if(!d.ok){ alert(d.error); btn.textContent=T('join'); btn.disabled=false; return; }
  for(let i=0;i<24;i++){
    await new Promise(r=>setTimeout(r,500));
    await selfInfo();
    if(selfLobby===lobby){ btn.textContent=T('joined'); $('#state').innerHTML='<span class="ok">'+T('state_joined')+'</span>'; load(); return; }
  }
  btn.textContent=T('join'); btn.disabled=false;
  await fetch('/api/failed?lobby='+lobby+'&reason=timeout');
  $('#err').innerHTML='<div class="err">'+T('err_join')+'</div>';
  load();
}
$('#quit').onclick=async()=>{ if(confirm(T('quit')+'?')){ try{ await fetch('/api/quit'); }catch(e){} window.close(); } };
$('#refresh').onclick=load;
$('#pbtn').onclick=e=>{ e.stopPropagation(); $('#ppanel').classList.toggle('open'); };
document.addEventListener('click',e=>{ if(!document.getElementById('pdd').contains(e.target)) closePPanel(); });
$('#diff').onchange=load;
$('#lang').onclick=()=>{
  LANG = LANG==='zh'?'en':'zh';
  localStorage.setItem('hd2lb_lang', LANG);
  applyI18n(); buildChips(); fillPlanets(); fillDiff(); render();
  paintVersion(VER);
};
applyI18n(); buildChips(); fillPlanets(); fillDiff(); render();
let VER=null;
function paintVersion(v){
  const a=document.getElementById('newver'), up=document.getElementById('upgrade');
  if(!a || !up) return;
  a.classList.remove('hot','bad');
  a.removeAttribute('href');
  a.onclick=null;
  if(!v || v.pending){ a.style.display='none'; return; }
  if(v.latest && v.latest > v.current){
    a.className='newver hot';
    a.textContent=T('newver').replace('%s','V'+v.latest);
    a.href=v.url || 'https://github.com/QLongYan/HD2LobbyBrowser/releases';
    a.target='_blank';
    a.title=T('upgrade_tip').replace('%s','V'+v.latest);
    up.disabled=false; up.classList.add('hot');
  }else if(v.error){
    a.className='newver bad';
    a.textContent=T('check_failed');
    a.title=v.error;
    up.disabled=false; up.classList.remove('hot');   // 允许手动重试
  }else{
    a.className='newver';
    a.textContent=T('up_to_date');
    up.disabled=true; up.classList.remove('hot');
  }
  a.style.display='inline-block';
}
async function initVersion(retry){
  try{
    const v=await (await fetch('/api/version')).json();
    VER=v;
    paintVersion(v);
    if(v && v.pending && retry>0) setTimeout(()=>initVersion(retry-1), 1500);
  }catch(e){
    if(retry>0){ setTimeout(()=>initVersion(retry-1), 1500); return; }
    paintVersion({current:0,error:'offline'});
  }
}
initVersion(3);
fetch('/api/preset').then(r=>r.json()).catch(()=>({})).then(p=>{
  PRESET=(p && typeof p==='object') ? p : {};
  return fetch('/api/planets');
}).then(r=>r.json()).then(d=>{
  planets=d.planets; fillPlanets(); fillDiff();
  for(let i=1;i<=10;i++){ if(!DIFFCN[i]) DIFFCN[i]=d.diff[i]; }
  if(PRESET && Object.keys(PRESET).length){       // 有预设就套用，并按它直接搜
    applyPreset(PRESET);
    fillDiff();
    if(PRESET.diff!=null) $('#diff').value=String(PRESET.diff);
  }
  load();
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
        except Exception:
            body = b""
        if u.path == "/api/preset":
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("bad preset")
                ok = preset_save(data)
            except Exception:
                ok = False
            return self._send(200, json.dumps({"ok": ok}, ensure_ascii=False))
        return self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            html = (PAGE
                    .replace("__VER__", "V%d" % VERSION)
                    .replace("__LANG__", json.dumps(DEFAULT_LANG))
                    .replace("__DIFFCN__", json.dumps(DIFF_CN, ensure_ascii=False))
                    .replace("__DIFFEN__", json.dumps(DIFF_EN, ensure_ascii=False))
                    .replace("__REGIONCN__", json.dumps(REGION_CN, ensure_ascii=False))
                    .replace("__REGIONEN__", json.dumps(REGION_EN, ensure_ascii=False))
                    .replace("__I18N__", json.dumps(I18N, ensure_ascii=False)))
            html = html.replace("<script>", "<script>window.I18N=" + json.dumps(I18N, ensure_ascii=False) + ";\n", 1)
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/version":
            # 启动时后台已在查，这里最多等 4 秒，尽量让首个页面就拿到结果
            pending = not VER_DONE.wait(4.0)
            return self._send(200, json.dumps(
                {"current": VERSION, "latest": VER_INFO["latest"], "url": VER_INFO["url"],
                 "checked": VER_INFO["checked"], "error": VER_INFO["error"],
                 "source": VER_INFO["source"], "pending": pending}, ensure_ascii=False))
            return
        if u.path == "/api/planets":
            counts = STATS.get() if STATS else {}
            live = STATS.factions() if STATS else {}
            owners, updated = STATS.owner_map() if STATS else ({}, 0)
            out = [{"i": p["i"], "n": p["n"], "s": p["s"],
                    "z": PLANETS_ZH.get(p["i"], ""),
                    "f": live.get(p["i"]) or FACTIONS.get(p["i"]) or "",
                    "sz": SECTORS_ZH.get(p["s"], ""),
                    "o": owners.get(p["i"], ""),
                    "pc": counts.get(p["i"], 0)} for p in PLANETS]
            return self._send(200, json.dumps(
                {"planets": out, "diff": DIFF_CN, "fshort": FACTION_SHORT,
                 "sectors": SECTORS_ZH, "updated": updated}, ensure_ascii=False))
        if u.path == "/api/lobbies":
            planets = q.get("planets", [""])[0]
            diff = q.get("diff", [None])[0]
            nosession = q.get("nosession", ["1"])[0] != "0"
            public_only = q.get("public", ["1"])[0] != "0"
            hidefull = q.get("hidefull", ["0"])[0] == "1"
            target = int(q.get("target", ["50"])[0] or 50)
            fronts = [x for x in q.get("fronts", [""])[0].split(",") if x]
            regions = [x for x in q.get("regions", [""])[0].split(",") if x]
            plats = [x for x in q.get("plats", [""])[0].split(",") if x]
            oses = [x for x in q.get("plat", [""])[0].split(",") if x in ("steam", "psn", "xbox")]
            plist = [x for x in planets.split(",") if x.strip().isdigit()] if planets else None
            res = STEAM.search(planets=plist, fronts=fronts, regions=regions, plats=plats,
                               diff=int(diff) if diff not in (None, "") else None,
                               hide_nosession=nosession, hide_full=hidefull,
                               public_only=public_only, platforms=oses or None)
            if res.get("ok") and FAILS:
                res["rooms"] = [r for r in res["rooms"] if not FAILS.failed(r["lobby"])]
                res["count"] = len(res["rooms"])
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/api/join":
            lobby = q.get("lobby", [""])[0]
            host = q.get("host", [""])[0]
            if not lobby.isdigit():
                return self._send(200, json.dumps({"ok": False, "error": "invalid lobby"}))
            url = STEAM.join_url(lobby, host)
            try:
                os.startfile(url)
                return self._send(200, json.dumps({"ok": True, "url": url}, ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        if u.path == "/api/failed":
            lobby = q.get("lobby", [""])[0]
            reason = q.get("reason", [""])[0]
            if lobby.isdigit() and FAILS:
                FAILS.mark(lobby, STEAM.rooms.get(lobby), reason)
            return self._send(200, json.dumps({"ok": True}))
        if u.path == "/api/progress":
            p = dict(PROGRESS)
            p["elapsed"] = round(time.time() - p["started"], 1) if p.get("started") else 0
            return self._send(200, json.dumps(p, ensure_ascii=False))
        if u.path == "/api/self":
            d = STEAM.self_lobby()
            d["v"] = VERSION
            return self._send(200, json.dumps(d, ensure_ascii=False))
        if u.path == "/api/preset":
            return self._send(200, json.dumps(preset_load(), ensure_ascii=False))
        if u.path == "/api/upgrade":
            ok, detail = upgrade_download()
            if not ok:
                return self._send(200, json.dumps(
                    {"ok": False, "error": UPGRADE_FAIL, "reason": detail}, ensure_ascii=False))
            applied, why = upgrade_apply(detail)
            if not applied:
                return self._send(200, json.dumps(
                    {"ok": False, "error": UPGRADE_FAIL, "reason": why}, ensure_ascii=False))
            return self._send(200, json.dumps({"ok": True, "restart": True}, ensure_ascii=False))
        if u.path == "/api/restart":
            threading.Thread(target=restart_app, daemon=True).start()
            return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
        if u.path == "/api/quit":
            threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
            return self._send(200, json.dumps({"ok": True}))
        return self._send(404, json.dumps({"ok": False, "error": "not found"}))


PRESET_FILE = "preset.json"
UPGRADE_PAGE = "https://ayakamods.com/mods/hd2lobbybrowser.6133/"
UPGRADE_FAIL = ("下载失败 请到B站：https://space.bilibili.com/38739761?spm_id_from=333.788.0.0"
                "  或 A网：https://ayakamods.com/mods/hd2lobbybrowser.6133/下载最新版"
                "  或 https://github.com/QLongYan/HD2LobbyBrowser/releases")
UPGRADE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def preset_load():
    try:
        with open(os.path.join(HERE, PRESET_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def preset_save(data):
    try:
        path = os.path.join(HERE, PRESET_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _fetch(url, referer=None, timeout=25):
    headers = {"User-Agent": UPGRADE_UA, "Accept": "*/*",
               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def direct_update_url():
    """update_url.txt 里配了 "file" 直链就用它（自建源 / 备用 CDN）。"""
    data = custom_update_source() or {}
    url = str(data.get("file") or "").strip()
    return url if url.lower().startswith("http") else ""


PKG_EXT = (".exe", ".json", ".txt", ".md")
# 这些是用户自己的东西，更新包一律不覆盖
PKG_DENY = {"preset.json", "update_url.txt", "hotkey.log",
            "startup_error.log", "join_fail.log"}


def _pkg_name(raw):
    """取包内文件的纯文件名；拒绝隐藏文件、路径穿越和用户配置。"""
    name = str(raw or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name.startswith(".") or name.lower() in PKG_DENY:
        return ""
    return name


def _grab_package(url, referer=None, timeout=70):
    """下载更新包（zip 或单个 exe）。

    返回 (True, {"exe": 临时exe, "extra": [(文件名, 临时路径)], "count": N}) 或 (False, 原因)。
    zip 里的 exe 用来替换主程序，其余 json / txt / md 会一并覆盖到 exe 同目录。
    """
    deadline = time.time() + timeout          # 整体预算，防止慢速连接一直挂着
    try:
        with _fetch(url, referer=referer, timeout=min(30, timeout)) as resp:
            chunks = []
            got = 0
            while True:
                if time.time() > deadline:
                    return False, "下载超时（网速太慢，%d 秒只拿到 %.1f MB）" % (timeout, got / 1048576.0)
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            blob = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        return False, "下载被拒: HTTP %s" % exc.code
    except Exception as exc:
        return False, "下载失败: %s" % exc

    exe_blob = None
    extras = []
    if blob[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except Exception as exc:
            return False, "压缩包解析失败: %s" % exc
        for item in zf.infolist():
            if item.is_dir():
                continue
            name = _pkg_name(item.filename)
            if not name:
                continue
            lower = name.lower()
            try:
                data = zf.read(item)
            except Exception:
                continue
            if lower.endswith(".exe"):
                if exe_blob is None and data[:2] == b"MZ":
                    exe_blob = data
                continue
            if lower.endswith(PKG_EXT) and data:
                extras.append((name, data))
    elif blob[:2] == b"MZ":
        exe_blob = blob
    else:
        return False, "下载到的不是 exe 也不是 zip"

    if not exe_blob or len(exe_blob) < 5 * 1024 * 1024:
        return False, "包里的 exe 无效（可能小于 5MB）"

    outdir = tempfile.mkdtemp(prefix="hd2lb_pkg_")
    try:
        exe_path = os.path.join(outdir, "HD2LobbyBrowser.exe")
        with open(exe_path, "wb") as fh:
            fh.write(exe_blob)
        written = []
        for name, data in extras:
            path = os.path.join(outdir, name)
            with open(path, "wb") as fh:
                fh.write(data)
            written.append((name, path))
    except Exception as exc:
        return False, "写入临时文件失败: %s" % exc
    return True, {"exe": exe_path, "extra": written, "count": len(written)}


def upgrade_download():
    """升级包来源：自定义直链 > Release 资产 > GitHub 固定地址 > 镜像。"""
    urls = []
    direct = direct_update_url()
    if direct:
        urls.append(direct)
    asset = str(VER_INFO.get("file") or "").strip()
    if asset.lower().startswith("http") and asset not in urls:
        urls.append(asset)
    if GITHUB_DL not in urls:
        urls.append(GITHUB_DL)
    for mirror in GITHUB_MIRRORS:                 # 国内直连不通时走镜像
        mirrored = mirror + GITHUB_DL
        if mirrored not in urls:
            urls.append(mirrored)
    reasons = []
    deadline = time.time() + 150                      # 总预算：不让用户干等
    for url in urls:
        if time.time() > deadline:
            reasons.append("超时")
            break
        budget = 70 if url.startswith("https://github.com/") else 35
        ok, detail = _grab_package(url, timeout=budget)
        if ok:
            return True, detail
        reasons.append("%s → %s" % (url.split("/")[2], detail))
    return False, "；".join(reasons[-3:]) or "没有可用的下载地址"


def cleanup_old():
    """清掉上次升级留下的 .old 文件（新进程启动时调用）。"""
    for fn in ("HD2LobbyBrowser.exe.old",):
        path = os.path.join(HERE, fn)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def upgrade_apply(pkg):
    """先覆盖数据文件，再替换 exe；exe 替换失败会回滚，不改坏现有程序。"""
    exe = os.path.abspath(sys.executable) if getattr(sys, "frozen", False) else ""
    if not exe or not os.path.exists(exe):
        return False, "非 exe 运行，跳过替换"
    new_exe = pkg.get("exe") if isinstance(pkg, dict) else pkg
    if not new_exe or not os.path.exists(new_exe):
        return False, "更新包里的 exe 不见了"

    done = 0
    for name, path in (pkg.get("extra") or []) if isinstance(pkg, dict) else []:
        try:
            shutil.copy2(path, os.path.join(HERE, name))
            done += 1
        except Exception:
            pass                                  # 单个数据文件失败不阻断整体

    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
        os.rename(exe, old)
    except Exception as exc:
        return False, "改名失败: %s" % exc
    try:
        shutil.copy2(new_exe, exe)
    except Exception as exc:
        try:
            os.rename(old, exe)
        except Exception:
            pass
        return False, "写入新版本失败: %s" % exc
    shutil.rmtree(os.path.dirname(new_exe), ignore_errors=True)
    return True, "已更新主程序 + %d 个数据文件" % done


def restart_app():
    exe = os.path.abspath(sys.executable) if getattr(sys, "frozen", False) else ""
    try:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)
    except Exception:
        pass
    time.sleep(0.4)
    os._exit(0)


def pick_port(start=8731):
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def existing_instance():
    """端口 8731 上已经有本工具在跑就复用，避免多开一堆实例。"""
    import urllib.error
    for port in range(8731, 8736):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/self" % port, timeout=0.6) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("v") == VERSION:
                return "http://127.0.0.1:%d/" % port
        except Exception:
            continue
    return None


def main():
    global STEAM, LATENCY, STATS, FAILS
    log = []
    def say(msg):
        log.append(msg)
        try:
            print(msg, flush=True)
        except Exception:
            pass
    say("HD2 大厅搜索器 V15")
    alive = existing_instance()
    if alive and not os.environ.get("HD2_FORCE_NEW"):
        say("already running: " + alive)
        if not os.environ.get("HD2_NO_BROWSER"):
            webbrowser.open(alive)
        return
    STEAM = SteamLobbies()
    LATENCY = Latency()
    STATS = PlanetStats()
    FAILS = JoinFailures()
    if STEAM.ok:
        say("Steam OK, SteamID: %s" % STEAM.self_id)
        LATENCY.warm()
        STATS.warm()
    else:
        say("!! " + STEAM.err)
    port = pick_port()
    threading.Thread(target=check_update, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    say("URL: " + url)
    try:
        with open(os.path.join(HERE, "startup.log"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(log))
    except Exception:
        pass
    if not os.environ.get("HD2_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        STEAM.shutdown()


if __name__ == "__main__":
    main()
