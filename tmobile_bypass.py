#!/usr/bin/env python3
"""
Carrier Bypass — Windows utility
=================================
1. TTL/Hop-limit fix: makes tethered (hotspot) traffic look like phone-native
   traffic so your carrier's hotspot cap doesn't apply.
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
10. Multi-carrier: auto-detect carrier via IP lookup, auto-detect hop count
    via tracert, per-carrier throttle verdicts, and optional Windows
    fingerprint hardening (NCSI beacons, metered Wi-Fi).

Run as Administrator (the .exe requests elevation via manifest; the .py
re-launches itself elevated). The TTL setting persists across reboots.

Logs: %APPDATA%\\CarrierBypass\\tmobile_bypass.log  (crash dumps: CRASH.log)
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
import datetime
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from urllib.error import URLError

# ----------------------------------------------------------------------------
# Core (no GUI deps)
# ----------------------------------------------------------------------------

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
BYPASS_TTL = 65          # legacy constant — kept for back-compat; live code uses bypass_ttl()
DEFAULT_TTL = 128        # Windows default hop limit

VERSION = "1.3.0"
GITHUB_REPO = "Predator04/CarrierBypass"
GITHUB_API = "https://api.github.com/repos/" + GITHUB_REPO

WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if WIN else 0

# ----------------------------------------------------------------------------
# Carrier profile table
# ----------------------------------------------------------------------------
# phone_ttl is 64 for every current profile (Android + iOS both ship 64) but
# the field is kept per-profile so a future carrier that differs can override.
CARRIERS = {
    "tmobile": {
        "name": "T-Mobile",
        "asn": ["AS21928"],
        "match": ["t-mobile", "tmobile", "tmus"],
        "phone_ttl": 64,
        "throttle_kbps": 600,
        "typical_allotment_gb": [15, 50],
        "notes": "Hotspot drops to 3G-ish ~600 kbps after allotment.",
    },
    "att": {
        "name": "AT&T",
        "asn": ["AS20057"],
        "match": ["at&t", "att mobility", "at t mobility", "cingular"],
        "phone_ttl": 64,
        "throttle_kbps": 128,
        "typical_allotment_gb": [5, 60],
        "notes": "Hotspot drops to 128 kbps after allotment.",
    },
    "verizon": {
        "name": "Verizon",
        "asn": ["AS6167"],
        "match": ["verizon", "cellco"],
        "phone_ttl": 64,
        "throttle_kbps": 600,
        "typical_allotment_gb": [5, 60],
        "notes": "600 kbps after cap (3 Mbps on 5G UW plans).",
    },
    "visible": {
        "name": "Visible (Verizon MVNO)",
        "asn": ["AS6167"],
        "match": ["visible"],
        "phone_ttl": 64,
        "throttle_kbps": 5000,
        "typical_allotment_gb": [],
        "notes": "5 Mbps baseline cap, unlimited-ish allotment.",
    },
    "metro": {
        "name": "Metro by T-Mobile",
        "asn": ["AS21928"],
        "match": ["metro", "metropcs"],
        "phone_ttl": 64,
        "throttle_kbps": 600,
        "typical_allotment_gb": [5, 15],
        "notes": "600 kbps after allotment.",
    },
    "cricket": {
        "name": "Cricket (AT&T MVNO)",
        "asn": ["AS20057"],
        "match": ["cricket"],
        "phone_ttl": 64,
        "throttle_kbps": 128,
        "typical_allotment_gb": [15],
        "notes": "128 kbps after allotment.",
    },
    "mint": {
        "name": "Mint Mobile (T-Mobile MVNO)",
        "asn": ["AS21928"],
        "match": ["mint mobile", "mint"],
        "phone_ttl": 64,
        "throttle_kbps": 128,
        "typical_allotment_gb": [5, 10],
        "notes": "128 kbps after allotment.",
    },
    "usmobile": {
        "name": "US Mobile",
        "asn": ["AS21928", "AS6167"],
        "match": ["us mobile", "usmobile"],
        "phone_ttl": 64,
        "throttle_kbps": 600,
        "typical_allotment_gb": [10, 50],
        "notes": "600 kbps after allotment.",
    },
    "googlefi": {
        "name": "Google Fi",
        "asn": ["AS21928"],
        "match": ["google fi", "google-fi", "googlefi", "project fi"],
        "phone_ttl": 64,
        "throttle_kbps": 256,
        "typical_allotment_gb": [5, 50],
        "notes": "256 kbps after allotment.",
    },
    "straighttalk": {
        "name": "Straight Talk / TracFone",
        "asn": ["AS21928", "AS6167"],
        "match": ["straight talk", "straighttalk", "tracfone"],
        "phone_ttl": 64,
        "throttle_kbps": 128,
        "typical_allotment_gb": [10],
        "notes": "128 kbps after allotment.",
    },
    "boost": {
        "name": "Boost Mobile",
        "asn": ["AS6167"],
        "match": ["boost mobile", "boost"],
        "phone_ttl": 64,
        "throttle_kbps": 512,
        "typical_allotment_gb": [12, 30],
        "notes": "512 kbps after allotment.",
    },
    "other": {
        "name": "Other / Unknown",
        "asn": [],
        "match": [],
        "phone_ttl": 64,
        "throttle_kbps": 600,
        "typical_allotment_gb": [],
        "notes": "Unknown carrier — assuming 600 kbps throttle.",
    },
}


def carrier_profile(carrier_id):
    """Return the carrier profile dict, falling back to 'other' if unknown."""
    return CARRIERS.get(carrier_id) or CARRIERS["other"]


# In-memory cache for detect_carrier() — key is None, value is (timestamp, result).
_carrier_cache = {"t": 0.0, "result": None}


def detect_carrier(timeout=6):
    """Return (carrier_id, detail_string, error_or_None). Never raises.

    Uses ip-api.com first, falls back to ipinfo.io. Result cached 60 s.
    """
    now = time.time()
    if _carrier_cache["result"] and (now - _carrier_cache["t"]) < 60:
        return _carrier_cache["result"]

    isp = org = as_str = query = ""
    err = None
    try:
        req = Request("http://ip-api.com/json/?fields=status,isp,org,as,query",
                      headers={"User-Agent": UA})
        with urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        if (data.get("status") or "").lower() == "success":
            isp = data.get("isp") or ""
            org = data.get("org") or ""
            as_str = data.get("as") or ""
            query = data.get("query") or ""
        else:
            err = "ip-api returned non-success"
    except Exception as e:
        err = f"ip-api failed: {e}"

    if not (isp or org or as_str):
        try:
            req2 = Request("https://ipinfo.io/json", headers={"User-Agent": UA})
            with urlopen(req2, timeout=timeout) as r2:
                d2 = json.load(r2)
            org = d2.get("org") or org
            query = d2.get("ip") or query
            err = None
        except Exception as e:
            if not err:
                err = f"ipinfo failed: {e}"

    if not (isp or org or as_str):
        result = ("other", "", err or "no carrier data")
        _carrier_cache.update(t=now, result=result)
        return result

    haystack = " ".join([isp, org, as_str]).lower()
    matched_id = None
    for cid, prof in CARRIERS.items():
        if cid == "other":
            continue
        if any(m.lower() in haystack for m in prof.get("match", [])):
            matched_id = cid
            break
    if not matched_id:
        for cid, prof in CARRIERS.items():
            if cid == "other":
                continue
            for asn in prof.get("asn", []):
                if asn.lower() in haystack:
                    matched_id = cid
                    break
            if matched_id:
                break
    if not matched_id:
        matched_id = "other"

    prof = CARRIERS[matched_id]
    asn_hint = ""
    m = re.search(r"AS\d+", as_str, re.I)
    if m:
        asn_hint = m.group(0).upper()
    elif prof.get("asn"):
        asn_hint = prof["asn"][0]
    ip_hint = ""
    if query:
        parts = query.split(".")
        if len(parts) == 4:
            ip_hint = f"{parts[0]}.{parts[1]}.x.x"
        else:
            ip_hint = query
    detail_bits = [prof["name"]]
    if asn_hint:
        detail_bits.append(f"({asn_hint})")
    if ip_hint:
        detail_bits.append(f"· {ip_hint}")
    detail = " ".join(detail_bits)

    result = (matched_id, detail, None)
    _carrier_cache.update(t=now, result=result)
    return result


# ----------------------------------------------------------------------------
# Hop-count auto-detect
# ----------------------------------------------------------------------------

def _is_private_ipv4(ip):
    """RFC1918 private: 10/8, 172.16/12, 192.168/16."""
    try:
        parts = [int(x) for x in ip.split(".")]
        if len(parts) != 4:
            return False
        a, b = parts[0], parts[1]
        if a == 10:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        if a == 192 and b == 168:
            return True
        return False
    except Exception:
        return False


def _is_cgnat_ipv4(ip):
    """CGNAT range 100.64.0.0/10 — carrier-side, stop counting there."""
    try:
        parts = [int(x) for x in ip.split(".")]
        if len(parts) != 4:
            return False
        return parts[0] == 100 and 64 <= parts[1] <= 127
    except Exception:
        return False


def detect_hop_count():
    """Return (hops, recommended_ttl, hop_ip_list). Never raises.

    hops counts the leading RFC1918 hops between the laptop and the carrier.
    CGNAT (100.64/10) is treated as the carrier side and stops the count.
    Clamped to 1..4. Falls back to 1 hop on parse failure.
    """
    hop_ips = []
    if not WIN:
        return (1, CARRIERS["other"]["phone_ttl"] + 1, hop_ips)
    try:
        r = _run(["tracert", "-d", "-h", "4", "-w", "400", "1.1.1.1"])
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^\s*\d+\s+.*?(\d{1,3}(?:\.\d{1,3}){3})\s*$", line)
            if not m:
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
                if not m or not re.match(r"^\s*\d+\s", line):
                    continue
            hop_ips.append(m.group(1))
    except Exception as e:
        log(f"detect_hop_count tracert failed: {e}")

    hops = 0
    for ip in hop_ips:
        if _is_cgnat_ipv4(ip):
            break
        if _is_private_ipv4(ip):
            hops += 1
        else:
            break
    if hops < 1:
        hops = 1
    if hops > 4:
        hops = 4
    ttl = CARRIERS["other"]["phone_ttl"] + hops
    return (hops, ttl, hop_ips)


def bypass_ttl(cfg=None):
    """Return the TTL to apply for the current carrier + hop-count config.

    Precedence: cfg["custom_ttl"] (32..255, nonzero) → carrier phone_ttl + hop_count.
    Safe to call with cfg=None (uses defaults).
    """
    if cfg is None:
        cfg = {}
    custom = cfg.get("custom_ttl") or 0
    try:
        custom = int(custom)
    except Exception:
        custom = 0
    if 32 <= custom <= 255:
        return custom
    cid = cfg.get("carrier") or "other"
    if cid == "auto":
        # never block the caller on a network round-trip — only use the
        # already-cached detect_carrier result, otherwise fall back to "other"
        cached = _carrier_cache.get("result")
        cid = cached[0] if cached else "other"
    prof = carrier_profile(cid)
    hops = cfg.get("hop_count") or 1
    try:
        hops = int(hops)
    except Exception:
        hops = 1
    hops = max(1, min(4, hops))
    return int(prof["phone_ttl"]) + hops


ABS_CLEAR_MBPS = 3.0   # nothing under this is "full speed" on modern LTE/5G


def throttle_verdict(mbps, carrier_id):
    """Classify a speed-test result vs the carrier's throttle floor.

    Returns (state, message) where state ∈ {"capped", "suspect", "clear"}.
    """
    prof = carrier_profile(carrier_id)
    throttle_kbps = int(prof.get("throttle_kbps") or 600)
    throttle_mbps = throttle_kbps / 1000.0
    name = prof.get("name") or "your carrier"
    if mbps is None or mbps < 0:
        return ("suspect", "Speed test unavailable")
    low = throttle_mbps * 0.6
    high = throttle_mbps * 1.4
    if low <= mbps <= high:
        return ("capped", f"You're sitting on {name}'s {throttle_kbps} kbps hotspot cap")
    if mbps < throttle_mbps * 1.6:
        return ("suspect", f"Below 1.6× the {name} throttle floor — bypass may be leaking")
    if mbps < ABS_CLEAR_MBPS:
        # Carriers with a very low floor (AT&T 128 kbps) would otherwise call
        # 0.6 Mbps "full speed". Nothing under a few Mbps is healthy on LTE/5G.
        return ("suspect",
                f"{mbps:.1f} Mbps clears {name}'s throttle floor but is still slow "
                f"for LTE/5G — check signal, or you may be deprioritized (TTL won't help that)")
    return ("clear", "Full-speed — bypass is holding")


# ----------------------------------------------------------------------------
# Hardening: NCSI beacons + metered Wi-Fi (Windows registry, fail-soft)
# ----------------------------------------------------------------------------

def _reg_read_dword(root_name, subkey, value_name):
    if not WIN:
        return None
    try:
        import winreg
        root = getattr(winreg, root_name)
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, value_name)
            return int(v)
    except FileNotFoundError:
        return None
    except Exception as e:
        log(f"_reg_read_dword {root_name}\\{subkey}\\{value_name} failed: {e}")
        return None


def _reg_write_dword(root_name, subkey, value_name, value):
    if not WIN:
        return (False, "not windows")
    try:
        import winreg
        root = getattr(winreg, root_name)
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, value_name, 0, winreg.REG_DWORD, int(value))
        return (True, "")
    except PermissionError as e:
        return (False, f"permission denied: {e}")
    except OSError as e:
        return (False, f"os error: {e}")
    except Exception as e:
        return (False, f"error: {e}")


_NCSI_KEY = (r"SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet",
             "EnableActiveProbing")
_METERED_KEY = (r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\DefaultMediaCost",
                "WiFi")


def apply_disable_ncsi(cfg):
    """Set EnableActiveProbing=0 to stop msftconnecttest.com beacons.

    Saves the previous value to cfg['ncsi_prev'] so restore is exact.
    Returns (ok, detail). Never raises.
    """
    prev = _reg_read_dword("HKEY_LOCAL_MACHINE", _NCSI_KEY[0], _NCSI_KEY[1])
    if prev is not None:
        cfg["ncsi_prev"] = prev
    ok, detail = _reg_write_dword("HKEY_LOCAL_MACHINE", _NCSI_KEY[0], _NCSI_KEY[1], 0)
    log(f"apply_disable_ncsi ok={ok} prev={prev} detail={detail!r}")
    return (ok, detail)


def restore_ncsi(cfg):
    """Restore EnableActiveProbing to its saved value (or 1 if none saved)."""
    prev = cfg.get("ncsi_prev")
    if prev is None:
        prev = 1
    ok, detail = _reg_write_dword("HKEY_LOCAL_MACHINE", _NCSI_KEY[0], _NCSI_KEY[1], int(prev))
    log(f"restore_ncsi ok={ok} to={prev} detail={detail!r}")
    return (ok, detail)


def apply_metered_wifi(cfg):
    """Mark Wi-Fi as metered (DefaultMediaCost\\WiFi = 2).

    This key is owned by TrustedInstaller and often blocks writes; return a
    clean (False, message) instead of raising.
    """
    prev = _reg_read_dword("HKEY_LOCAL_MACHINE", _METERED_KEY[0], _METERED_KEY[1])
    if prev is not None:
        cfg["metered_prev"] = prev
    ok, detail = _reg_write_dword("HKEY_LOCAL_MACHINE", _METERED_KEY[0], _METERED_KEY[1], 2)
    if not ok:
        detail = "Windows blocks writes to this key — skip or take ownership manually"
    log(f"apply_metered_wifi ok={ok} prev={prev} detail={detail!r}")
    return (ok, detail)


def restore_metered_wifi(cfg):
    """Restore Wi-Fi media cost to the saved value (or 1 = unmetered)."""
    prev = cfg.get("metered_prev")
    if prev is None:
        prev = 1
    ok, detail = _reg_write_dword("HKEY_LOCAL_MACHINE", _METERED_KEY[0], _METERED_KEY[1], int(prev))
    if not ok:
        detail = "Windows blocks writes to this key — skip or take ownership manually"
    log(f"restore_metered_wifi ok={ok} to={prev} detail={detail!r}")
    return (ok, detail)


# ----------------------------------------------------------------------------
# v1.3.0 — stack-masking helpers (TCP timestamps, MTU, window auto-tuning)
# ----------------------------------------------------------------------------

def _netsh_tcp_global():
    return _run(["netsh", "int", "tcp", "show", "global"]).stdout or ""


def _read_tcp_timestamps():
    """Return 'enabled' / 'disabled' / '' if unknown."""
    out = _netsh_tcp_global()
    m = re.search(r"Timestamps\s*:?\s*(\w+)", out, re.I)
    if m:
        return m.group(1).strip().lower()
    return ""


def _read_autotuning_level():
    out = _netsh_tcp_global()
    m = re.search(r"Receive\s+Window\s+Auto[-\s]?Tuning\s+Level\s*:?\s*(\w+)", out, re.I)
    if m:
        return m.group(1).strip().lower()
    return ""


def apply_disable_tcp_timestamps(cfg):
    """netsh int tcp set global timestamps=disabled. Saves previous to cfg['prev_timestamps']."""
    prev = _read_tcp_timestamps()
    if prev:
        cfg["prev_timestamps"] = prev
    r = _run(["netsh", "int", "tcp", "set", "global", "timestamps=disabled"])
    ok = r.returncode == 0
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    log(f"apply_disable_tcp_timestamps ok={ok} prev={prev} detail={detail[:120]!r}")
    return (ok, detail)


def restore_tcp_timestamps(cfg):
    # VERIFIED: on current Windows the default is `allowed`, not `enabled`
    # (netsh reports "RFC 1323 Timestamps : allowed"). Omitting it here meant
    # restore silently rewrote the machine's original value to `enabled`.
    prev = (cfg.get("prev_timestamps") or "").strip().lower()
    if prev not in ("enabled", "disabled", "allowed"):
        prev = "allowed"
    r = _run(["netsh", "int", "tcp", "set", "global", f"timestamps={prev}"])
    ok = r.returncode == 0
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    log(f"restore_tcp_timestamps ok={ok} to={prev} detail={detail[:120]!r}")
    return (ok, detail)


def _read_mtu(alias):
    """Read the current MTU for the given interface alias, or None."""
    if not alias:
        return None
    out = _run(["netsh", "interface", "ipv4", "show", "subinterfaces"]).stdout or ""
    for line in out.splitlines():
        parts = line.split()
        # columns: MTU  MediaSenseState  BytesIn  BytesOut  Interface
        if len(parts) >= 5 and parts[0].isdigit():
            iface = " ".join(parts[4:]).strip()
            if iface.lower() == alias.lower():
                try:
                    return int(parts[0])
                except Exception:
                    return None
    return None


def apply_cellular_mtu(cfg, alias=None, mtu=None):
    """Set interface MTU to look cellular. Saves previous MTU to cfg['prev_mtu']."""
    if alias is None:
        alias = wifi_interface_alias()
    if not alias:
        return (False, "no Wi-Fi adapter alias resolved")
    if mtu is None:
        mtu = int(cfg.get("cellular_mtu") or 1420)
    mtu = max(1280, min(1500, int(mtu)))
    prev = _read_mtu(alias)
    if prev is not None:
        cfg["prev_mtu"] = int(prev)
    r = _run(["netsh", "interface", "ipv4", "set", "subinterface", alias,
              f"mtu={mtu}", "store=persistent"])
    ok = r.returncode == 0
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    log(f"apply_cellular_mtu alias={alias!r} mtu={mtu} prev={prev} ok={ok} detail={detail[:120]!r}")
    return (ok, detail)


def restore_mtu(cfg, alias=None):
    if alias is None:
        alias = wifi_interface_alias()
    if not alias:
        return (False, "no Wi-Fi adapter alias resolved")
    prev = int(cfg.get("prev_mtu") or 1500)
    if not (1280 <= prev <= 1500):
        prev = 1500
    r = _run(["netsh", "interface", "ipv4", "set", "subinterface", alias,
              f"mtu={prev}", "store=persistent"])
    ok = r.returncode == 0
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    log(f"restore_mtu alias={alias!r} to={prev} ok={ok} detail={detail[:120]!r}")
    return (ok, detail)


def apply_autotuning_restricted(cfg):
    """netsh int tcp set global autotuninglevel=restricted. Reduces receive window,
    can cut throughput on high-latency links — keep OFF by default and never
    include in an 'apply all'.
    """
    prev = _read_autotuning_level()
    if prev:
        cfg["prev_autotuning"] = prev
    r = _run(["netsh", "int", "tcp", "set", "global", "autotuninglevel=restricted"])
    ok = r.returncode == 0
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    log(f"apply_autotuning_restricted ok={ok} prev={prev} detail={detail[:120]!r}")
    return (ok, detail)


def restore_autotuning(cfg):
    prev = cfg.get("prev_autotuning") or "normal"
    if prev not in ("disabled", "highlyrestricted", "restricted", "normal", "experimental"):
        prev = "normal"
    r = _run(["netsh", "int", "tcp", "set", "global", f"autotuninglevel={prev}"])
    ok = r.returncode == 0
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    log(f"restore_autotuning ok={ok} to={prev} detail={detail[:120]!r}")
    return (ok, detail)


# ----------------------------------------------------------------------------
# v1.3.0 — phone-resolver DNS (Feature 6)
# ----------------------------------------------------------------------------

def _read_dns_servers(alias):
    """Return the comma-joined static DNS servers for alias, or '' if DHCP/unknown."""
    if not alias:
        return ""
    out = _run(["netsh", "interface", "ipv4", "show", "dnsservers", f"name={alias}"]).stdout or ""
    ips = re.findall(r"(\d{1,3}(?:\.\d{1,3}){3})", out)
    if not ips:
        return ""
    if re.search(r"DHCP", out, re.I) and not re.search(r"Statically", out, re.I):
        return ""
    return ",".join(ips)


def set_dns_to_gateway(cfg):
    """Point the Wi-Fi adapter's DNS at the current gateway (mimics carrier DNS on phone).

    Saves the previous DNS setting to cfg['prev_dns'] so restore is exact.
    Returns (ok, detail). Never raises.
    """
    alias = wifi_interface_alias()
    if not alias:
        return (False, "no Wi-Fi adapter alias resolved")
    gw = get_gateway_ip()
    if not gw:
        return (False, "no gateway IP resolved")
    prev = _read_dns_servers(alias)
    if prev:
        cfg["prev_dns"] = prev
    else:
        cfg["prev_dns"] = ""  # was DHCP
    r = _run(["netsh", "interface", "ipv4", "set", "dnsservers",
              f"name={alias}", "static", gw, "primary"])
    ok = r.returncode == 0
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    log(f"set_dns_to_gateway alias={alias!r} gw={gw} prev={prev!r} ok={ok} detail={detail[:120]!r}")
    return (ok, detail)


def restore_dns_dhcp(cfg):
    """Restore the Wi-Fi adapter to DHCP DNS (or the saved previous static list)."""
    alias = wifi_interface_alias()
    if not alias:
        return (False, "no Wi-Fi adapter alias resolved")
    prev = (cfg.get("prev_dns") or "").strip()
    if prev:
        # restore explicit static list
        ips = [ip.strip() for ip in prev.split(",") if ip.strip()]
        if ips:
            r = _run(["netsh", "interface", "ipv4", "set", "dnsservers",
                      f"name={alias}", "static", ips[0], "primary"])
            ok = r.returncode == 0
            detail = ((r.stdout or "") + (r.stderr or "")).strip()
            for i, ip in enumerate(ips[1:], start=2):
                _run(["netsh", "interface", "ipv4", "add", "dnsservers",
                      f"name={alias}", ip, f"index={i}"])
            log(f"restore_dns_dhcp alias={alias!r} restored static={ips} ok={ok}")
            return (ok, detail)
    r = _run(["netsh", "interface", "ipv4", "set", "dnsservers",
              f"name={alias}", "dhcp"])
    ok = r.returncode == 0
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    log(f"restore_dns_dhcp alias={alias!r} → dhcp ok={ok} detail={detail[:120]!r}")
    return (ok, detail)


# ----------------------------------------------------------------------------
# v1.3.0 — Router rule export (Feature 5)
# ----------------------------------------------------------------------------

DEFAULT_ROUTER_INTERFACES = ["usb0", "usb1", "eth1", "eth2", "wwan0", "wlan0"]


def router_rules(ttl, interfaces=None):
    """Return a string of iptables/ip6tables mangle rules that rewrite the TTL/HL
    on the given router interfaces. Four rules per interface (PREROUTING +
    POSTROUTING, v4 + v6). Interfaces defaults to a common OpenWRT/GL.iNet set.
    """
    try:
        ttl = int(ttl)
    except Exception:
        ttl = 65
    ttl = max(1, min(255, ttl))
    if interfaces is None:
        interfaces = list(DEFAULT_ROUTER_INTERFACES)
    lines = []
    for iface in interfaces:
        iface = str(iface).strip()
        if not iface:
            continue
        lines.append(f"iptables  -t mangle -I PREROUTING  -i {iface} -j TTL --ttl-set {ttl}")
        lines.append(f"iptables  -t mangle -I POSTROUTING -o {iface} -j TTL --ttl-set {ttl}")
        lines.append(f"ip6tables -t mangle -I PREROUTING  -i {iface} -j HL  --hl-set  {ttl}")
        lines.append(f"ip6tables -t mangle -I POSTROUTING -o {iface} -j HL  --hl-set  {ttl}")
    return "\n".join(lines) + ("\n" if lines else "")


# ----------------------------------------------------------------------------
# v1.3.0 — Wi-Fi adapter alias + counters (Feature 2 helpers, also used by 3/6)
# ----------------------------------------------------------------------------

def wifi_interface_alias():
    """Return the Wi-Fi adapter's alias (e.g. 'Wi-Fi'), or None.

    Tries `netsh wlan show interfaces` first, falls back to Get-NetAdapter.
    """
    if not WIN:
        return None
    try:
        out = _run(["netsh", "wlan", "show", "interfaces"]).stdout or ""
        m = re.search(r"^\s*Name\s*:\s*(.+)$", out, re.M)
        if m:
            return m.group(1).strip()
    except Exception as e:
        log(f"wifi_interface_alias netsh wlan failed: {e}")
    try:
        r = _run(["powershell", "-NoProfile", "-Command",
                  "(Get-NetAdapter -Physical | Where-Object {$_.PhysicalMediaType -like '*802.11*' -and $_.Status -eq 'Up'} | Select-Object -First 1 -ExpandProperty Name)"])
        name = (r.stdout or "").strip()
        if name:
            return name.splitlines()[-1].strip()
    except Exception as e:
        log(f"wifi_interface_alias powershell failed: {e}")
    return None


def read_adapter_counters(alias):
    """Return (rx_bytes, tx_bytes) for the given adapter alias, or (None, None)."""
    if not WIN or not alias:
        return (None, None)
    try:
        cmd = ("Get-NetAdapterStatistics -Name '" + alias.replace("'", "''") +
               "' | Select-Object -ExpandProperty ReceivedBytes; "
               "Get-NetAdapterStatistics -Name '" + alias.replace("'", "''") +
               "' | Select-Object -ExpandProperty SentBytes")
        r = _run(["powershell", "-NoProfile", "-Command", cmd])
        parts = [p.strip() for p in (r.stdout or "").splitlines() if p.strip()]
        if len(parts) >= 2:
            try:
                return (int(parts[0]), int(parts[1]))
            except Exception:
                pass
        return (None, None)
    except Exception as e:
        log(f"read_adapter_counters({alias!r}) failed: {e}")
        return (None, None)


# ----------------------------------------------------------------------------
# v1.3.0 — Per-SSID usage tracking (Feature 2)
# ----------------------------------------------------------------------------

def _cycle_key(day, now=None):
    """Return YYYY-MM key for the billing cycle that contains `now`.

    day = billing start day (1..28). If today is before that day, we're still in
    the previous month's cycle.
    """
    if now is None:
        now = datetime.date.today()
    try:
        day = int(day)
    except Exception:
        day = 1
    day = max(1, min(28, day))
    y, m = now.year, now.month
    if now.day < day:
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return f"{y:04d}-{m:02d}"


def _load_usage():
    try:
        with open(_usage_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_usage(data):
    p = _usage_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def usage_add(ssid, cycle_start_day, rx_delta, tx_delta):
    """Add rx/tx deltas to the current SSID+cycle bucket. Returns updated dict."""
    if not ssid or (rx_delta <= 0 and tx_delta <= 0):
        return _load_usage()
    data = _load_usage()
    key = _cycle_key(cycle_start_day)
    bucket = data.setdefault(ssid, {}).setdefault(key, {"rx": 0, "tx": 0})
    bucket["rx"] = int(bucket.get("rx", 0)) + int(max(0, rx_delta))
    bucket["tx"] = int(bucket.get("tx", 0)) + int(max(0, tx_delta))
    _save_usage(data)
    return data


def usage_current(ssid, cycle_start_day):
    """Return (rx_bytes, tx_bytes) for this SSID's current cycle."""
    if not ssid:
        return (0, 0)
    data = _load_usage()
    key = _cycle_key(cycle_start_day)
    b = data.get(ssid, {}).get(key) or {}
    return (int(b.get("rx", 0)), int(b.get("tx", 0)))


def usage_reset(ssid, cycle_start_day):
    """Zero the current-cycle bucket for `ssid`."""
    if not ssid:
        return
    data = _load_usage()
    key = _cycle_key(cycle_start_day)
    if ssid in data and key in data[ssid]:
        data[ssid][key] = {"rx": 0, "tx": 0}
        _save_usage(data)


# ----------------------------------------------------------------------------
# v1.3.0 — Network signature (Feature 4)
# ----------------------------------------------------------------------------

def network_signature():
    """Return (ssid, gateway_ip, interface_alias). Any component may be None.

    Used to detect when the user has moved between phone-direct, travel-router,
    etc. — the hop count and target TTL usually need to change with it.
    """
    try:
        ssid = get_active_ssid()
    except Exception:
        ssid = None
    try:
        gw = get_gateway_ip()
    except Exception:
        gw = None
    try:
        alias = wifi_interface_alias()
    except Exception:
        alias = None
    return (ssid, gw, alias)


# ----------------------------------------------------------------------------
# v1.3.0 — Egress TTL verification (Feature 1)
# ----------------------------------------------------------------------------

def _pktmon_help():
    # VERIFIED: the correct form is `pktmon start help`. `--help` returns
    # "Unknown parameter '--help'", which would poison any flag sniffing.
    r = _run(["pktmon", "start", "help"])
    return ((r.stdout or "") + (r.stderr or "")).lower()


def _extract_ttl_from_pktmon_text(text):
    """Parse the OUTBOUND IPv4 TTL out of `pktmon etl2txt --verbose` output.

    Verified against a real capture on Windows build 26200. The decoded form is
    three lines per packet:

        [12]0004.0514::... [Microsoft-Windows-PktMon] PktGroupId ..., Direction Tx , ...
        \tAA-.. > BB-.., ethertype IPv4 (0x0800), length 66: (tos 0x0, ttl 128, id 46774, ...)
            192.168.1.8.14813 > 1.1.1.1.443: Flags [S], seq ..., win 65535, ...

    Two things make the naive parse wrong:
      1. The token is `ttl 128` — space separated, not `ttl=128`.
      2. Direction lives on the PRECEDING line. Inbound packets from the target
         carry the remote host's TTL (observed: 58). Grabbing the first TTL in
         the file yields a confident, completely wrong answer.
    Every packet is also logged once per component, so we take the most common
    Tx value rather than the first.
    """
    if not text:
        return None
    counts = {}
    direction_tx = False
    for line in text.splitlines():
        if "Direction" in line and "PktMon" in line:
            direction_tx = "Direction Tx" in line
            continue
        if not direction_tx:
            continue
        m = re.search(r"\bttl\s+(\d{1,3})\b", line)
        if not m:  # tolerate other decoder builds
            m = re.search(r"\bttl\s*[=:]\s*(\d{1,3})\b", line, re.I)
        if m:
            v = int(m.group(1))
            if 1 <= v <= 255:
                counts[v] = counts.get(v, 0) + 1
            direction_tx = False  # consume; next TTL needs a fresh direction line
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _pktmon_capture_ttl(dest_ip, timeout):
    """Try to capture and decode the outbound TTL to dest_ip. Returns int or None.

    Always cleans up filters and temp files.
    """
    if not WIN:
        return (None, "not windows")
    tmp_etl = os.path.join(tempfile.gettempdir(), "cbverify.etl")
    tmp_txt = os.path.join(tempfile.gettempdir(), "cbverify.txt")
    try:
        _run(["pktmon", "stop"])
        _run(["pktmon", "filter", "remove"])
        _run(["pktmon", "filter", "add", "CBVerify", "-i", dest_ip, "-t", "TCP"])
        # VERIFIED flag set for Windows 10 1809+ / 11. Fall back to the minimal
        # form if this build rejects --comp/--pkt-size, rather than sniffing help
        # text (which is fragile and was the original failure mode).
        start_cmd = ["pktmon", "start", "--capture", "--comp", "nics",
                     "--pkt-size", "128", "--file-name", tmp_etl]
        r = _run(start_cmd)
        if r.returncode != 0:
            start_cmd = ["pktmon", "start", "--capture", "--file-name", tmp_etl]
            r = _run(start_cmd)
        if r.returncode != 0:
            return (None, f"pktmon start failed: {((r.stdout or '') + (r.stderr or '')).strip()[:120]}")

        # Generate outbound traffic so pktmon has something to see.
        for _ in range(3):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                try:
                    s.connect((dest_ip, 443))
                except Exception:
                    pass
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.3)

        _run(["pktmon", "stop"])
        if not os.path.exists(tmp_etl):
            return (None, "pktmon did not produce an ETL file")
        r2 = _run(["pktmon", "etl2txt", tmp_etl, "--out", tmp_txt, "--verbose"])
        if r2.returncode != 0 or not os.path.exists(tmp_txt):
            return (None, f"pktmon etl2txt failed: {((r2.stdout or '') + (r2.stderr or '')).strip()[:120]}")
        try:
            # VERIFIED: pktmon etl2txt writes UTF-16LE **with a BOM**. Reading it
            # as UTF-8 yields zero matches and the whole feature silently reports
            # "unavailable". Sniff the BOM instead of assuming an encoding.
            with open(tmp_txt, "rb") as f:
                raw = f.read()
            if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                text = raw.decode("utf-16", errors="replace")
            elif raw[:3] == b"\xef\xbb\xbf":
                text = raw[3:].decode("utf-8", errors="replace")
            else:
                text = raw.decode("utf-8", errors="replace")
                if "ttl" not in text.lower() and b"t\x00t\x00l\x00" in raw:
                    text = raw.decode("utf-16-le", errors="replace")
        except Exception as e:
            return (None, f"could not read pktmon txt: {e}")
        ttl = _extract_ttl_from_pktmon_text(text)
        if ttl is None:
            return (None, "no TTL parsed from pktmon output")
        return (ttl, "ok")
    finally:
        try:
            _run(["pktmon", "stop"])
        except Exception:
            pass
        try:
            _run(["pktmon", "filter", "remove"])
        except Exception:
            pass
        for p in (tmp_etl, tmp_txt):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


def _probe_ttl_endpoint(url, timeout):
    """GET url and parse either a bare int body or a JSON {"ttl": N}."""
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", errors="replace").strip()
    except Exception as e:
        return (None, f"probe fetch failed: {e}")
    try:
        j = json.loads(body)
        if isinstance(j, dict) and "ttl" in j:
            v = int(j["ttl"])
            if 1 <= v <= 255:
                return (v, "ok")
    except Exception:
        pass
    m = re.search(r"\b(\d{1,3})\b", body)
    if m:
        try:
            v = int(m.group(1))
            if 1 <= v <= 255:
                return (v, "ok")
        except Exception:
            pass
    return (None, "probe body did not contain a TTL")


def verify_egress_ttl(dest_ip="1.1.1.1", timeout=12, cfg=None):
    """Try to observe the real outbound TTL as it leaves the NIC.

    Returns {"state": "verified"|"mismatch"|"unavailable",
             "observed_ttl": int|None, "expected_ttl": int,
             "method": "pktmon"|"probe"|None, "detail": "..."}

    Never reports 'verified' from configured registry values — the whole point
    is to distinguish configured from actual.
    """
    if cfg is None:
        try:
            cfg = _load_config()
        except Exception:
            cfg = {}
    expected = bypass_ttl(cfg)

    # 1. Try pktmon.
    if WIN:
        try:
            ttl, detail = _pktmon_capture_ttl(dest_ip, timeout)
        except Exception as e:
            ttl, detail = (None, f"pktmon threw: {e}")
        if ttl is not None:
            state = "verified" if ttl == expected else "mismatch"
            return {"state": state, "observed_ttl": int(ttl),
                    "expected_ttl": int(expected), "method": "pktmon",
                    "detail": f"pktmon observed TTL={ttl}, expected {expected}"}
        pktmon_detail = detail or "pktmon produced no TTL"
    else:
        pktmon_detail = "not windows"

    # 2. Fallback: custom probe endpoint.
    probe_url = (cfg.get("ttl_probe_url") or "").strip()
    if probe_url:
        try:
            ttl, detail = _probe_ttl_endpoint(probe_url, timeout)
        except Exception as e:
            ttl, detail = (None, f"probe threw: {e}")
        if ttl is not None:
            state = "verified" if ttl == expected else "mismatch"
            return {"state": state, "observed_ttl": int(ttl),
                    "expected_ttl": int(expected), "method": "probe",
                    "detail": f"probe observed TTL={ttl}, expected {expected}"}
        probe_detail = detail
    else:
        probe_detail = "no ttl_probe_url configured"

    return {"state": "unavailable", "observed_ttl": None,
            "expected_ttl": int(expected), "method": None,
            "detail": f"could not verify: pktmon: {pktmon_detail}; probe: {probe_detail}"}


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
# Config + history (JSON under %APPDATA%\CarrierBypass, with one-time migration
# from %APPDATA%\T-MobileBypass on first run of v1.3.0)
# ----------------------------------------------------------------------------

_MIGRATION_DONE = False


def _data_dir():
    global _MIGRATION_DONE
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    new = os.path.join(base, "CarrierBypass")
    old = os.path.join(base, "T-MobileBypass")
    try:
        if not os.path.isdir(new):
            if os.path.isdir(old) and not _MIGRATION_DONE:
                try:
                    os.makedirs(new, exist_ok=True)
                    for fname in ("config.json", "history.json", "usage.json",
                                  "tmobile_bypass.log", "tmobile_bypass.log.1"):
                        src = os.path.join(old, fname)
                        dst = os.path.join(new, fname)
                        if os.path.exists(src) and not os.path.exists(dst):
                            try:
                                shutil.copy2(src, dst)
                            except Exception:
                                pass
                    _MIGRATION_DONE = True
                    return new
                except Exception:
                    _MIGRATION_DONE = True
                    # copy failed — fall back to the old dir so the user keeps their data
                    return old
            os.makedirs(new, exist_ok=True)
        return new
    except Exception:
        try:
            if os.path.isdir(old):
                return old
        except Exception:
            pass
        return tempfile.gettempdir()


def _usage_path():
    return os.path.join(_data_dir(), "usage.json")


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
        # multi-carrier
        "carrier": "auto",
        "hop_count": 1,
        "custom_ttl": 0,
        "auto_detect_carrier": True,
        # hardening
        "ncsi_disabled": False,
        "metered_wifi": False,
        # v1.3.0
        "ttl_probe_url": "",
        "cycle_start_day": 1,
        "auto_redetect": True,
        "tcp_timestamps_disabled": False,
        "mtu_masked": False,
        "cellular_mtu": 1420,
        "prev_mtu": 0,
        "autotuning_restricted": False,
        "prev_autotuning": "",
        "dns_gateway": False,
        "prev_dns": "",
        "prev_timestamps": "",
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


def _cleanup_stale_startup_value():
    """Delete a stale HKCU Run\\T-MobileBypass entry left over from the pre-rename build."""
    if not WIN:
        return
    try:
        import winreg
    except Exception:
        return
    sub = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, "T-MobileBypass")
                log("removed stale HKCU Run\\T-MobileBypass value (rename cleanup)")
            except FileNotFoundError:
                pass
    except Exception as e:
        log(f"_cleanup_stale_startup_value failed: {e}")


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
                winreg.SetValueEx(k, "CarrierBypass", 0, winreg.REG_SZ, _startup_command())
                # also drop any old T-MobileBypass value so we don't start twice
                try:
                    winreg.DeleteValue(k, "T-MobileBypass")
                except FileNotFoundError:
                    pass
            else:
                for name in ("CarrierBypass", "T-MobileBypass"):
                    try:
                        winreg.DeleteValue(k, name)
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
            for name in ("CarrierBypass", "T-MobileBypass"):
                try:
                    winreg.QueryValueEx(k, name)
                    return True
                except FileNotFoundError:
                    continue
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
                    "Carrier Bypass needs Administrator rights to change the hop limit.\n\n"
                    "Re-open the app and click Yes on the UAC prompt.",
                    "Carrier Bypass", 0x10)
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
            # dots colored by bypass state (any non-default, non-zero TTL = bypass was on)
            for i, (v, ttl) in enumerate(self._points):
                bypass_on = bool(ttl) and ttl != DEFAULT_TTL
                col = QColor(ACCENT) if bypass_on else QColor(MUTED)
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
            self.setWindowTitle("Carrier Bypass")
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setFixedSize(600, 760)
            self._drag = None
            self._speed_worker = None
            self._dl_worker = None
            self._queue_worker = None
            self._update_worker = None
            self._detect_worker = None
            self._verify_worker = None
            self._stack_worker = None
            self._dns_worker = None
            self._bypass_test = None
            self._quitting = False
            self._last_counters = None    # (alias, ssid, rx, tx)
            self._last_signature = None
            self._last_sig_change = 0.0
            self._build()
            self._build_tray()
            _cleanup_stale_startup_value()
            log("app started")

        # ---- tray ----
        def _build_tray(self):
            self.tray = QSystemTrayIcon(_icon(), self)
            self.tray.setToolTip("Carrier Bypass")
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
            title = QLabel("Carrier Bypass")
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
            # auto-detect carrier + hop count on launch (never blocks — worker thread)
            if cfg.get("auto_detect_carrier", True):
                QTimer.singleShot(1500, self._on_detect_carrier)
            # watchdog timer
            self._watchdog = QTimer(self)
            self._watchdog.timeout.connect(self._watchdog_tick)
            self._watchdog.start(5000)
            # hotspot monitor
            self._hotspot_timer = QTimer(self)
            self._hotspot_timer.timeout.connect(self._hotspot_tick)
            self._hotspot_timer.start(10000)
            # per-SSID data usage poll
            self._usage_timer = QTimer(self)
            self._usage_timer.timeout.connect(self._usage_tick)
            self._usage_timer.start(30000)
            QTimer.singleShot(2000, self._usage_tick)  # prime the counters
            # network-change detector (feature 4)
            self._netchange_timer = QTimer(self)
            self._netchange_timer.timeout.connect(self._netchange_tick)
            self._netchange_timer.start(5000)

        def _build_bypass_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            v.setContentsMargins(4, 10, 4, 4)
            v.setSpacing(8)

            card, cv = _card(page, "STATUS")
            self.hl_label = QLabel("Hop limit: —")
            self.hl_label.setStyleSheet(f"color:{TEXT}; font-size:20px; font-weight:700;")
            cv.addWidget(self.hl_label)
            self.carrier_label = QLabel("Carrier: —")
            self.carrier_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv.addWidget(self.carrier_label)
            self.path_label = QLabel("Path: —")
            self.path_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv.addWidget(self.path_label)
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

            # Carrier picker + Detect (compact single row)
            crow = QHBoxLayout()
            self.carrier_combo = QComboBox()
            self.carrier_combo.addItem("Auto-detect", "auto")
            for cid, prof in CARRIERS.items():
                self.carrier_combo.addItem(prof["name"], cid)
            sel = cfg.get("carrier", "auto")
            idx = self.carrier_combo.findData(sel)
            if idx >= 0:
                self.carrier_combo.setCurrentIndex(idx)
            self.carrier_combo.setStyleSheet(
                f"QComboBox {{ background:rgba(0,0,0,0.3); color:{TEXT}; border:1px solid rgba(255,255,255,0.1);"
                f" border-radius:9px; padding:6px 10px; font-size:12px; }}\n"
                f"QComboBox:focus {{ border:1px solid {ACCENT}; }}\n"
                f"QComboBox QAbstractItemView {{ background:#141821; color:{TEXT}; selection-background-color:{ACCENT}; }}")
            self.carrier_combo.currentIndexChanged.connect(self._on_carrier_changed)
            crow.addWidget(self.carrier_combo, 1)
            self.detect_btn = QPushButton("Detect")
            self.detect_btn.setFixedHeight(32)
            self.detect_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.08); color:{TEXT}; border:1px solid rgba(255,255,255,0.15);"
                f" border-radius:9px; font-size:12px; font-weight:600; padding:0 14px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.12); }}")
            self.detect_btn.clicked.connect(self._on_detect_carrier)
            crow.addWidget(self.detect_btn)
            self.verify_btn = QPushButton("Verify on wire")
            self.verify_btn.setFixedHeight(32)
            self.verify_btn.setToolTip(
                "Uses pktmon to observe the actual TTL leaving your NIC "
                "(not just the configured registry value).")
            self.verify_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.08); color:{TEXT}; border:1px solid rgba(255,255,255,0.15);"
                f" border-radius:9px; font-size:12px; font-weight:600; padding:0 12px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.12); }}")
            self.verify_btn.clicked.connect(self._on_verify_egress)
            crow.addWidget(self.verify_btn)
            v.addLayout(crow)

            self.verify_label = QLabel("Verify: not yet checked")
            self.verify_label.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            self.verify_label.setWordWrap(True)
            v.addWidget(self.verify_label)

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

            # DATA USAGE (per-SSID, per-cycle)
            card_du, cv_du = _card(page, "DATA USAGE")
            self.usage_ssid_label = QLabel("SSID: —")
            self.usage_ssid_label.setStyleSheet(f"color:{MUTED}; font-size:12px;")
            cv_du.addWidget(self.usage_ssid_label)
            self.usage_value_label = QLabel("0.0 GB used this cycle")
            self.usage_value_label.setStyleSheet(f"color:{TEXT}; font-size:16px; font-weight:700;")
            cv_du.addWidget(self.usage_value_label)
            self.usage_pbar = QProgressBar()
            self.usage_pbar.setRange(0, 100)
            self.usage_pbar.setTextVisible(False)
            self.usage_pbar.setFixedHeight(6)
            self.usage_pbar.setStyleSheet(
                f"QProgressBar {{ background:rgba(0,0,0,0.3); border:none; border-radius:3px; }}\n"
                f"QProgressBar::chunk {{ background:{ACCENT}; border-radius:3px; }}")
            cv_du.addWidget(self.usage_pbar)
            self.usage_note_label = QLabel("")
            self.usage_note_label.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            self.usage_note_label.setWordWrap(True)
            cv_du.addWidget(self.usage_note_label)
            self.usage_reset_btn = QPushButton("Reset cycle")
            self.usage_reset_btn.setFixedHeight(28)
            self.usage_reset_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.06); color:{MUTED}; border:none;"
                f" border-radius:8px; font-size:11px; padding:0 12px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.10); color:{TEXT}; }}")
            self.usage_reset_btn.clicked.connect(self._on_usage_reset)
            cv_du.addWidget(self.usage_reset_btn)
            v.addWidget(card_du)

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
            self.verdict_label = QLabel("")
            self.verdict_label.setStyleSheet(f"color:{MUTED}; font-size:12px; font-weight:600;")
            self.verdict_label.setWordWrap(True)
            cv.addWidget(self.verdict_label)
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

            # Hop count + custom TTL override
            from PySide6.QtWidgets import QSpinBox  # local import — avoids top-of-file bloat
            card_hops, cv_hops = _card(page, "TTL / HOP COUNT")
            spinstyle = (
                f"QSpinBox {{ background:rgba(0,0,0,0.3); color:{TEXT};"
                f" border:1px solid rgba(255,255,255,0.1); border-radius:9px;"
                f" padding:4px 8px; font-size:12px; min-width:70px; }}\n"
                f"QSpinBox:focus {{ border:1px solid {ACCENT}; }}")
            hop_row = QHBoxLayout()
            hop_lbl = QLabel("Hop count (laptop → phone)")
            hop_lbl.setStyleSheet(f"color:{TEXT}; font-size:12px;")
            self.hop_spin = QSpinBox()
            self.hop_spin.setRange(1, 4)
            self.hop_spin.setValue(int(cfg.get("hop_count") or 1))
            self.hop_spin.setStyleSheet(spinstyle)
            self.hop_spin.valueChanged.connect(self._on_hop_count_changed)
            hop_row.addWidget(hop_lbl); hop_row.addStretch(); hop_row.addWidget(self.hop_spin)
            cv_hops.addLayout(hop_row)

            ttl_row = QHBoxLayout()
            ttl_lbl = QLabel("Custom TTL override (0 = off · 32–255)")
            ttl_lbl.setStyleSheet(f"color:{TEXT}; font-size:12px;")
            self.ttl_spin = QSpinBox()
            self.ttl_spin.setRange(0, 255)
            self.ttl_spin.setValue(int(cfg.get("custom_ttl") or 0))
            self.ttl_spin.setStyleSheet(spinstyle)
            self.ttl_spin.valueChanged.connect(self._on_custom_ttl_changed)
            ttl_row.addWidget(ttl_lbl); ttl_row.addStretch(); ttl_row.addWidget(self.ttl_spin)
            cv_hops.addLayout(ttl_row)
            v.addWidget(card_hops)

            # Hardening toggles
            card_hard, cv_hard = _card(page, "HARDENING (extra tethering signals)")
            self.chk_ncsi = QCheckBox("Disable Windows connectivity beacons (NCSI)")
            self.chk_ncsi.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.chk_ncsi.setChecked(bool(cfg.get("ncsi_disabled")))
            self.chk_ncsi.toggled.connect(self._on_ncsi_toggle)
            cv_hard.addWidget(self.chk_ncsi)
            ncsi_note = QLabel(
                "Stops periodic msftconnecttest.com probes that fingerprint the device as a Windows PC. "
                "Side effect: the Wi-Fi icon may stop showing “internet access”.")
            ncsi_note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            ncsi_note.setWordWrap(True)
            cv_hard.addWidget(ncsi_note)

            self.chk_metered = QCheckBox("Treat this Wi-Fi as metered")
            self.chk_metered.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.chk_metered.setChecked(bool(cfg.get("metered_wifi")))
            self.chk_metered.toggled.connect(self._on_metered_toggle)
            cv_hard.addWidget(self.chk_metered)
            metered_note = QLabel(
                "Windows Update / Store / OneDrive stop pulling large transfers that give away a desktop. "
                "This key is owned by TrustedInstaller — if the write is blocked you’ll see a clean skip message.")
            metered_note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            metered_note.setWordWrap(True)
            cv_hard.addWidget(metered_note)

            # ---- Stack masking sub-group ----
            sm_title = QLabel("Stack masking")
            sm_title.setStyleSheet(f"color:{MUTED}; font-size:11px; letter-spacing:1px; font-weight:600; margin-top:4px;")
            cv_hard.addWidget(sm_title)

            self.chk_tstamp = QCheckBox("Disable TCP timestamps")
            self.chk_tstamp.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.chk_tstamp.setChecked(bool(cfg.get("tcp_timestamps_disabled")))
            self.chk_tstamp.toggled.connect(self._on_tstamp_toggle)
            cv_hard.addWidget(self.chk_tstamp)

            mtu_row = QHBoxLayout()
            self.chk_mtu = QCheckBox("Match cellular MTU")
            self.chk_mtu.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.chk_mtu.setChecked(bool(cfg.get("mtu_masked")))
            self.chk_mtu.toggled.connect(self._on_mtu_toggle)
            mtu_row.addWidget(self.chk_mtu)
            mtu_row.addStretch()
            self.mtu_spin = QSpinBox()
            self.mtu_spin.setRange(1280, 1500)
            self.mtu_spin.setValue(int(cfg.get("cellular_mtu") or 1420))
            self.mtu_spin.setStyleSheet(spinstyle)
            self.mtu_spin.valueChanged.connect(self._on_mtu_value_changed)
            mtu_row.addWidget(self.mtu_spin)
            cv_hard.addLayout(mtu_row)

            self.chk_autotune = QCheckBox("Restrict receive-window auto-tuning (⚠ cuts throughput on high-latency links)")
            self.chk_autotune.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.chk_autotune.setChecked(bool(cfg.get("autotuning_restricted")))
            self.chk_autotune.toggled.connect(self._on_autotune_toggle)
            cv_hard.addWidget(self.chk_autotune)

            stack_row = QHBoxLayout()
            self.stack_apply_btn = QPushButton("Apply stack masking")
            self.stack_apply_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.08); color:{TEXT}; border:1px solid rgba(255,255,255,0.15);"
                f" border-radius:9px; font-size:11px; font-weight:600; padding:6px 10px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.12); }}")
            self.stack_apply_btn.clicked.connect(self._on_stack_apply)
            stack_row.addWidget(self.stack_apply_btn)
            self.stack_restore_btn = QPushButton("Restore")
            self.stack_restore_btn.setStyleSheet(self.stack_apply_btn.styleSheet())
            self.stack_restore_btn.clicked.connect(self._on_stack_restore)
            stack_row.addWidget(self.stack_restore_btn)
            stack_row.addStretch()
            cv_hard.addLayout(stack_row)

            # ---- Phone-resolver DNS ----
            dns_title = QLabel("Phone-resolver DNS")
            dns_title.setStyleSheet(f"color:{MUTED}; font-size:11px; letter-spacing:1px; font-weight:600; margin-top:4px;")
            cv_hard.addWidget(dns_title)
            self.chk_dns = QCheckBox("Use gateway as DNS (mimics phone-side resolver)")
            self.chk_dns.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.chk_dns.setChecked(bool(cfg.get("dns_gateway")))
            self.chk_dns.toggled.connect(self._on_dns_toggle)
            cv_hard.addWidget(self.chk_dns)
            dns_note = QLabel(
                "Only applies to the current Wi-Fi adapter; reverts to DHCP on restore. "
                "A laptop querying 8.8.8.8 while 'on a phone' is a tell — phones use carrier DNS.")
            dns_note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            dns_note.setWordWrap(True)
            cv_hard.addWidget(dns_note)

            self.hardening_status = QLabel("")
            self.hardening_status.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            self.hardening_status.setWordWrap(True)
            cv_hard.addWidget(self.hardening_status)
            v.addWidget(card_hard)

            # ---- Billing cycle + auto redetect + TTL probe URL ----
            card_cy, cv_cy = _card(page, "USAGE / DETECTION")
            cy_row = QHBoxLayout()
            cy_lbl = QLabel("Billing cycle start day (1–28)")
            cy_lbl.setStyleSheet(f"color:{TEXT}; font-size:12px;")
            self.cycle_spin = QSpinBox()
            self.cycle_spin.setRange(1, 28)
            self.cycle_spin.setValue(int(cfg.get("cycle_start_day") or 1))
            self.cycle_spin.setStyleSheet(spinstyle)
            self.cycle_spin.valueChanged.connect(self._on_cycle_changed)
            cy_row.addWidget(cy_lbl); cy_row.addStretch(); cy_row.addWidget(self.cycle_spin)
            cv_cy.addLayout(cy_row)

            self.chk_auto_redetect = QCheckBox("Re-detect carrier + hop count on network change")
            self.chk_auto_redetect.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; spacing:8px; }}")
            self.chk_auto_redetect.setChecked(bool(cfg.get("auto_redetect", True)))
            self.chk_auto_redetect.toggled.connect(self._on_auto_redetect_toggle)
            cv_cy.addWidget(self.chk_auto_redetect)

            probe_lbl = QLabel("TTL probe URL (optional — a VPS endpoint that echoes the TTL of the incoming packet)")
            probe_lbl.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            probe_lbl.setWordWrap(True)
            cv_cy.addWidget(probe_lbl)
            self.probe_edit = QLineEdit()
            self.probe_edit.setText(cfg.get("ttl_probe_url") or "")
            self.probe_edit.setPlaceholderText("https://your-vps.example/ttl")
            self.probe_edit.setStyleSheet(
                f"QLineEdit {{ background:rgba(0,0,0,0.3); color:{TEXT}; border:1px solid rgba(255,255,255,0.1);"
                f" border-radius:9px; padding:6px; font-size:11px; }}\n"
                f"QLineEdit:focus {{ border:1px solid {ACCENT}; }}")
            self.probe_edit.editingFinished.connect(self._on_probe_url_changed)
            cv_cy.addWidget(self.probe_edit)
            v.addWidget(card_cy)

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

            # ---- Router rule export ----
            from PySide6.QtWidgets import QPlainTextEdit, QFileDialog  # local import
            card_rr, cv_rr = _card(page, "ROUTER RULES  (OpenWRT / GL.iNet)")
            rr_lbl = QLabel(
                "Paste these into your router's custom rules box (OpenWRT: Network → Firewall → "
                "Custom Rules). The router adds a hop, so the laptop behind it should go back to "
                "the Windows default hop limit (128).")
            rr_lbl.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            rr_lbl.setWordWrap(True)
            cv_rr.addWidget(rr_lbl)

            rr_if_row = QHBoxLayout()
            rr_if_lbl = QLabel("Interfaces:")
            rr_if_lbl.setStyleSheet(f"color:{TEXT}; font-size:12px;")
            rr_if_row.addWidget(rr_if_lbl)
            self.rr_ifaces = QLineEdit()
            self.rr_ifaces.setText(", ".join(DEFAULT_ROUTER_INTERFACES))
            self.rr_ifaces.setStyleSheet(
                f"QLineEdit {{ background:rgba(0,0,0,0.3); color:{TEXT}; border:1px solid rgba(255,255,255,0.1);"
                f" border-radius:9px; padding:6px; font-size:11px; }}\n"
                f"QLineEdit:focus {{ border:1px solid {ACCENT}; }}")
            self.rr_ifaces.editingFinished.connect(self._refresh_router_rules)
            rr_if_row.addWidget(self.rr_ifaces, 1)
            cv_rr.addLayout(rr_if_row)

            self.rr_text = QPlainTextEdit()
            self.rr_text.setReadOnly(True)
            self.rr_text.setStyleSheet(
                f"QPlainTextEdit {{ background:rgba(0,0,0,0.35); color:{TEXT};"
                f" border:1px solid rgba(255,255,255,0.08); border-radius:9px;"
                f" font-family:'Consolas','Courier New',monospace; font-size:11px; padding:6px; }}")
            self.rr_text.setMinimumHeight(120)
            cv_rr.addWidget(self.rr_text)

            rr_btn_row = QHBoxLayout()
            self.rr_copy_btn = QPushButton("Copy")
            self.rr_copy_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:#06231d; border:none; border-radius:8px;"
                f" font-size:11px; font-weight:700; padding:6px 12px; }}\n"
                f"QPushButton:hover {{ background:#00e8bb; }}")
            self.rr_copy_btn.clicked.connect(self._copy_router_rules)
            rr_btn_row.addWidget(self.rr_copy_btn)
            self.rr_save_btn = QPushButton("Save .txt")
            self.rr_save_btn.setStyleSheet(
                f"QPushButton {{ background:rgba(255,255,255,0.08); color:{TEXT}; border:1px solid rgba(255,255,255,0.15);"
                f" border-radius:8px; font-size:11px; font-weight:600; padding:6px 12px; }}\n"
                f"QPushButton:hover {{ background:rgba(255,255,255,0.12); }}")
            self.rr_save_btn.clicked.connect(lambda: self._save_router_rules(QFileDialog))
            rr_btn_row.addWidget(self.rr_save_btn)
            rr_btn_row.addStretch()
            cv_rr.addLayout(rr_btn_row)
            v.addWidget(card_rr)
            self._refresh_router_rules()

            card4, cv4 = _card(page, "ABOUT & LOGS")
            about = QLabel(
                "Defeats your carrier's hotspot cap via TTL/hop-limit fix.\n"
                "Only affects your own connection. May violate your carrier's terms of service.\n"
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
                for w in (self._speed_worker, self._dl_worker, self._queue_worker,
                          self._update_worker, self._detect_worker,
                          self._verify_worker, self._stack_worker, self._dns_worker):
                    if w is not None and w.isRunning():
                        w.wait(3000)
                super().closeEvent(e)
            else:
                e.ignore()
                self.hide()
                self.tray.showMessage(
                    "Carrier Bypass",
                    "Still running in the tray. Right-click the icon to quit.",
                    QSystemTrayIcon.Information, 2500)

        # ---- refresh / watchdog / hotspot ----
        def _current_carrier_id(self):
            sel = cfg.get("carrier", "auto")
            if sel == "auto":
                cached = _carrier_cache.get("result")
                if cached:
                    return cached[0]
                return "other"
            return sel

        def _refresh(self):
            hl = get_hoplimit()
            target = bypass_ttl(cfg)
            log(f"refresh: hoplimit={hl} target={target} admin={is_admin()}")
            if hl is None:
                self.hl_label.setText("Hop limit: unknown")
                self.state_label.setText("⚠ couldn't read netsh (not admin?)")
                self.state_label.setStyleSheet(f"color:{WARN}; font-size:12px;")
            elif hl == target:
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
            # carrier + path
            cid = self._current_carrier_id()
            prof = carrier_profile(cid)
            sel_mode = "auto" if cfg.get("carrier", "auto") == "auto" else "manual"
            cached = _carrier_cache.get("result")
            detail = cached[1] if (cached and sel_mode == "auto") else ""
            ctext = f"Carrier: {prof['name']} ({sel_mode})"
            if detail and sel_mode == "auto":
                ctext += f"  ·  {detail}"
            self.carrier_label.setText(ctext)
            hops = int(cfg.get("hop_count") or 1)
            custom = int(cfg.get("custom_ttl") or 0)
            if 32 <= custom <= 255:
                self.path_label.setText(f"Path: custom TTL override → {custom}")
            else:
                hop_word = "hop" if hops == 1 else "hops"
                self.path_label.setText(
                    f"Path: laptop → phone ({hops} {hop_word}) → target TTL {target}")
            host, ip = detect_connection()
            self.conn_label.setText(f"Connection: {host} ({ip})")
            ssid = get_active_ssid()
            self.ssid_label.setText(f"Wi-Fi: {ssid or '—'}")
            # keep router rules and data-usage card in sync with the current target TTL
            try:
                self._refresh_router_rules()
            except Exception:
                pass
            try:
                self._refresh_usage_ui(ssid)
            except Exception:
                pass

        def _watchdog_tick(self):
            if cfg.get("auto_bypass"):
                hl = get_hoplimit()
                target = bypass_ttl(cfg)
                if hl is not None and hl != target:
                    log(f"watchdog: hoplimit drifted to {hl}, re-applying {target}")
                    set_hoplimit(target)
                    self._refresh()

        def _hotspot_tick(self):
            if cfg.get("hotspot_auto"):
                ssid = get_active_ssid()
                if is_hotspot_ssid(ssid, cfg.get("hotspot_ssids", [])):
                    hl = get_hoplimit()
                    target = bypass_ttl(cfg)
                    if hl != target:
                        log(f"hotspot detected ({ssid}), enabling bypass ttl={target}")
                        set_hoplimit(target)
                        self._refresh()
                        self.tray.showMessage(
                            "Carrier Bypass", f"Hotspot detected ({ssid}) — bypass enabled.",
                            QSystemTrayIcon.Information, 3000)

        # ---- bypass actions ----
        def _on_toggle(self):
            hl = get_hoplimit()
            target = bypass_ttl(cfg)
            if hl == target:
                return self._on_restore()
            ok, detail = set_hoplimit(target)
            log(f"enable bypass: target={target} ok={ok} detail={detail!r}")
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
                target = bypass_ttl(cfg)
                ok, _ = set_hoplimit(target)
                log(f"auto_bypass enabled; apply {target} ok={ok}")
                self._refresh()

        # ---- carrier / hop detection ----
        def _on_carrier_changed(self, _idx):
            cid = self.carrier_combo.currentData() or "auto"
            cfg["carrier"] = cid
            _save_config(cfg)
            self._refresh()

        def _on_detect_carrier(self):
            self.detect_btn.setEnabled(False)
            self.detect_btn.setText("Detecting…")

            def _do():
                cinfo = detect_carrier()
                hinfo = detect_hop_count()
                return (cinfo, hinfo)

            def _done(res):
                self.detect_btn.setEnabled(True)
                self.detect_btn.setText("Detect")
                if res[0] != "ok":
                    self.state_label.setText(f"Detect failed: {str(res[1])[:80]}")
                    self.state_label.setStyleSheet(f"color:{WARN}; font-size:12px;")
                    return
                (cid, detail, cerr), (hops, ttl, hop_ips) = res[1]
                cfg["hop_count"] = int(hops)
                if cfg.get("auto_detect_carrier", True) and cfg.get("carrier", "auto") == "auto":
                    pass  # auto mode: cid comes from live cache on _refresh
                _save_config(cfg)
                if hasattr(self, "hop_spin"):
                    self.hop_spin.blockSignals(True)
                    self.hop_spin.setValue(int(hops))
                    self.hop_spin.blockSignals(False)
                log(f"detect: carrier={cid} detail={detail!r} err={cerr} hops={hops} ttl={ttl} ips={hop_ips}")
                self._refresh()

            self._detect_worker = Worker(_do)
            self._detect_worker.done.connect(_done)
            self._detect_worker.start()

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

        def _on_hop_count_changed(self, value):
            cfg["hop_count"] = int(value)
            _save_config(cfg)
            # if bypass is currently on, re-apply the new target so the fix stays valid
            hl = get_hoplimit()
            if hl is not None and hl != DEFAULT_TTL:
                target = bypass_ttl(cfg)
                if hl != target:
                    ok, _ = set_hoplimit(target)
                    log(f"hop_count changed to {value}; re-apply {target} ok={ok}")
            self._refresh()

        def _on_custom_ttl_changed(self, value):
            v = int(value)
            if v != 0 and not (32 <= v <= 255):
                # invalid range → treat as off
                v = 0
            cfg["custom_ttl"] = v
            _save_config(cfg)
            hl = get_hoplimit()
            if hl is not None and hl != DEFAULT_TTL:
                target = bypass_ttl(cfg)
                if hl != target:
                    ok, _ = set_hoplimit(target)
                    log(f"custom_ttl changed to {v}; re-apply {target} ok={ok}")
            self._refresh()

        def _on_ncsi_toggle(self, checked):
            try:
                if checked:
                    ok, detail = apply_disable_ncsi(cfg)
                else:
                    ok, detail = restore_ncsi(cfg)
            except Exception as e:  # belt-and-braces; helpers already fail-soft
                ok, detail = (False, str(e))
                log(f"_on_ncsi_toggle unexpected: {e}")
            cfg["ncsi_disabled"] = bool(checked and ok)
            _save_config(cfg)
            if not ok:
                self.chk_ncsi.blockSignals(True)
                self.chk_ncsi.setChecked(not checked)
                self.chk_ncsi.blockSignals(False)
                self.hardening_status.setText(f"NCSI toggle failed: {detail[:120]}")
                self.hardening_status.setStyleSheet(f"color:{WARN}; font-size:11px;")
            else:
                self.hardening_status.setText(
                    "NCSI beacons disabled." if checked else "NCSI beacons restored.")
                self.hardening_status.setStyleSheet(f"color:{ACCENT}; font-size:11px;")

        def _on_metered_toggle(self, checked):
            try:
                if checked:
                    ok, detail = apply_metered_wifi(cfg)
                else:
                    ok, detail = restore_metered_wifi(cfg)
            except Exception as e:
                ok, detail = (False, str(e))
                log(f"_on_metered_toggle unexpected: {e}")
            cfg["metered_wifi"] = bool(checked and ok)
            _save_config(cfg)
            if not ok:
                self.chk_metered.blockSignals(True)
                self.chk_metered.setChecked(not checked)
                self.chk_metered.blockSignals(False)
                self.hardening_status.setText(f"Metered Wi-Fi toggle failed: {detail[:160]}")
                self.hardening_status.setStyleSheet(f"color:{WARN}; font-size:11px;")
            else:
                self.hardening_status.setText(
                    "Wi-Fi marked as metered." if checked else "Wi-Fi metered flag restored.")
                self.hardening_status.setStyleSheet(f"color:{ACCENT}; font-size:11px;")

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
                        self._set_verdict(None)
                    else:
                        self.speed_val.setText(f"{mbps:.1f} Mbps")
                        ttl = get_hoplimit() or 0
                        hist = _append_history({"t": time.time(), "mbps": round(mbps, 1), "ttl": ttl})
                        self.graph.set_points([(h["mbps"], h["ttl"]) for h in hist])
                        self._set_verdict(mbps)
                else:
                    log(f"speed test error: {res[1]}")
                    self.speed_val.setText(f"✗ {res[1][:40]}")
                    self._set_verdict(None)

            self._speed_worker = Worker(speed_test)
            self._speed_worker.done.connect(done)
            self._speed_worker.start()

        def _set_verdict(self, mbps):
            if mbps is None:
                self.verdict_label.setText("")
                self.verdict_label.setStyleSheet(f"color:{MUTED}; font-size:12px; font-weight:600;")
                return
            cid = self._current_carrier_id()
            state, msg = throttle_verdict(mbps, cid)
            color = {"capped": BAD, "suspect": WARN, "clear": ACCENT}.get(state, MUTED)
            self.verdict_label.setText(msg)
            self.verdict_label.setStyleSheet(f"color:{color}; font-size:12px; font-weight:600;")

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
                target = bypass_ttl(cfg)
                ok, detail = set_hoplimit(target)
                log(f"bypass+test: apply ttl={target} ok={ok} detail={detail!r}")
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
                        self._set_verdict(after_mbps)
                        QMessageBox.information(self, "Bypass + Test", msg)
                    else:
                        self.speed_val.setText(f"before={before} after={after_mbps}")
                        self._set_verdict(None)

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

        # ---- verify on wire (feature 1) ----
        def _on_verify_egress(self):
            self.verify_btn.setEnabled(False)
            self.verify_label.setText("Verifying on wire…")
            self.verify_label.setStyleSheet(f"color:{MUTED}; font-size:11px;")

            cfg_snap = dict(cfg)

            def _do():
                return verify_egress_ttl(cfg=cfg_snap)

            def _done(res):
                self.verify_btn.setEnabled(True)
                if res[0] != "ok":
                    self.verify_label.setText(f"Verify: could not verify ({str(res[1])[:80]})")
                    self.verify_label.setStyleSheet(f"color:{MUTED}; font-size:11px;")
                    return
                d = res[1] or {}
                st = d.get("state")
                obs = d.get("observed_ttl")
                exp = d.get("expected_ttl")
                meth = d.get("method") or "?"
                if st == "verified":
                    self.verify_label.setText(f"Verify: ✓ verified on wire (TTL {obs}, via {meth})")
                    self.verify_label.setStyleSheet(f"color:{ACCENT}; font-size:11px; font-weight:600;")
                elif st == "mismatch":
                    self.verify_label.setText(
                        f"Verify: ✗ mismatch — observed TTL {obs}, expected {exp} (via {meth})")
                    self.verify_label.setStyleSheet(f"color:{BAD}; font-size:11px; font-weight:600;")
                else:
                    self.verify_label.setText(f"Verify: could not verify — {d.get('detail','')[:140]}")
                    self.verify_label.setStyleSheet(f"color:{MUTED}; font-size:11px;")

            self._verify_worker = Worker(_do)
            self._verify_worker.done.connect(_done)
            self._verify_worker.start()

        # ---- data usage (feature 2) ----
        def _usage_tick(self):
            alias = wifi_interface_alias()
            ssid = get_active_ssid()
            if not alias:
                self._last_counters = None
                self._refresh_usage_ui(ssid)
                return
            rx, tx = read_adapter_counters(alias)
            if rx is None or tx is None:
                self._last_counters = None
                self._refresh_usage_ui(ssid)
                return
            prev = self._last_counters
            if prev and prev[0] == alias and prev[1] == ssid:
                d_rx = rx - prev[2]
                d_tx = tx - prev[3]
                # counter reset (adapter disable/reboot): use current value as delta
                if d_rx < 0:
                    d_rx = rx
                if d_tx < 0:
                    d_tx = tx
                if ssid and (d_rx > 0 or d_tx > 0):
                    try:
                        usage_add(ssid, cfg.get("cycle_start_day", 1), d_rx, d_tx)
                    except Exception as e:
                        log(f"usage_add failed: {e}")
            self._last_counters = (alias, ssid, rx, tx)
            self._refresh_usage_ui(ssid)

        def _refresh_usage_ui(self, ssid):
            if not hasattr(self, "usage_ssid_label"):
                return
            self.usage_ssid_label.setText(f"SSID: {ssid or '—'}")
            rx, tx = usage_current(ssid, cfg.get("cycle_start_day", 1)) if ssid else (0, 0)
            gb = (rx + tx) / (1024 ** 3)
            cid = self._current_carrier_id()
            prof = carrier_profile(cid)
            allot = prof.get("typical_allotment_gb") or []
            low = allot[0] if allot else None
            if low:
                self.usage_value_label.setText(f"{gb:.2f} / {low} GB  this cycle")
                pct = min(100, int(gb / low * 100)) if low else 0
                self.usage_pbar.setValue(pct)
                if pct >= 95:
                    color = BAD
                elif pct >= 80:
                    color = WARN
                else:
                    color = ACCENT
                self.usage_pbar.setStyleSheet(
                    f"QProgressBar {{ background:rgba(0,0,0,0.3); border:none; border-radius:3px; }}\n"
                    f"QProgressBar::chunk {{ background:{color}; border-radius:3px; }}")
                self.usage_note_label.setText(
                    f"{prof.get('name','?')} low-end allotment: {low} GB · cycle starts day "
                    f"{cfg.get('cycle_start_day',1)}")
            else:
                self.usage_value_label.setText(f"{gb:.2f} GB used this cycle")
                self.usage_pbar.setValue(0)
                self.usage_note_label.setText(
                    f"cycle starts day {cfg.get('cycle_start_day',1)} · no allotment recorded for {prof.get('name','?')}")

        def _on_usage_reset(self):
            ssid = get_active_ssid()
            if not ssid:
                QMessageBox.information(self, "Data usage", "No active Wi-Fi SSID — nothing to reset.")
                return
            r = QMessageBox.question(
                self, "Reset cycle",
                f"Zero the current-cycle counter for '{ssid}'?",
                QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes:
                return
            usage_reset(ssid, cfg.get("cycle_start_day", 1))
            self._last_counters = None  # re-baseline
            self._refresh_usage_ui(ssid)

        def _on_cycle_changed(self, value):
            cfg["cycle_start_day"] = int(value)
            _save_config(cfg)
            self._refresh_usage_ui(get_active_ssid())

        # ---- stack masking (feature 3) ----
        def _stack_status(self, ok, detail, ok_msg):
            if ok:
                self.hardening_status.setText(ok_msg)
                self.hardening_status.setStyleSheet(f"color:{ACCENT}; font-size:11px;")
            else:
                self.hardening_status.setText(f"{ok_msg.split('.')[0]} failed: {str(detail)[:160]}")
                self.hardening_status.setStyleSheet(f"color:{WARN}; font-size:11px;")

        def _on_tstamp_toggle(self, checked):
            try:
                if checked:
                    ok, detail = apply_disable_tcp_timestamps(cfg)
                else:
                    ok, detail = restore_tcp_timestamps(cfg)
            except Exception as e:
                ok, detail = (False, str(e))
            cfg["tcp_timestamps_disabled"] = bool(checked and ok)
            _save_config(cfg)
            if not ok:
                self.chk_tstamp.blockSignals(True)
                self.chk_tstamp.setChecked(not checked)
                self.chk_tstamp.blockSignals(False)
            self._stack_status(ok, detail,
                               "TCP timestamps disabled." if checked else "TCP timestamps restored.")

        def _on_mtu_toggle(self, checked):
            try:
                if checked:
                    ok, detail = apply_cellular_mtu(cfg, mtu=self.mtu_spin.value())
                else:
                    ok, detail = restore_mtu(cfg)
            except Exception as e:
                ok, detail = (False, str(e))
            cfg["mtu_masked"] = bool(checked and ok)
            _save_config(cfg)
            if not ok:
                self.chk_mtu.blockSignals(True)
                self.chk_mtu.setChecked(not checked)
                self.chk_mtu.blockSignals(False)
            self._stack_status(ok, detail,
                               f"MTU set to {self.mtu_spin.value()}." if checked else "MTU restored.")

        def _on_mtu_value_changed(self, value):
            cfg["cellular_mtu"] = int(value)
            _save_config(cfg)
            # if already applied, re-apply with new value
            if cfg.get("mtu_masked"):
                try:
                    ok, detail = apply_cellular_mtu(cfg, mtu=int(value))
                    self._stack_status(ok, detail, f"MTU updated to {value}.")
                except Exception as e:
                    log(f"_on_mtu_value_changed re-apply failed: {e}")

        def _on_autotune_toggle(self, checked):
            try:
                if checked:
                    ok, detail = apply_autotuning_restricted(cfg)
                else:
                    ok, detail = restore_autotuning(cfg)
            except Exception as e:
                ok, detail = (False, str(e))
            cfg["autotuning_restricted"] = bool(checked and ok)
            _save_config(cfg)
            if not ok:
                self.chk_autotune.blockSignals(True)
                self.chk_autotune.setChecked(not checked)
                self.chk_autotune.blockSignals(False)
            self._stack_status(ok, detail,
                               "Auto-tuning restricted." if checked else "Auto-tuning restored.")

        def _on_stack_apply(self):
            """Apply items 1 and 2 (timestamps + MTU) — never auto-tuning."""
            def _do():
                r1 = apply_disable_tcp_timestamps(cfg)
                r2 = apply_cellular_mtu(cfg, mtu=self.mtu_spin.value())
                return (r1, r2)

            def _done(res):
                if res[0] != "ok":
                    self._stack_status(False, res[1], "Apply stack masking")
                    return
                r1, r2 = res[1]
                ok = bool(r1[0]) and bool(r2[0])
                cfg["tcp_timestamps_disabled"] = bool(r1[0])
                cfg["mtu_masked"] = bool(r2[0])
                _save_config(cfg)
                self.chk_tstamp.blockSignals(True); self.chk_tstamp.setChecked(bool(r1[0])); self.chk_tstamp.blockSignals(False)
                self.chk_mtu.blockSignals(True); self.chk_mtu.setChecked(bool(r2[0])); self.chk_mtu.blockSignals(False)
                self._stack_status(ok, (r1[1] or "") + " | " + (r2[1] or ""),
                                   "Stack masking applied (timestamps + MTU).")

            self._stack_worker = Worker(_do)
            self._stack_worker.done.connect(_done)
            self._stack_worker.start()

        def _on_stack_restore(self):
            def _do():
                r1 = restore_tcp_timestamps(cfg)
                r2 = restore_mtu(cfg)
                r3 = restore_autotuning(cfg) if cfg.get("autotuning_restricted") else (True, "not applied")
                return (r1, r2, r3)

            def _done(res):
                if res[0] != "ok":
                    self._stack_status(False, res[1], "Restore stack masking")
                    return
                r1, r2, r3 = res[1]
                cfg["tcp_timestamps_disabled"] = False
                cfg["mtu_masked"] = False
                cfg["autotuning_restricted"] = False
                _save_config(cfg)
                for chk in (self.chk_tstamp, self.chk_mtu, self.chk_autotune):
                    chk.blockSignals(True); chk.setChecked(False); chk.blockSignals(False)
                self._stack_status(True, "", "Stack masking restored.")

            self._stack_worker = Worker(_do)
            self._stack_worker.done.connect(_done)
            self._stack_worker.start()

        # ---- phone-resolver DNS (feature 6) ----
        def _on_dns_toggle(self, checked):
            def _do():
                return set_dns_to_gateway(cfg) if checked else restore_dns_dhcp(cfg)

            def _done(res):
                if res[0] != "ok":
                    ok, detail = (False, str(res[1]))
                else:
                    ok, detail = res[1]
                cfg["dns_gateway"] = bool(checked and ok)
                _save_config(cfg)
                if not ok:
                    self.chk_dns.blockSignals(True)
                    self.chk_dns.setChecked(not checked)
                    self.chk_dns.blockSignals(False)
                    self.hardening_status.setText(f"DNS toggle failed: {detail[:160]}")
                    self.hardening_status.setStyleSheet(f"color:{WARN}; font-size:11px;")
                else:
                    if checked:
                        gw = get_gateway_ip() or "?"
                        prev = cfg.get("prev_dns") or "DHCP"
                        self.hardening_status.setText(f"DNS → {gw} (was {prev}).")
                    else:
                        self.hardening_status.setText("DNS restored to DHCP.")
                    self.hardening_status.setStyleSheet(f"color:{ACCENT}; font-size:11px;")

            self._dns_worker = Worker(_do)
            self._dns_worker.done.connect(_done)
            self._dns_worker.start()

        # ---- network change (feature 4) ----
        def _netchange_tick(self):
            if not cfg.get("auto_redetect", True):
                return
            sig = network_signature()
            if self._last_signature is None:
                self._last_signature = sig
                return
            if sig == self._last_signature:
                return
            now = time.time()
            if (now - self._last_sig_change) < 15:
                return  # anti-thrash
            self._last_sig_change = now
            old = self._last_signature
            self._last_signature = sig
            log(f"network change: {old} → {sig}")

            def _do():
                return (detect_carrier(), detect_hop_count())

            def _done(res):
                if res[0] != "ok":
                    return
                (cid, detail, cerr), (hops, ttl, hop_ips) = res[1]
                cfg["hop_count"] = int(hops)
                _save_config(cfg)
                if hasattr(self, "hop_spin"):
                    self.hop_spin.blockSignals(True)
                    self.hop_spin.setValue(int(hops))
                    self.hop_spin.blockSignals(False)
                target = bypass_ttl(cfg)
                hl = get_hoplimit()
                if hl is not None and hl != DEFAULT_TTL and hl != target:
                    ok, _ = set_hoplimit(target)
                    log(f"network change re-apply: ttl={target} ok={ok}")
                self._refresh()
                self._last_counters = None  # different SSID / adapter → re-baseline
                try:
                    self.tray.showMessage(
                        "Carrier Bypass",
                        f"Network changed → hops={hops}, target TTL {target}",
                        QSystemTrayIcon.Information, 3000)
                except Exception:
                    pass

            self._detect_worker = Worker(_do)
            self._detect_worker.done.connect(_done)
            self._detect_worker.start()

        def _on_auto_redetect_toggle(self, checked):
            cfg["auto_redetect"] = bool(checked)
            _save_config(cfg)

        def _on_probe_url_changed(self):
            url = self.probe_edit.text().strip()
            cfg["ttl_probe_url"] = url
            _save_config(cfg)

        # ---- router rules (feature 5) ----
        def _current_router_interfaces(self):
            parts = [p.strip() for p in self.rr_ifaces.text().split(",") if p.strip()]
            return parts or list(DEFAULT_ROUTER_INTERFACES)

        def _refresh_router_rules(self):
            if not hasattr(self, "rr_text"):
                return
            ttl = bypass_ttl(cfg)
            self.rr_text.setPlainText(router_rules(ttl, self._current_router_interfaces()))

        def _copy_router_rules(self):
            try:
                QApplication.clipboard().setText(self.rr_text.toPlainText())
                self.rr_copy_btn.setText("Copied ✓")
                QTimer.singleShot(1500, lambda: self.rr_copy_btn.setText("Copy"))
            except Exception as e:
                log(f"clipboard set failed: {e}")

        def _save_router_rules(self, QFileDialog):
            try:
                default = os.path.join(os.path.expanduser("~"), "Downloads", "carrier_bypass_router_rules.txt")
                path, _f = QFileDialog.getSaveFileName(self, "Save router rules", default, "Text (*.txt)")
                if not path:
                    return
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self.rr_text.toPlainText())
                self.rr_save_btn.setText("Saved ✓")
                QTimer.singleShot(1500, lambda: self.rr_save_btn.setText("Save .txt"))
            except Exception as e:
                log(f"save router rules failed: {e}")

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
            new_exe = os.path.join(tempfile.gettempdir(), "CarrierBypass-new.exe")

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
