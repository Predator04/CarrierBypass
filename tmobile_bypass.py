#!/usr/bin/env python3
"""
T-Mobile Bypass — Windows utility
=================================
1. TTL/Hop-limit fix: makes tethered (hotspot) traffic look like phone-native
   traffic so T-Mobile's 600 kbps hotspot cap doesn't apply.
2. Parallel-chunk downloader: grabs large files (AI models, etc.) at full
   bandwidth using multiple simultaneous connections with resume support.
3. Download queue: line up multiple URLs, download them in order.
4. Auto-TTL watchdog: keep the bypass applied on boot and re-apply it if
   Windows resets the hop limit.
5. Hotspot auto-detect: enable the bypass automatically when you join a
   known phone hotspot.
6. Speed test history with a trend graph.
7. One-click "Bypass + Test": apply the fix, flush DNS, and measure before/after.
8. Self-update: check GitHub for a new release and swap in the new build.
9. System tray icon with quick toggles.

Run as Administrator (the .exe requests elevation via manifest; the .py
re-launches itself elevated). The TTL setting persists across reboots.

Logs: %APPDATA%\\T-MobileBypass\\tmobile_bypass.log  (crash dumps: CRASH.log)
"""

import os
import re
import sys
import time
import json
import socket
import shutil
import subprocess
import traceback
import tempfile
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from urllib.error import URLError

# ----------------------------------------------------------------------------
# Core (no GUI deps)
# ----------------------------------------------------------------------------

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
BYPASS_TTL = 65          # 64 + 1 hop for the phone
DEFAULT_TTL = 128        # Windows default hop limit

VERSION = "1.1.0"
GITHUB_REPO = "Predator04/T-MobileBypass"
GITHUB_API = "https://api.github.com/repos/" + GITHUB_REPO

WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if WIN else 0

# Common phone-hotspot SSIDs used for auto-detect. User can extend in Settings.
DEFAULT_HOTSPOT_SSIDS = [
    "iphone", "androidap", "galaxy", "pixel", "oneplus", "hotspot",
    "moto", "motorola", "honor", "xiaomi", "redmi", "samsung", "s24", "s23",
    "iphone hotspot", "personal hotspot",
]


def _run(cmd):
    kwargs = dict(capture_output=True, text=True)
    if WIN:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        return subprocess.run(cmd, timeout=30, **kwargs)
    except Exception as e:
        class _R:
            returncode = 1
            stdout = ""
            stderr = str(e)
        return _R()


# ----------------------------------------------------------------------------
# Config + history (JSON under %APPDATA%\T-MobileBypass)
# ----------------------------------------------------------------------------

def _data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "T-MobileBypass")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return tempfile.gettempdir()


def _config_path():
    return os.path.join(_data_dir(), "config.json")


def _history_path():
    return os.path.join(_data_dir(), "history.json")


def _load_config():
    default = {
        "auto_bypass": False,
        "auto_start": False,
        "hotspot_auto": False,
        "hotspot_ssids": list(DEFAULT_HOTSPOT_SSIDS),
        "check_updates": True,
    }
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in default.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return dict(default)


def _save_config(cfg):
    p = _config_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def _load_history():
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _append_history(entry):
    hist = _load_history()
    hist.append(entry)
    # keep last 200 points
    hist = hist[-200:]
    p = _history_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f)
        os.replace(tmp, p)
    except Exception:
        pass
    return hist


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

LOG_MAX_BYTES = 512 * 1024  # rotate the log once it grows past ~512 KB


def log(msg, level="INFO"):
    try:
        path = os.path.join(_data_dir(), "tmobile_bypass.log")
        try:
            if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
                rotated = path + ".1"
                if os.path.exists(rotated):
                    os.remove(rotated)
                os.replace(path, rotated)
        except Exception:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [{level}] {msg}\n")
    except Exception:
        pass


def log_path():
    return os.path.join(_data_dir(), "tmobile_bypass.log")


def _install_excepthook():
    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        log("UNHANDLED EXCEPTION:\n" + msg, "ERROR")
        try:
            with open(os.path.join(_data_dir(), "CRASH.log"), "w", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook


# ----------------------------------------------------------------------------
# TTL / network core
# ----------------------------------------------------------------------------

def get_hoplimit():
    """Return current IPv4 default hop limit, or None if unavailable."""
    out = _run(["netsh", "int", "ipv4", "show", "glob"]).stdout
    m = re.search(r"default\s+hop\s+limit\s*:?\s*(\d+)", out, re.I)
    return int(m.group(1)) if m else None


def set_hoplimit(value):
    """Set IPv4+IPv6 default hop limit. Returns (ok, detail)."""
    r1 = _run(["netsh", "int", "ipv4", "set", "glob", f"defaultcurhoplimit={value}"])
    r2 = _run(["netsh", "int", "ipv6", "set", "glob", f"defaultcurhoplimit={value}"])
    detail = (r1.stdout + r1.stderr + r2.stdout + r2.stderr).strip()
    if r1.returncode != 0 or r2.returncode != 0:
        return False, detail
    if get_hoplimit() != value:
        return False, "hop limit did not change after set"
    return True, detail


def flush_dns():
    r = _run(["ipconfig", "/flushdns"])
    return r.returncode == 0


def is_admin():
    if not WIN:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    if not WIN:
        return False
    import ctypes
    try:
        if getattr(sys, "frozen", False):
            params = " ".join(f'"{a}"' for a in sys.argv[1:])
        else:
            params = " ".join(f'"{a}"' for a in [os.path.abspath(sys.argv[0])] + sys.argv[1:])
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                  params or "", None, 1)
        if ret <= 32:
            log(f"relaunch_as_admin failed: ShellExecuteW returned {ret}")
            return False
        return True
    except Exception as e:
        log(f"relaunch_as_admin failed: {e}")
        return False


def get_active_ssid():
    """Return the SSID of the currently connected Wi-Fi, or None."""
    out = _run(["netsh", "wlan", "show", "interfaces"]).stdout
    m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
    if m:
        return m.group(1).strip()
    return None


def get_gateway_ip():
    """Best-effort default gateway IP, or None."""
    out = _run(["ipconfig"]).stdout
    m = re.search(r"Default Gateway[ .]*:\s*([0-9.]+)", out)
    if m:
        return m.group(1)
    return None


def is_hotspot_ssid(ssid, extra_ssids):
    """Return True if ssid looks like a phone hotspot (whole-token match)."""
    if not ssid:
        return False
    tokens = re.findall(r"[a-z0-9]+", ssid.lower())
    pool = set((s or "").lower().strip() for s in (extra_ssids or []))
    pool |= set(DEFAULT_HOTSPOT_SSIDS)
    pool.discard("")
    return any(t in pool for t in tokens)


# ----------------------------------------------------------------------------
# Boot startup (HKCU Run key)
# ----------------------------------------------------------------------------

def _app_path():
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0] or __file__)


def _startup_command():
    """Command string to launch the app at logon (minimized to tray)."""
    if getattr(sys, "frozen", False):
        return f'"{_app_path()}" --minimized'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{_app_path()}" --minimized'


def set_startup_enabled(enable):
    if not WIN:
        return False
    try:
        import winreg
    except Exception as e:
        log(f"winreg import failed: {e}")
        return False
    sub = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, "T-MobileBypass", 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(k, "T-MobileBypass")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        log(f"set_startup_enabled({enable}) failed: {e}")
        return False


def is_startup_enabled():
    if not WIN:
        return False
    try:
        import winreg
    except Exception:
        return False
    sub = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_READ) as k:
            try:
                winreg.QueryValueEx(k, "T-MobileBypass")
                return True
            except FileNotFoundError:
                return False
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Self-update (GitHub releases)
# ----------------------------------------------------------------------------

def _version_tuple(v):
    return tuple(int(x) for x in re.sub(r"[^0-9.]", "", v).split(".") if x.isdigit())


def check_latest_version():
    """Return (latest_version, asset_url) or (None, None) on failure/no-release."""
    try:
        req = Request(GITHUB_API + "/releases/latest", headers={"User-Agent": UA})
        with urlopen(req, timeout=20) as r:
            data = json.load(r)
        tag = (data.get("tag_name") or "").lstrip("v")
        assets = data.get("assets") or []
        asset_url = None
        for a in assets:
            name = (a.get("name") or "").lower()
            if name.endswith(".exe"):
                asset_url = a.get("browser_download_url")
                break
        return (tag or None, asset_url)
    except URLError as e:
        code = getattr(e, "code", None)
        if code == 404:
            return (None, None)  # no release yet
        log(f"check_latest_version error: {e}")
        return (None, None)
    except Exception as e:
        log(f"check_latest_version error: {e}")
        return (None, None)


def download_update_asset(asset_url, dest_path, progress_cb=None):
    """Download the update asset to dest_path. Returns True on success."""
    try:
        req = Request(asset_url, headers={"User-Agent": UA})
        with urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
        return True
    except Exception as e:
        log(f"download_update_asset error: {e}")
        return False


def install_update(new_exe):
    """Swap new_exe over the running build and restart (frozen EXE only)."""
    if not WIN or not getattr(sys, "frozen", False):
        return False
    try:
        cur = sys.executable
        bat = os.path.join(tempfile.gettempdir(), "tmb_update.bat")
        lines = [
            "@echo off",
            "setlocal",
            ":loop",
            f'move /y "{new_exe}" "{cur}" >nul 2>&1',
            "if errorlevel 1 (timeout /t 1 /nobreak >nul & goto loop)",
            f'start "" "{cur}"',
            'del "%~f0"',
        ]
        with open(bat, "w", encoding="ascii") as f:
            f.write("\r\n".join(lines))
        subprocess.Popen(["cmd", "/c", bat], creationflags=CREATE_NO_WINDOW)
        return True
    except Exception as e:
        log(f"install_update error: {e}")
        return False


# ----------------------------------------------------------------------------
# Parallel downloader (thread-safe, resumable)
# ----------------------------------------------------------------------------

class DownloadError(Exception):
    pass


class ParallelDownloader:
    BLOCK = 1 << 20  # 1 MiB resume granularity

    def __init__(self, url, dest, threads=12, resume=True):
        self.url = url
        self.dest = dest
        self.threads = max(1, int(threads))
        self.resume = resume
        self.size = 0
        self.supports_ranges = False
        self.done = 0
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._last_done = 0
        self._last_time = time.time()
        self.speed = 0.0
        self.part = dest + ".part"
        self.meta = dest + ".part.meta"
        self._bitmap = None

    def _head(self):
        try:
            req = Request(self.url, method="HEAD", headers={"User-Agent": UA})
            with urlopen(req, timeout=30) as r:
                self.size = int(r.headers.get("Content-Length") or 0)
        except Exception:
            self.size = 0

    def _probe_ranges(self):
        try:
            req = Request(self.url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
            with urlopen(req, timeout=30) as r:
                status = getattr(r, "status", None) or getattr(r, "code", None) or 200
                cr = r.headers.get("Content-Range")
                return int(status) == 206 and cr is not None
        except Exception:
            return None  # transient error — unknown range support, don't destroy resume state

    def _nblocks(self):
        return (self.size + self.BLOCK - 1) // self.BLOCK if self.size > 0 else 0

    def _load_bitmap(self):
        n = self._nblocks()
        bm = bytearray(n)
        if self.resume and os.path.exists(self.meta) and os.path.exists(self.part):
            try:
                data = open(self.meta, "rb").read()
                if data.startswith(b"SIZE="):
                    nl = data.index(b"\n")
                    saved_size = int(data[5:nl])
                    if saved_size != self.size:
                        return bm  # remote size changed → discard stale bitmap
                    data = data[nl + 1:]
                k = min(n, len(data))
                bm[:k] = data[:k]
            except Exception:
                bm = bytearray(n)
        return bm

    def _save_bitmap(self):
        if self._bitmap is None:
            return
        try:
            with open(self.meta, "wb") as f:
                f.write(f"SIZE={self.size}\n".encode())
                f.write(bytes(self._bitmap))
        except Exception:
            pass

    def _completed_bytes(self):
        if self._bitmap is None:
            return 0
        return min(self.size, sum(self._bitmap) * self.BLOCK)

    def _mark(self, start, end):
        if self._bitmap is None:
            return
        b0, b1 = start // self.BLOCK, end // self.BLOCK
        with self._lock:
            for b in range(b0, b1 + 1):
                if b < len(self._bitmap):
                    self._bitmap[b] = 1

    def _mark_completed_upto(self, start, end_written_inclusive):
        """Mark only blocks whose final byte has actually been written.

        Avoids marking a block complete when the write pointer has only touched
        its first byte — otherwise a resume after a mid-block cancel would skip
        the unwritten tail and silently corrupt the file.
        """
        if self._bitmap is None:
            return
        b0 = start // self.BLOCK
        last_full = (end_written_inclusive + 1) // self.BLOCK - 1
        if last_full < b0:
            return
        with self._lock:
            for b in range(b0, last_full + 1):
                if 0 <= b < len(self._bitmap):
                    self._bitmap[b] = 1

    def _fetch_range(self, start, end):
        if self._cancel.is_set():
            return
        headers = {"User-Agent": UA, "Range": f"bytes={start}-{end}"}
        req = Request(self.url, headers=headers)
        with urlopen(req, timeout=60) as r:
            status = getattr(r, "status", None) or getattr(r, "code", None) or 200
            if int(status) != 206:
                raise DownloadError(f"server ignored Range request (HTTP {status})")
            pos = start
            with open(self.part, "r+b") as fh:
                while True:
                    if self._cancel.is_set():
                        break
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    fh.seek(pos)
                    fh.write(chunk)
                    n = len(chunk)
                    pos += n
                    with self._lock:
                        self.done += n
                        now = time.time()
                        dt = now - self._last_time
                        if dt >= 0.5:
                            self.speed = (self.done - self._last_done) / dt
                            self._last_done = self.done
                            self._last_time = now
                    self._mark_completed_upto(start, pos - 1)
            if self._cancel.is_set():
                return  # cancelled mid-range — partial blocks already marked
            if pos - 1 != end:
                raise DownloadError(f"short read: got {pos - start} of {end - start + 1} bytes")
            self._mark(start, end)

    def _parallel(self, progress_cb, cancel_check):
        n = self._nblocks()
        self._bitmap = self._load_bitmap()
        if not os.path.exists(self.part):
            self._bitmap = bytearray(n)

        missing = []
        i = 0
        CHUNK_BLOCKS = 32  # ~32 MiB per worker task — so fresh downloads actually parallelize
        while i < n:
            if self._bitmap[i]:
                i += 1
                continue
            s = i
            while i < n and not self._bitmap[i]:
                i += 1
            c = s
            while c < i:
                e = min(c + CHUNK_BLOCKS, i)
                missing.append((c * self.BLOCK, min(e * self.BLOCK, self.size) - 1))
                c = e

        if not missing:
            self._finalize()
            return

        self.done = self._completed_bytes()
        self._last_done = self.done
        self._start_time = time.time()
        self._last_time = time.time()

        if not os.path.exists(self.part):
            open(self.part, "wb").close()
        with open(self.part, "r+b") as fh:
            fh.truncate(self.size)

        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            futs = [ex.submit(self._fetch_range, s, e) for s, e in missing]
            while not all(f.done() for f in futs):
                if cancel_check and cancel_check():
                    self._cancel.set()
                if progress_cb:
                    progress_cb(self.done, self.size, self.speed)
                time.sleep(0.2)
            try:
                for f in futs:
                    f.result()  # re-raise DownloadError
            finally:
                self._save_bitmap()  # persist progress even when a worker fails

        if self._cancel.is_set():
            raise DownloadError("cancelled")
        self._finalize()

    def _sequential(self, progress_cb, cancel_check):
        start = 0
        if self.resume and self.supports_ranges and os.path.exists(self.part):
            start = os.path.getsize(self.part)
        headers = {"User-Agent": UA}
        mode = "wb"
        if start > 0:
            headers["Range"] = f"bytes={start}-"
            mode = "ab"
        req = Request(self.url, headers=headers)
        self.done = start
        self._last_done = start
        self._start_time = time.time()
        self._last_time = time.time()
        with urlopen(req, timeout=60) as r:
            status = getattr(r, "status", None) or getattr(r, "code", None) or 200
            if start > 0 and int(status) != 206:
                raise DownloadError("server ignored Range on resume; restart download")
            if mode == "wb":
                self.size = int(r.headers.get("Content-Length") or 0)
            with open(self.part, mode) as fh:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    self.done += len(chunk)
                    now = time.time()
                    dt = now - self._last_time
                    if dt >= 0.5:
                        self.speed = (self.done - self._last_done) / dt
                        self._last_done = self.done
                        self._last_time = now
                    if progress_cb:
                        progress_cb(self.done, self.size, self.speed)
                    if cancel_check and cancel_check():
                        raise DownloadError("cancelled")
        self._finalize()

    def _finalize(self):
        if os.path.exists(self.part):
            os.replace(self.part, self.dest)  # atomic overwrite — never pre-delete dest
        if os.path.exists(self.meta):
            try:
                os.remove(self.meta)
            except Exception:
                pass

    def download(self, progress_cb=None, cancel_check=None):
        self._head()
        if self.size > 0:
            probe = self._probe_ranges()
            self.supports_ranges = True if probe is None else probe  # assume ranges on transient failure
        else:
            self.supports_ranges = False
        if not self.supports_ranges and os.path.exists(self.part):
            os.remove(self.part)
            if os.path.exists(self.meta):
                os.remove(self.meta)
        if not self.supports_ranges or self.size <= 0 or self.threads == 1:
            self._sequential(progress_cb, cancel_check)
            return self.dest
        self._parallel(progress_cb, cancel_check)
        return self.dest


def speed_test(callback=None, size_bytes=25_000_000):
    """Download `size_bytes` from Cloudflare speed-test and return Mbps."""
    url = f"https://speed.cloudflare.com/__down?bytes={size_bytes}"
    t0 = time.time()
    got = 0
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=30) as r:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                got += len(chunk)
                if callback:
                    callback(got, size_bytes, got / max(time.time() - t0, 0.01))
    except (URLError, Exception) as e:
        return -1, str(e)
    dt = max(time.time() - t0, 0.01)
    mbps = (got * 8) / dt / 1_000_000
    return mbps, None


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def safe_filename(name):
    name = urllib.parse.unquote(name or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    return name[:200] if name else "download.bin"


def detect_connection():
    try:
        host = socket.gethostname()
        ip = socket.gethostbyname(host)
    except Exception:
        host, ip = "?", "?"
    return host, ip


# ----------------------------------------------------------------------------
# GUI (PySide6, lazy import)
# ----------------------------------------------------------------------------

def build_ui():
    _install_excepthook()

    if is_admin() is False and WIN:
        log("not admin — relaunching elevated")
        if not relaunch_as_admin():
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "T-Mobile Bypass needs Administrator rights to change the hop limit.\n\n"
                    "Re-open the app and click Yes on the UAC prompt.",
                    "T-Mobile Bypass", 0x10)
            except Exception:
                pass
        sys.exit(0)

    try:
        from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                                       QLabel, QPushButton, QProgressBar, QLineEdit,
                                       QFrame, QTabWidget, QListWidget, QListWidgetItem,
                                       QCheckBox, QMessageBox, QSystemTrayIcon, QMenu,
                                       QScrollArea, QSizePolicy, QComboBox)
        from PySide6.QtCore import Qt, QThread, Signal, QTimer
        from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QPen, QPolygonF, QAction
        from PySide6.QtCore import QPointF
        import ctypes
    except ImportError:
        log("PySide6 not installed")
        print("PySide6 not installed. Run:  pip install PySide6")
        sys.exit(1)

    ACCENT = "#00d4aa"
    WARN = "#ffb454"
    BAD = "#ff5c6c"
    TEXT = "#e8ecf3"
    MUTED = "#8b93a5"
    SURFACE = "rgba(255,255,255,0.06)"

    cfg = _load_config()

    def _acrylic(hwnd):
        try:
            class ACCENT_POLICY(ctypes.Structure):
                _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                            ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]
            class WCAD(ctypes.Structure):
                _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                            ("SizeOfData", ctypes.c_size_t)]
            accent = ACCENT_POLICY()
            accent.AccentState = 3
            accent.GradientColor = 0xCC0A0A0A
            data = WCAD()
            data.Attribute = 19
            data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
            data.SizeOfData = ctypes.sizeof(accent)
            ctypes.windll.user32.SetWindowCompositionAttribute(int(hwnd), ctypes.byref(data))
        except Exception:
            pass

    def _card(parent, title):
        f = QFrame(parent)
        f.setStyleSheet(f"QFrame {{ background:{SURFACE}; border-radius:14px; }}")
        v = QVBoxLayout(f)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(6)
        t = QLabel(title)
        t.setStyleSheet(f"color:{MUTED}; font-size:11px; letter-spacing:1px; font-weight:600;")
        v.addWidget(t)
        return f, v

    def _icon():
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(ACCENT))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4, 4, 56, 56, 16, 16)
        p.setPen(QColor("#06231d"))
        f = QFont()
        f.setPixelSize(30)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, "65")
        p.end()
        return QIcon(pm)

    # ---- speed history graph ----
    class TrendGraph(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setMinimumHeight(160)
            self._points = []  # list of (mbps, ttl)

        def set_points(self, points):
            self._points = points[-60:]
            self.update()

        def paintEvent(self, e):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            p.fillRect(self.rect(), QColor(0, 0, 0, 70))
            if len(self._points) < 2:
                p.setPen(QColor(MUTED))
                p.drawText(self.rect(), Qt.AlignCenter, "run a few tests to see the trend")
                p.end()
                return
            vals = [x[0] for x in self._points]
            vmax = max(vals) or 1
            vmin = min(vals)
            span = max(vmax - vmin, 0.01)
            pad = 14

            def pt(i, v):
                x = pad + (w - 2 * pad) * i / (len(self._points) - 1)
                y = h - pad - (h - 2 * pad) * (v - vmin) / span
                return QPointF(x, y)

            # grid
            p.setPen(QColor(255, 255, 255, 24))
            for frac in (0.0, 0.5, 1.0):
                gy = h - pad - (h - 2 * pad) * frac
                p.drawLine(int(pad), int(gy), int(w - pad), int(gy))
            # line
            poly = QPolygonF([pt(i, v) for i, (v, _) in enumerate(self._points)])
            p.setPen(QPen(QColor(ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawPolyline(poly)
            # dots colored by bypass state
            for i, (v, ttl) in enumerate(self._points):
                col = QColor(ACCENT) if ttl == BYPASS_TTL else QColor(MUTED)
                p.setBrush(col)
                p.setPen(Qt.NoPen)
                p.drawEllipse(pt(i, v), 4, 4)
            # labels
            p.setPen(QColor(MUTED))
            p.drawText(int(pad), h - 2, f"{vmin:.0f}")
            p.drawText(int(w - pad - 30), 12, f"{vmax:.0f} Mbps")
            p.end()

    # ---- workers ----
    class Worker(QThread):
        done = Signal(object)

        def __init__(self, fn, *a, **k):
            super().__init__()
            self.fn = fn
            self.a = a
            self.k = k

        def run(self):
            try:
                self.done.emit(("ok", self.fn(*self.a, **self.k)))
            except Exception as e:
                log(f"Worker error: {e}")
                self.done.emit(("err", str(e)))

    class DownloadWorker(QThread):
        progress = Signal(float, float, float)   # done, total, speed (float64 avoids 2GB int overflow)
        result = Signal(object)

        def __init__(self, url, dest, threads):
            super().__init__()
            self.url = url
            self.dest = dest
            self.threads = threads

        def run(self):
            try:
                dl = ParallelDownloader(self.url, self.dest, threads=self.threads, resume=True)
                path = dl.download(progress_cb=lambda d, t, s: self.progress.emit(d, t, s))
                self.result.emit(("ok", path))
            except Exception as e:
                log(f"Download error: {e}")
                self.result.emit(("err", str(e)))

    class QueueWorker(QThread):
        item_progress = Signal(int, float, float, float)   # index, done, total, speed (float64 for big files)
        item_result = Signal(int, object)              # index, ("ok",path)|("err",msg)
        all_done = Signal()

        def __init__(self, items, threads=12):
            super().__init__()
            self.items = items  # list of (url, dest)
            self.threads = threads
            self._cancel = False

        def run(self):
            for i, (url, dest) in enumerate(self.items):
                if self._cancel:
                    break
                try:
                    dl = ParallelDownloader(url, dest, threads=self.threads, resume=True)
                    path = dl.download(
                        progress_cb=lambda d, t, s, idx=i: self.item_progress.emit(idx, d, t, s))
                    self.item_result.emit(i, ("ok", path))
                except Exception as e:
                    log(f"Queue item {i} error: {e}")
                    self.item_result.emit(i, ("err", str(e)))
            self.all_done.emit()

        def cancel(self):
            self._cancel = True

    # ---- main window ----
    class MainWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("T-Mobile Bypass")
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setFixedSize(600, 760)
            self._drag = None
            self._speed_worker = None
            self._dl_worker = None
            self._queue_worker = None
            self._update_worker = None
            self._bypass_test = None
            self._quitting = False
            self._build()
            self._build_tray()
            log("app started")

        # ---- tray ----
        def _build_tray(self):
            self.tray = QSystemTrayIcon(_icon(), self)
            self.tray.setToolTip("T-Mobile Bypass")
            menu = QMenu()
            a_show = QAction("Open", self)
            a_show.triggered.connect(self._show_from_tray)
            a_bypass = QAction("Enable bypass", self)
            a_bypass.triggered.connect(self._tray_toggle_bypass)
            a_speed = QAction("Speed test", self)
            a_speed.triggered.connect(self._tray_speed)
            a_quit = QAction("Quit", self)
            a_quit.triggered.connect(self._quit)
            for a in (a_show, a_bypass, a_speed):
                menu.addAction(a)
            menu.addSeparator()
            menu.addAction(a_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()

        def _on_tray_activated(self, reason):
            if reason == QSystemTrayIcon.Trigger:
                self._show_from_tray()

        def _show_from_tray(self):
            self.showNormal()
            self.raise_()
            self.activateWindow()

        def _tray_toggle_bypass(self):
            self._on_toggle()

        def _tray_speed(self):
            self.tabs.setCurrentIndex(1)
            self._show_from_tray()
            self._on_speed()

        def _quit(self):
            self._quitting = True
            self.tray.hide()
            QApplication.instance().quit()

        # ---- UI build ----
        def _build(self):
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            bar = QHBoxLayout()
            title = QLabel("T-Mobile Bypass")
            title.setStyleSheet(f"color:{TEXT}; font-size:16px; font-weight:700;")
            sub = QLabel(f"v{VERSION}  ·  hotspot cap killer  ·  fast downloader")
            sub.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            tv = QVBoxLayout(); tv.setSpacing(0)
            tv.addWidget(title); tv.addWidget(sub)
            bar.addLayout(tv); bar.addStretch()
            close = QPushButton("—")
            close.setFixedSize(30, 30)
            close.setToolTip("Minimize to tray")
            close.setStyleSheet(
                f"QPushButton {{ color:{MUTED}; background:transparent; border:none; font-size:16px; }}\n"
                f"QPushButton:hover {{ color:{TEXT}; background:rgba(255,255,255,0.08); border-radius:8px; }}")
            close.clicked.connect(self.hide)
            bar.addWidget(close)
            root.addLayout(bar)

            self.tabs = QTabWidget()
            self.tabs.setStyleSheet(
                f"QTabWidget::pane {{ border:none; background:transparent; }}\n"
                f"QTabBar::tab {{ background:rgba(255,255,255,0.04); color:{MUTED}; padding:8px 14px;"
                f" border:none; border-radius:9px; margin-right:4px; font-size:12px; }}\n"
                f"QTabBar::tab:selected {{ background:{ACCENT}; color:#06231d; font-weight:700; }}")
            root.addWidget(self.tabs)
            self._build_bypass_tab()
            self._build_speed_tab()
            self._build_download_tab()
            self._build_settings_tab()

            foot = QLabel("Runs elevated · TTL persists until restored")
            foot.setStyleSheet(f"color:{MUTED}; font-size:10px;")
            foot.setAlignment(Qt.AlignCenter)
            root.addWidget(foot)

            self._refresh()
            # auto-check update
            if cfg.get("check_updates", True):
                QTimer.singleShot(4000, self._auto_check_update)
            # watchdog timer
            self._watchdog = QTimer(self)
            self._watchdog.timeout.connect(self._watchdog_tick)
            self._watchdog.start(5000)
            # hotspot monitor
            self._hotspot_timer = QTimer(self)
            self._hotspot_timer.timeout.connect(self._hotspot_tick)
            self._hotspot_timer.start(10000)

        def _build_bypass_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            v.setContentsMargins(4, 10, 4, 4)
            v.setSpacing(8)

            card, cv = _card(page, "STATUS")
            self.hl_label = QLabel("Hop limit: —")
            self.hl_label.setStyleSheet(f"color:{TEXT}; font-size:20px; font-weight:700;")
            cv.addWidget(self.hl_label)
            self.conn_label = QLabel("Connection: —")
            self.conn_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv.addWidget(self.conn_label)
            self.ssid_label = QLabel("Wi-Fi: —")
            self.ssid_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv.addWidget(self.ssid_label)
            self.state_label = QLabel("—")
            self.state_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv.addWidget(self.state_label)
            v.addWidget(card)

            self.toggle = QPushButton("ENABLE BYPASS")
            self.toggle.setFixedHeight(52)
            self._set_toggle_style(active=False)
            self.toggle.clicked.connect(self._on_toggle)
            v.addWidget(self.toggle)

            self.bypass_test_btn = QPushButton("⚡ BYPASS + TEST  (before → after)")
            self.bypass_test_btn.setFixedHeight(40)
            self.bypass_test_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.08); color:{TEXT}; border:1px solid {ACCENT};"
                f" border-radius:10px; font-size:13px; font-weight:700; }}\n"
                f"QPushButton:hover {{ background:rgba(0,212,170,0.15); }}")
            self.bypass_test_btn.clicked.connect(self._on_bypass_test)
            v.addWidget(self.bypass_test_btn)

            self.restore = QPushButton("Restore default (128)")
            self.restore.setFixedHeight(36)
            self.restore.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.06); color:{MUTED}; border:none;"
                f" border-radius:10px; font-size:12px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.10); color:{TEXT}; }}")
            self.restore.clicked.connect(self._on_restore)
            v.addWidget(self.restore)

            card2, cv2 = _card(page, "AUTOMATION")
            self.auto_bypass = QCheckBox("Keep bypass on (watchdog re-applies if reset)")
            self.auto_bypass.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.auto_bypass.setChecked(bool(cfg.get("auto_bypass")))
            self.auto_bypass.toggled.connect(self._on_auto_bypass)
            cv2.addWidget(self.auto_bypass)

            self.hotspot_auto = QCheckBox("Auto-enable on phone hotspot")
            self.hotspot_auto.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.hotspot_auto.setChecked(bool(cfg.get("hotspot_auto")))
            self.hotspot_auto.toggled.connect(self._on_hotspot_auto)
            cv2.addWidget(self.hotspot_auto)
            v.addWidget(card2)

            v.addStretch()
            self.tabs.addTab(page, "Bypass")

        def _build_speed_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            v.setContentsMargins(4, 10, 4, 4)
            v.setSpacing(8)

            card, cv = _card(page, "SPEED TEST")
            row = QHBoxLayout()
            self.speed_val = QLabel("— Mbps")
            self.speed_val.setStyleSheet(f"color:{TEXT}; font-size:22px; font-weight:700;")
            row.addWidget(self.speed_val); row.addStretch()
            self.speed_btn = QPushButton("Run test")
            self.speed_btn.setFixedHeight(32)
            self.speed_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:#06231d; border:none; border-radius:9px;"
                f" font-size:12px; font-weight:700; padding:0 14px; }}\n"
                f"QPushButton:hover {{ background:#00e8bb; }}")
            self.speed_btn.clicked.connect(self._on_speed)
            row.addWidget(self.speed_btn)
            cv.addLayout(row)
            self.speed_state = QLabel("green = bypass on · gray = off")
            self.speed_state.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            cv.addWidget(self.speed_state)
            v.addWidget(card)

            card2, cv2 = _card(page, "HISTORY")
            self.graph = TrendGraph()
            cv2.addWidget(self.graph)
            self.hist_clear = QPushButton("Clear history")
            self.hist_clear.setFixedHeight(30)
            self.hist_clear.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.06); color:{MUTED}; border:none;"
                f" border-radius:8px; font-size:11px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.10); color:{TEXT}; }}")
            self.hist_clear.clicked.connect(self._clear_history)
            cv2.addWidget(self.hist_clear)
            v.addWidget(card2)

            v.addStretch()
            self.tabs.addTab(page, "Speed")

        def _build_download_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            v.setContentsMargins(4, 10, 4, 4)
            v.setSpacing(8)

            card, cv = _card(page, "DOWNLOAD QUEUE  (parallel · resume)")
            self.url = QLineEdit()
            self.url.setPlaceholderText("Paste URL (HuggingFace resolve link, etc.)")
            self.url.setStyleSheet(
                f"QLineEdit {{ background:rgba(0,0,0,0.3); color:{TEXT}; border:1px solid rgba(255,255,255,0.1);"
                f" border-radius:9px; padding:9px; font-size:12px; }}\n"
                f"QLineEdit:focus {{ border:1px solid {ACCENT}; }}")
            cv.addWidget(self.url)
            row = QHBoxLayout()
            add_btn = QPushButton("Add to queue")
            add_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:#06231d; border:none; border-radius:9px;"
                f" font-size:12px; font-weight:700; padding:8px 12px; }}\n"
                f"QPushButton:hover {{ background:#00e8bb; }}")
            add_btn.clicked.connect(self._add_to_queue)
            row.addWidget(add_btn)
            self.dl_btn = QPushButton("▶ Download all")
            self.dl_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:#06231d; border:none; border-radius:9px;"
                f" font-size:12px; font-weight:700; padding:8px 12px; }}\n"
                f"QPushButton:hover {{ background:#00e8bb; }}")
            self.dl_btn.clicked.connect(self._on_download_all)
            row.addWidget(self.dl_btn)
            cv.addLayout(row)

            self.queue = QListWidget()
            self.queue.setStyleSheet(
                f"QListWidget {{ background:rgba(0,0,0,0.3); color:{TEXT}; border:1px solid rgba(255,255,255,0.08);"
                f" border-radius:9px; font-size:11px; padding:4px; }}\n"
                f"QListWidget::item {{ padding:6px; border-bottom:1px solid rgba(255,255,255,0.04); }}\n"
                f"QListWidget::item:selected {{ background:rgba(0,212,170,0.15); }}")
            self.queue.setMinimumHeight(150)
            cv.addWidget(self.queue)

            row2 = QHBoxLayout()
            rm_btn = QPushButton("Remove selected")
            rm_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.06); color:{MUTED}; border:none;"
                f" border-radius:8px; font-size:11px; padding:6px 10px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.10); color:{TEXT}; }}")
            rm_btn.clicked.connect(self._remove_selected)
            row2.addWidget(rm_btn)
            clr_btn = QPushButton("Clear all")
            clr_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.06); color:{MUTED}; border:none;"
                f" border-radius:8px; font-size:11px; padding:6px 10px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.10); color:{TEXT}; }}")
            clr_btn.clicked.connect(self._clear_queue)
            row2.addWidget(clr_btn)
            row2.addStretch()
            cv.addLayout(row2)

            self.pbar = QProgressBar()
            self.pbar.setRange(0, 100)
            self.pbar.setTextVisible(True)
            self.pbar.setStyleSheet(
                f"QProgressBar {{ background:rgba(0,0,0,0.3); border:none; border-radius:6px;"
                f" color:{TEXT}; font-size:11px; height:18px; text-align:center; }}\n"
                f"QProgressBar::chunk {{ background:{ACCENT}; border-radius:6px; }}")
            cv.addWidget(self.pbar)
            self.dl_info = QLabel("Add URLs, then Download all.")
            self.dl_info.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            cv.addWidget(self.dl_info)
            v.addWidget(card)

            v.addStretch()
            self.tabs.addTab(page, "Downloads")

        def _build_settings_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            v.setContentsMargins(4, 10, 4, 4)
            v.setSpacing(8)

            card, cv = _card(page, "STARTUP")
            self.auto_start = QCheckBox("Start with Windows (minimized to tray)")
            self.auto_start.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.auto_start.setChecked(is_startup_enabled())
            self.auto_start.toggled.connect(self._on_auto_start)
            cv.addWidget(self.auto_start)
            v.addWidget(card)

            card2, cv2 = _card(page, "HOTSPOT SSIDs (auto-detect)")
            self.ssid_edit = QLineEdit()
            self.ssid_edit.setText(", ".join(cfg.get("hotspot_ssids", [])))
            self.ssid_edit.setPlaceholderText("comma-separated SSIDs that trigger auto-bypass")
            self.ssid_edit.setStyleSheet(
                f"QLineEdit {{ background:rgba(0,0,0,0.3); color:{TEXT}; border:1px solid rgba(255,255,255,0.1);"
                f" border-radius:9px; padding:8px; font-size:11px; }}\n"
                f"QLineEdit:focus {{ border:1px solid {ACCENT}; }}")
            cv2.addWidget(self.ssid_edit)
            self.ssid_save = QPushButton("Save SSIDs")
            self.ssid_save.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:#06231d; border:none; border-radius:9px;"
                f" font-size:12px; font-weight:700; padding:8px; }}\n"
                f"QPushButton:hover {{ background:#00e8bb; }}")
            self.ssid_save.clicked.connect(self._on_save_ssids)
            cv2.addWidget(self.ssid_save)
            v.addWidget(card2)

            card3, cv3 = _card(page, "UPDATES")
            self.update_label = QLabel(f"Current version: {VERSION}")
            self.update_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv3.addWidget(self.update_label)
            self.update_btn = QPushButton("Check for updates")
            self.update_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.08); color:{TEXT}; border:1px solid rgba(255,255,255,0.15);"
                f" border-radius:9px; font-size:12px; font-weight:600; padding:8px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.12); }}")
            self.update_btn.clicked.connect(self._on_check_update)
            cv3.addWidget(self.update_btn)
            self.auto_update = QCheckBox("Check for updates on launch")
            self.auto_update.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.auto_update.setChecked(bool(cfg.get("check_updates", True)))
            self.auto_update.toggled.connect(self._on_check_updates_toggle)
            cv3.addWidget(self.auto_update)
            v.addWidget(card3)

            card4, cv4 = _card(page, "ABOUT & LOGS")
            about = QLabel(
                "Defeats T-Mobile's hotspot cap via TTL/hop-limit fix.\n"
                "Only affects your own connection. Violates T-Mobile ToS.\n"
                f"Repo: github.com/{GITHUB_REPO}")
            about.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            about.setWordWrap(True)
            cv4.addWidget(about)
            self.log_btn = QPushButton("Open log file")
            self.log_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.08); color:{TEXT}; border:1px solid rgba(255,255,255,0.15);"
                f" border-radius:9px; font-size:12px; font-weight:600; padding:8px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.12); }}")
            self.log_btn.clicked.connect(self._open_log)
            cv4.addWidget(self.log_btn)
            v.addWidget(card4)

            v.addStretch()
            self.tabs.addTab(page, "Settings")

        # ---- styling helpers ----
        def _set_toggle_style(self, active):
            if active:
                self.toggle.setText("BYPASS ACTIVE — tap to disable")
                self.toggle.setStyleSheet(
                    f"QPushButton {{ background:{WARN}; color:#2a1a00; border:none; border-radius:12px;"
                    f" font-size:15px; font-weight:800; letter-spacing:1px; }}")
            else:
                self.toggle.setText("ENABLE BYPASS")
                self.toggle.setStyleSheet(
                    f"QPushButton {{ background:{ACCENT}; color:#06231d; border:none; border-radius:12px;"
                    f" font-size:15px; font-weight:800; letter-spacing:1px; }}\n"
                    f"QPushButton:hover {{ background:#00e8bb; }}")

        # ---- window events ----
        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

        def mouseMoveEvent(self, e):
            if self._drag and e.buttons() & Qt.LeftButton:
                self.move(e.globalPosition().toPoint() - self._drag)

        def mouseReleaseEvent(self, e):
            self._drag = None

        def showEvent(self, e):
            super().showEvent(e)
            _acrylic(self.winId())

        def closeEvent(self, e):
            if self._quitting:
                for w in (self._speed_worker, self._dl_worker, self._queue_worker, self._update_worker):
                    if w is not None and w.isRunning():
                        w.wait(3000)
                super().closeEvent(e)
            else:
                e.ignore()
                self.hide()
                self.tray.showMessage(
                    "T-Mobile Bypass",
                    "Still running in the tray. Right-click the icon to quit.",
                    QSystemTrayIcon.Information, 2500)

        # ---- refresh / watchdog / hotspot ----
        def _refresh(self):
            hl = get_hoplimit()
            log(f"refresh: hoplimit={hl} admin={is_admin()}")
            if hl is None:
                self.hl_label.setText("Hop limit: unknown")
                self.state_label.setText("⚠ couldn't read netsh (not admin?)")
                self.state_label.setStyleSheet(f"color:{WARN}; font-size:12px;")
            elif hl == BYPASS_TTL:
                self.hl_label.setText(f"Hop limit: {hl}")
                self.hl_label.setStyleSheet(f"color:{ACCENT}; font-size:20px; font-weight:700;")
                self.state_label.setText("✓ Bypass active — tethered traffic looks phone-native")
                self.state_label.setStyleSheet(f"color:{ACCENT}; font-size:12px;")
                self._set_toggle_style(active=True)
            else:
                self.hl_label.setText(f"Hop limit: {hl}")
                self.hl_label.setStyleSheet(f"color:{TEXT}; font-size:20px; font-weight:700;")
                self.state_label.setText("— bypass off (carrier sees tethered TTL)")
                self.state_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
                self._set_toggle_style(active=False)
            host, ip = detect_connection()
            self.conn_label.setText(f"Connection: {host} ({ip})")
            ssid = get_active_ssid()
            self.ssid_label.setText(f"Wi-Fi: {ssid or '—'}")

        def _watchdog_tick(self):
            if cfg.get("auto_bypass"):
                hl = get_hoplimit()
                if hl is not None and hl != BYPASS_TTL:
                    log(f"watchdog: hoplimit drifted to {hl}, re-applying {BYPASS_TTL}")
                    set_hoplimit(BYPASS_TTL)
                    self._refresh()

        def _hotspot_tick(self):
            if cfg.get("hotspot_auto"):
                ssid = get_active_ssid()
                if is_hotspot_ssid(ssid, cfg.get("hotspot_ssids", [])):
                    hl = get_hoplimit()
                    if hl != BYPASS_TTL:
                        log(f"hotspot detected ({ssid}), enabling bypass")
                        set_hoplimit(BYPASS_TTL)
                        self._refresh()
                        self.tray.showMessage(
                            "T-Mobile Bypass", f"Hotspot detected ({ssid}) — bypass enabled.",
                            QSystemTrayIcon.Information, 3000)

        # ---- bypass actions ----
        def _on_toggle(self):
            hl = get_hoplimit()
            if hl == BYPASS_TTL:
                return self._on_restore()
            ok, detail = set_hoplimit(BYPASS_TTL)
            log(f"enable bypass: ok={ok} detail={detail!r}")
            if ok:
                self._set_toggle_style(active=True)
            else:
                self._set_toggle_style(active=False)
                self.state_label.setText(f"✗ failed: {detail[:80]}")
                self.state_label.setStyleSheet(f"color:{BAD}; font-size:12px;")
            self._refresh()

        def _on_restore(self):
            ok, detail = set_hoplimit(DEFAULT_TTL)
            log(f"restore: ok={ok} detail={detail!r}")
            self._set_toggle_style(active=False)
            self._refresh()

        def _on_auto_bypass(self, checked):
            cfg["auto_bypass"] = bool(checked)
            _save_config(cfg)
            if checked:
                ok, _ = set_hoplimit(BYPASS_TTL)
                log(f"auto_bypass enabled; apply now ok={ok}")
                self._refresh()

        def _on_hotspot_auto(self, checked):
            cfg["hotspot_auto"] = bool(checked)
            _save_config(cfg)

        def _on_save_ssids(self):
            parts = [s.strip() for s in self.ssid_edit.text().split(",") if s.strip()]
            cfg["hotspot_ssids"] = parts
            _save_config(cfg)
            self.ssid_save.setText("Saved ✓")

        def _on_auto_start(self, checked):
            ok = set_startup_enabled(bool(checked))
            log(f"auto_start set to {checked}: ok={ok}")
            if not ok:
                self.auto_start.setChecked(False)

        def _on_check_updates_toggle(self, checked):
            cfg["check_updates"] = bool(checked)
            _save_config(cfg)

        def _open_log(self):
            path = log_path()
            if not os.path.exists(path):
                QMessageBox.information(self, "Log", "No log file yet.")
                return
            if WIN:
                try:
                    os.startfile(path)
                except Exception as e:
                    QMessageBox.warning(self, "Log", f"Couldn't open log:\n{e}")
            else:
                QMessageBox.information(self, "Log", path)

        # ---- speed test ----
        def _on_speed(self):
            self.speed_btn.setEnabled(False)
            self.speed_val.setText("testing…")
            log("speed test started")

            def done(res):
                self.speed_btn.setEnabled(True)
                if res[0] == "ok":
                    mbps, err = res[1]
                    log(f"speed test result: {mbps} err={err}")
                    if mbps < 0:
                        self.speed_val.setText(f"✗ {err[:40]}")
                    else:
                        self.speed_val.setText(f"{mbps:.1f} Mbps")
                        ttl = get_hoplimit() or 0
                        hist = _append_history({"t": time.time(), "mbps": round(mbps, 1), "ttl": ttl})
                        self.graph.set_points([(h["mbps"], h["ttl"]) for h in hist])
                else:
                    log(f"speed test error: {res[1]}")
                    self.speed_val.setText(f"✗ {res[1][:40]}")

            self._speed_worker = Worker(speed_test)
            self._speed_worker.done.connect(done)
            self._speed_worker.start()

        def _clear_history(self):
            try:
                os.remove(_history_path())
            except Exception:
                pass
            self.graph.set_points([])

        # ---- bypass + test ----
        def _on_bypass_test(self):
            self.bypass_test_btn.setEnabled(False)
            self.speed_val.setText("before…")
            self._bypass_test = {"before": None}
            before_ttl = get_hoplimit()
            log(f"bypass+test: before ttl={before_ttl}")

            def after_before(res):
                mbps, _ = res[1] if res[0] == "ok" else (-1, res[1])
                self._bypass_test["before"] = mbps
                self.speed_val.setText("applying bypass…")
                ok, detail = set_hoplimit(BYPASS_TTL)
                log(f"bypass+test: apply ok={ok} detail={detail!r}")
                flush_dns()

                def after_test(res2):
                    self.bypass_test_btn.setEnabled(True)
                    after_mbps, err2 = res2[1] if res2[0] == "ok" else (-1, res2[1])
                    before = self._bypass_test["before"]
                    self._refresh()
                    if before is not None and before >= 0 and after_mbps >= 0:
                        self.speed_val.setText(f"{after_mbps:.1f} Mbps")
                        msg = (f"Before: {before:.1f} Mbps\nAfter:  {after_mbps:.1f} Mbps\n"
                               f"Speed-up: {after_mbps / max(before, 0.01):.1f}×")
                        ttl = get_hoplimit() or 0
                        hist = _append_history({"t": time.time(), "mbps": round(after_mbps, 1), "ttl": ttl})
                        self.graph.set_points([(h["mbps"], h["ttl"]) for h in hist])
                        QMessageBox.information(self, "Bypass + Test", msg)
                    else:
                        self.speed_val.setText(f"before={before} after={after_mbps}")

                self._speed_worker = Worker(speed_test)
                self._speed_worker.done.connect(after_test)
                self._speed_worker.start()

            self._speed_worker = Worker(speed_test)
            self._speed_worker.done.connect(after_before)
            self._speed_worker.start()

        # ---- download queue ----
        def _add_to_queue(self):
            url = self.url.text().strip()
            if not url:
                return
            self.queue.addItem(url)
            self.url.clear()

        def _remove_selected(self):
            for item in self.queue.selectedItems():
                self.queue.takeItem(self.queue.row(item))

        def _clear_queue(self):
            self.queue.clear()

        def _queue_items(self):
            items = []
            for i in range(self.queue.count()):
                url = self.queue.item(i).text().strip()
                if not url or url.startswith(("✓", "✗", "→")):
                    continue
                name = safe_filename(url.split("?")[0].split("/")[-1] or "download.bin")
                dest = os.path.join(os.path.expanduser("~"), "Downloads", name)
                items.append((url, dest))
            return items

        def _on_download_all(self):
            items = self._queue_items()
            if not items:
                self.dl_info.setText("Queue is empty")
                return
            self.dl_btn.setEnabled(False)
            self.dl_info.setText(f"Downloading {len(items)} item(s)…")
            self._queue_worker = QueueWorker(items, 12)
            self._queue_worker.item_progress.connect(self._on_queue_progress)
            self._queue_worker.item_result.connect(self._on_queue_result)
            self._queue_worker.all_done.connect(self._on_queue_done)
            self._queue_worker.start()

        def _on_queue_progress(self, idx, done, total, speed):
            pct = int(done / total * 100) if total else 0
            self.pbar.setValue(pct)
            self.dl_info.setText(f"#{idx + 1}: {human_bytes(done)} / {human_bytes(total)}  ·  {human_bytes(speed)}/s")

        def _on_queue_result(self, idx, res):
            if idx < self.queue.count():
                item = self.queue.item(idx)
                if res[0] == "ok":
                    item.setText(f"✓ {res[1]}")
                    item.setForeground(QColor(ACCENT))
                else:
                    item.setText(f"✗ {res[1][:80]}")
                    item.setForeground(QColor(BAD))

        def _on_queue_done(self):
            self.dl_btn.setEnabled(True)
            self.pbar.setValue(100)
            self.dl_info.setText("✓ Queue complete")

        # ---- self-update ----
        def _auto_check_update(self):
            self._check_update(silent=True)

        def _on_check_update(self):
            self._check_update(silent=False)

        def _check_update(self, silent=False):
            self.update_btn.setEnabled(False)
            self.update_label.setText("Checking…")

            def done(res):
                self.update_btn.setEnabled(True)
                if res[0] != "ok":
                    self.update_label.setText("Update check failed")
                    return
                latest, asset_url = res[1]
                if not latest:
                    self.update_label.setText(f"Current: {VERSION} — no release yet")
                    if not silent:
                        QMessageBox.information(self, "Updates", "No update available.")
                    return
                if _version_tuple(latest) > _version_tuple(VERSION):
                    self.update_label.setText(f"New version: {latest} (you're on {VERSION})")
                    if asset_url:
                        self._prompt_update(latest, asset_url)
                    elif not silent:
                        QMessageBox.information(self, "Updates", f"New version {latest} available (no EXE asset).")
                else:
                    self.update_label.setText(f"Current: {VERSION} (up to date)")
                    if not silent:
                        QMessageBox.information(self, "Updates", "You're on the latest version.")

            self._update_worker = Worker(check_latest_version)
            self._update_worker.done.connect(done)
            self._update_worker.start()

        def _prompt_update(self, latest, asset_url):
            if not getattr(sys, "frozen", False):
                QMessageBox.information(
                    self, "Update available",
                    f"Version {latest} is available. Run the app from the built .exe to auto-update, "
                    f"or pull the latest from GitHub.")
                return
            r = QMessageBox.question(
                self, "Update available",
                f"Version {latest} is available. Download and install now?",
                QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes:
                return
            self.update_label.setText(f"Downloading {latest}…")
            new_exe = os.path.join(tempfile.gettempdir(), "T-MobileBypass-new.exe")

            def dl_done(res):
                if res[0] == "ok" and res[1]:
                    self.update_label.setText("Installing…")
                    if install_update(new_exe):
                        self._quit()
                    else:
                        self.update_label.setText("Install failed — see log")
                else:
                    self.update_label.setText("Download failed")

            self._update_worker = Worker(download_update_asset, asset_url, new_exe)
            self._update_worker.done.connect(dl_done)
            self._update_worker.start()

    # ---- launch ----
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # keep running in tray
    win = MainWindow()
    if "--minimized" in sys.argv:
        win.hide()
    else:
        win.show()
    sys.exit(app.exec())


def main():
    build_ui()


if __name__ == "__main__":
    main()
