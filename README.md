# T-Mobile Bypass (Windows)

Glass-UI Windows utility that defeats T-Mobile's hotspot throttle and pulls big
files at full speed.

## Features

1. **TTL / hop-limit fix** — sets your default hop limit to 65 so tethered
   (hotspot) traffic looks phone-native. T-Mobile keys its 600 kbps hotspot cap
   off the TTL; this removes it. Persists until you hit **Restore**.
2. **Parallel-chunk downloader** — 12 simultaneous connections with resume, to
   pull large files (AI models, etc.) at full bandwidth.
3. **Download queue** — paste multiple URLs, download them in order.
4. **Auto-TTL watchdog** — keep the bypass applied on boot and re-apply it
   automatically if Windows resets the hop limit.
5. **Hotspot auto-detect** — enable the bypass automatically when you join a
   known phone hotspot (editable SSID list).
6. **Speed-test history + trend graph** — every test is logged and graphed
   (green = bypass on, gray = off).
7. **One-click "Bypass + Test"** — apply the fix, flush DNS, and measure
   before/after side by side.
8. **Self-update** — checks GitHub for new releases and swaps in the new build.
9. **System tray icon** — minimize to tray with quick toggles.

## Run

**Option A — run as script (needs Python 3.11 + PySide6):**
```
pip install PySide6
python tmobile_bypass.py
```
It re-launches itself as Administrator automatically.

**Option B — build a one-click .exe:**
```
build_exe.bat
```
Output lands in `dist\T-MobileBypass.exe`.

**Start with Windows** — Settings → "Start with Windows" (launches minimized to tray).

## Usage

1. Open the app (it elevates to admin).
2. Tap **ENABLE BYPASS** → hop limit becomes 65.
3. Disconnect + reconnect your phone's hotspot.
4. Confirm with **⚡ Bypass + Test** (speed should jump from ~0.6 Mbps to full).
5. Paste download URLs in the **Downloads** tab and hit **Download all**.

## Honest notes

- This only defeats T-Mobile's **hotspot** cap. It does **not** bypass
  deprioritization of on-device data after your priority allotment, and a VPN
  won't either.
- Violates T-Mobile ToS (affects only your own connection).
- TTL value 65 = phone sends 64, minus the one phone-hop = 64 at the carrier,
  indistinguishable from on-phone traffic.
