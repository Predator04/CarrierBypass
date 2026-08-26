#!/usr/bin/env python3
"""
T-Mobile Bypass — Windows utility
=================================
1. TTL/Hop-limit fix: makes tethered (hotspot) traffic look like phone-native
   traffic so T-Mobile's 600 kbps hotspot cap doesn't apply.
2. Parallel-chunk downloader: grabs large files (AI models, etc.) at full
   bandwidth using multiple simultaneous connections with resume support.
3. Diagnostics: current hop limit, speed test, connection info.

Run as Administrator (the .exe requests elevation via manifest; the .py
re-launches itself elevated). The TTL setting persists across reboots.

Logs: %APPDATA%\\T-MobileBypass\\tmobile_bypass.log  (crash dumps: CRASH.log)
"""

import os
import re
import sys
import time
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

WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if WIN else 0


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


def get_hoplimit():
    """Return current IPv4 default hop limit, or None if unavailable."""
    out = _run(["netsh", "int", "ipv4", "show", "glob"]).stdout
    # real netsh output is "Default Hop Limit                   : 128 hops"
    m = re.search(r"default\s+hop\s+limit\s*:?\s*(\d+)", out, re.I)
    return int(m.group(1)) if m else None


def set_hoplimit(value):
    """Set IPv4+IPv6 default hop limit. Returns (ok, detail)."""
    r1 = _run(["netsh", "int", "ipv4", "set", "glob", f"defaultcurhoplimit={value}"])
    r2 = _run(["netsh", "int", "ipv6", "set", "glob", f"defaultcurhoplimit={value}"])
    detail = (r1.stdout + r1.stderr + r2.stdout + r2.stderr).strip()
    bad = ("elevation", "denied", "failed", "requires", "access is denied")
    lowered = detail.lower()
    if r1.returncode != 0 or r2.returncode != 0 or any(w in lowered for w in bad):
        return False, detail
    if get_hoplimit() != value:
        return False, "hop limit did not change after set"
    return True, detail


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
        return
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
    except Exception as e:
        log(f"relaunch_as_admin failed: {e}")


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def _log_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "T-MobileBypass")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return tempfile.gettempdir()


def log(msg):
    try:
        path = os.path.join(_log_dir(), "tmobile_bypass.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def _install_excepthook():
    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        log("UNHANDLED EXCEPTION:\n" + msg)
        try:
            with open(os.path.join(_log_dir(), "CRASH.log"), "w", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook


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
        """Return True only if the server honours Range (206 + Content-Range)."""
        try:
            req = Request(self.url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
            with urlopen(req, timeout=30) as r:
                status = getattr(r, "status", None) or getattr(r, "code", None) or 200
                cr = r.headers.get("Content-Range")
                return int(status) == 206 and cr is not None
        except Exception:
            return False

    # ---- resume bitmap ----
    def _nblocks(self):
        return (self.size + self.BLOCK - 1) // self.BLOCK if self.size > 0 else 0

    def _load_bitmap(self):
        n = self._nblocks()
        bm = bytearray(n)
        if self.resume and os.path.exists(self.meta) and os.path.exists(self.part):
            try:
                data = open(self.meta, "rb").read()
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
            # each worker owns its own file handle -> no seek/write race
            with open(self.part, "r+b") as fh:
                while True:
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
                    self._mark(start, pos - 1)
            self._mark(start, end)

    def _parallel(self, progress_cb, cancel_check):
        n = self._nblocks()
        self._bitmap = self._load_bitmap()
        if not os.path.exists(self.part):
            self._bitmap = bytearray(n)

        # compute missing byte ranges from the bitmap
        missing = []
        i = 0
        while i < n:
            if self._bitmap[i]:
                i += 1
                continue
            s = i
            while i < n and not self._bitmap[i]:
                i += 1
            missing.append((s * self.BLOCK, min(i * self.BLOCK, self.size) - 1))

        if not missing:
            self._finalize()
            return

        self.done = self._completed_bytes()
        self._last_done = self.done
        self._start_time = time.time()
        self._last_time = time.time()

        # pre-allocate the full file once (before workers open their handles)
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
            for f in futs:
                f.result()  # re-raise DownloadError

        self._save_bitmap()
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
            shutil.move(self.part, self.dest)
        if os.path.exists(self.meta):
            try:
                os.remove(self.meta)
            except Exception:
                pass

    def download(self, progress_cb=None, cancel_check=None):
        self._head()
        self.supports_ranges = self._probe_ranges() if self.size > 0 else False
        if not self.supports_ranges and os.path.exists(self.part):
            os.remove(self.part)  # cannot resume a server that ignores ranges
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

    # elevate before touching Qt
    if is_admin() is False and WIN:
        log("not admin — relaunching elevated")
        relaunch_as_admin()
        sys.exit(0)

    try:
        from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                                       QLabel, QPushButton, QProgressBar, QLineEdit,
                                       QFrame)
        from PySide6.QtCore import Qt, QThread, Signal
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
        f.setStyleSheet("QFrame { background: rgba(255,255,255,0.06); border-radius: 14px; }")
        v = QVBoxLayout(f)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(6)
        t = QLabel(title)
        t.setStyleSheet(f"color:{MUTED}; font-size:11px; letter-spacing:1px; font-weight:600;")
        v.addWidget(t)
        return f, v

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
        progress = Signal(int, int, float)
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

    class MainWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("T-Mobile Bypass")
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setFixedSize(520, 640)
            self._drag = None
            self._speed_worker = None
            self._dl_worker = None
            self._build()
            log("app started")

        def _build(self):
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            bar = QHBoxLayout()
            title = QLabel("T-Mobile Bypass")
            title.setStyleSheet(f"color:{TEXT}; font-size:16px; font-weight:700;")
            sub = QLabel("hotspot cap killer  ·  fast downloader")
            sub.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            tv = QVBoxLayout(); tv.setSpacing(0)
            tv.addWidget(title); tv.addWidget(sub)
            bar.addLayout(tv); bar.addStretch()
            close = QPushButton("✕")
            close.setFixedSize(30, 30)
            close.setStyleSheet(
                f"QPushButton {{ color:{MUTED}; background:transparent; border:none; font-size:14px; }}"
                f"QPushButton:hover {{ color:{TEXT}; background:rgba(255,255,255,0.08); border-radius:8px; }}")
            close.clicked.connect(self.close)
            bar.addWidget(close)
            root.addLayout(bar)

            card, cv = _card(self, "STATUS")
            self.hl_label = QLabel("Hop limit: —")
            self.hl_label.setStyleSheet(f"color:{TEXT}; font-size:20px; font-weight:700;")
            cv.addWidget(self.hl_label)
            self.conn_label = QLabel("Connection: —")
            self.conn_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv.addWidget(self.conn_label)
            self.state_label = QLabel("—")
            self.state_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv.addWidget(self.state_label)
            root.addWidget(card)

            self.toggle = QPushButton("ENABLE BYPASS")
            self.toggle.setFixedHeight(52)
            self._set_toggle_style(active=False)
            self.toggle.clicked.connect(self._on_toggle)
            root.addWidget(self.toggle)

            self.restore = QPushButton("Restore default (128)")
            self.restore.setFixedHeight(36)
            self.restore.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.06); color:{MUTED}; border:none;"
                f" border-radius:10px; font-size:12px; }}"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.10); color:{TEXT}; }}")
            self.restore.clicked.connect(self._on_restore)
            root.addWidget(self.restore)

            card2, cv2 = _card(self, "SPEED TEST")
            row = QHBoxLayout()
            self.speed_val = QLabel("— Mbps")
            self.speed_val.setStyleSheet(f"color:{TEXT}; font-size:22px; font-weight:700;")
            row.addWidget(self.speed_val); row.addStretch()
            self.speed_btn = QPushButton("Run test")
            self.speed_btn.setFixedHeight(32)
            self.speed_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:#06231d; border:none; border-radius:9px;"
                f" font-size:12px; font-weight:700; padding:0 14px; }}"
                f"QPushButton:hover {{ background:#00e8bb; }}")
            self.speed_btn.clicked.connect(self._on_speed)
            row.addWidget(self.speed_btn)
            cv2.addLayout(row)
            root.addWidget(card2)

            card3, cv3 = _card(self, "FAST DOWNLOAD  (parallel · resume)")
            self.url = QLineEdit()
            self.url.setPlaceholderText("Paste URL (HuggingFace resolve link, etc.)")
            self.url.setStyleSheet(
                f"QLineEdit {{ background:rgba(0,0,0,0.3); color:{TEXT}; border:1px solid rgba(255,255,255,0.1);"
                f" border-radius:9px; padding:9px; font-size:12px; }}"
                f"QLineEdit:focus {{ border:1px solid {ACCENT}; }}")
            cv3.addWidget(self.url)
            self.dl_btn = QPushButton("Download")
            self.dl_btn.setFixedHeight(38)
            self.dl_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:#06231d; border:none; border-radius:10px;"
                f" font-size:13px; font-weight:700; }}"
                f"QPushButton:hover {{ background:#00e8bb; }}")
            self.dl_btn.clicked.connect(self._on_download)
            cv3.addWidget(self.dl_btn)
            self.pbar = QProgressBar()
            self.pbar.setRange(0, 100)
            self.pbar.setTextVisible(True)
            self.pbar.setStyleSheet(
                f"QProgressBar {{ background:rgba(0,0,0,0.3); border:none; border-radius:6px;"
                f" color:{TEXT}; font-size:11px; height:18px; text-align:center; }}"
                f"QProgressBar::chunk {{ background:{ACCENT}; border-radius:6px; }}")
            cv3.addWidget(self.pbar)
            self.dl_info = QLabel("")
            self.dl_info.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            cv3.addWidget(self.dl_info)
            root.addWidget(card3)

            root.addStretch()

            foot = QLabel("Runs elevated · TTL setting persists until restored")
            foot.setStyleSheet(f"color:{MUTED}; font-size:10px;")
            foot.setAlignment(Qt.AlignCenter)
            root.addWidget(foot)

            self._refresh()

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
                    f" font-size:15px; font-weight:800; letter-spacing:1px; }}"
                    f"QPushButton:hover {{ background:#00e8bb; }}")

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
            for w in (self._speed_worker, self._dl_worker):
                if w is not None and w.isRunning():
                    w.wait(3000)
            super().closeEvent(e)

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
            else:
                self.hl_label.setText(f"Hop limit: {hl}")
                self.hl_label.setStyleSheet(f"color:{TEXT}; font-size:20px; font-weight:700;")
                self.state_label.setText("— bypass off (carrier sees tethered TTL)")
                self.state_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            host, ip = detect_connection()
            self.conn_label.setText(f"Connection: {host} ({ip})")

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
                else:
                    log(f"speed test error: {res[1]}")
                    self.speed_val.setText(f"✗ {res[1][:40]}")

            self._speed_worker = Worker(speed_test)
            self._speed_worker.done.connect(done)
            self._speed_worker.start()

        def _on_download(self):
            url = self.url.text().strip()
            if not url:
                self.dl_info.setText("Paste a URL first")
                return
            name = safe_filename(url.split("?")[0].split("/")[-1] or "download.bin")
            dest = os.path.join(os.path.expanduser("~"), "Downloads", name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self.dl_btn.setEnabled(False)
            self.dl_info.setText(f"→ {dest}")
            log(f"download started: {url} -> {dest}")

            self._dl_worker = DownloadWorker(url, dest, 12)
            self._dl_worker.progress.connect(self._on_dl_progress)
            self._dl_worker.result.connect(self._on_dl_done)
            self._dl_worker.start()

        def _on_dl_progress(self, done, total, speed):
            pct = int(done / total * 100) if total else 0
            self.pbar.setValue(pct)
            self.dl_info.setText(f"{human_bytes(done)} / {human_bytes(total)}  ·  {human_bytes(speed)}/s")

        def _on_dl_done(self, res):
            self.dl_btn.setEnabled(True)
            if res[0] == "ok":
                self.pbar.setValue(100)
                self.dl_info.setText(f"✓ saved to {res[1]}")
                log(f"download ok: {res[1]}")
            else:
                self.dl_info.setText(f"✗ {res[1][:100]}")
                log(f"download failed: {res[1]}")

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


def main():
    build_ui()


if __name__ == "__main__":
    main()
