# T-Mobile Bypass (Windows)

Two real capabilities in one glass UI:

1. **TTL / hop-limit fix** — sets your Windows default hop limit to 65 so
   tethered (hotspot) traffic looks like phone-native traffic. T-Mobile keys
   its 600 kbps hotspot cap off the TTL, so this removes the cap. Persists
   across reboots until you hit **Restore**.
2. **Parallel-chunk downloader** — 12 simultaneous connections with resume, to
   pull large files (AI models, etc.) at full bandwidth.

## Run

**Option A — run as script (needs Python):**
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

## Usage

1. Open the app (it elevates to admin).
2. Tap **ENABLE BYPASS** → hop limit becomes 65.
3. Disconnect + reconnect your phone's hotspot.
4. Confirm with **Run test** (speed should jump from ~0.6 Mbps to full).
5. Paste a download URL (e.g. a HuggingFace `resolve/main/...gguf` link) and
   hit **Download**.

## Honest notes

- This only defeats T-Mobile's **hotspot** cap. It does **not** bypass
  deprioritization of on-device data after your priority allotment, and a VPN
  won't either.
- Violates T-Mobile ToS (affects only your own connection).
- TTL value 65 = phone sends 64, minus the one phone-hop = 64 at the carrier,
  indistinguishable from on-phone traffic.
