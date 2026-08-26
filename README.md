# Carrier Bypass (Windows)

Glass-UI Windows utility that defeats your carrier's hotspot throttle and pulls
big files at full speed. Works on any US carrier — T-Mobile, AT&T, Verizon and
their MVNOs — with auto-detection of both the carrier and the number of hops
between your laptop and the phone.

## Features

1. **TTL / hop-limit fix** — sets your default hop limit so tethered (hotspot)
   traffic looks phone-native. Carriers key their hotspot cap off the TTL; this
   removes it. Persists until you hit **Restore**.
2. **Multi-carrier auto-detect** — figures out which carrier the phone is on by
   inspecting the public IP (ip-api → ipinfo fallback), so the correct throttle
   verdict and hop-count target apply automatically.
3. **Hop-count auto-detect** — runs a short `tracert` and counts leading
   RFC1918 hops (10/8, 172.16/12, 192.168/16). CGNAT (100.64/10) is treated as
   the carrier side and stops the count. Handles travel-router / laptop → router
   → phone → carrier layouts (2 private hops = TTL 66).
4. **Throttle verdict** — after every speed test the app labels the result
   `capped` / `suspect` / `clear` against the selected carrier's known throttle
   floor, so you can tell at a glance if the bypass is holding.
5. **Windows fingerprint hardening (optional)** — two reversible toggles that
   remove non-TTL tethering signals: disable the NCSI connectivity beacons
   (`msftconnecttest.com`) and mark the current Wi-Fi as metered so Windows
   Update / Store / OneDrive stop pulling desktop-shaped transfers.
6. **Parallel-chunk downloader** — 12 simultaneous connections with resume, to
   pull large files (AI models, etc.) at full bandwidth.
7. **Download queue** — paste multiple URLs, download them in order.
8. **Auto-TTL watchdog** — keep the bypass applied on boot and re-apply it
   automatically if Windows resets the hop limit.
9. **Hotspot auto-detect** — enable the bypass automatically when you join a
   known phone hotspot (editable SSID list).
10. **Speed-test history + trend graph** — every test is logged and graphed
    (green = bypass on, gray = off).
11. **One-click "Bypass + Test"** — apply the fix, flush DNS, and measure
    before/after side by side.
12. **Self-update** — checks GitHub for new releases and swaps in the new build.
13. **System tray icon** — minimize to tray with quick toggles.
14. **On-wire TTL verification** — a `Verify on wire` button uses Windows
    `pktmon` to observe the actual TTL leaving your NIC. The whole point is to
    distinguish *configured* from *actual*, so a green ✓ means we saw the TTL
    on the wire, not that the registry says so.
15. **Per-SSID data usage counter** — tracks bytes per Wi-Fi SSID per billing
    cycle so you can see how close you are to the throttle before you hit it.
16. **TCP/IP stack masking** — an independently-toggleable sub-group in the
    Hardening card: disable TCP timestamps, match a cellular MTU (1420 by
    default), and (with a warning) restrict receive-window auto-tuning.
17. **Auto re-detect on network change** — moving from direct-to-phone to
    travel router silently changes the hop count; this notices the SSID /
    gateway / adapter change and re-runs detection, re-applying the correct
    TTL.
18. **Travel-router rule export** — a copyable block of iptables/ip6tables
    mangle rules for the case where the bypass has to live on the router
    (OpenWRT, GL.iNet).
19. **Phone-resolver DNS** — optional toggle to point the current Wi-Fi
    adapter's DNS at the gateway, so a laptop querying 8.8.8.8 while "on a
    phone" doesn't stand out.

## Supported carriers

Values below match what the carrier throttles hotspot traffic *down to* once
your priority allotment for the month runs out.

| Carrier | Detected ASN(s) | After-cap speed | Typical allotment (GB) |
|---|---|---|---|
| T-Mobile | AS21928 | 600 kbps | 15 / 50 |
| AT&T | AS20057 | 128 kbps | 5 / 60 |
| Verizon | AS6167 | 600 kbps (3 Mbps on 5G UW) | 5 / 60 |
| Visible (Verizon MVNO) | AS6167 | 5 Mbps baseline cap | unlimited-ish |
| Metro by T-Mobile | AS21928 | 600 kbps | 5 / 15 |
| Cricket (AT&T MVNO) | AS20057 | 128 kbps | 15 |
| Mint Mobile (T-Mobile MVNO) | AS21928 | 128 kbps | 5 / 10 |
| US Mobile | AS21928, AS6167 | 600 kbps | 10 / 50 |
| Google Fi | AS21928 | 256 kbps | 5 / 50 |
| Straight Talk / TracFone | AS21928, AS6167 | 128 kbps | 10 |
| Boost Mobile | AS6167 | 512 kbps | 12 / 30 |
| Other / Unknown | — | 600 kbps (assumed) | unknown |

Pick your carrier from the drop-down on the Bypass tab, or leave it on
**Auto-detect** and hit **Detect** to have the app populate it for you.

## Carrier auto-detect

`detect_carrier()` queries `http://ip-api.com/json/` for `isp`, `org`, `as`, and
`query` (falling back to `https://ipinfo.io/json`). It matches carrier keywords
first, then ASNs, and caches the answer for 60 seconds so UI refreshes don't
hammer the endpoint. If both endpoints fail it reports `other` with the reason
in the log and never raises.

## Hop-count detection

`detect_hop_count()` runs `tracert -d -h 4 -w 400 1.1.1.1` and counts how many
leading hops are RFC1918-private. CGNAT (100.64/10) is treated as the *carrier
side* and stops the count. The result (clamped 1..4) is added to the carrier's
phone TTL to compute the exact hop-limit you need — no more guessing.

- Direct tether (laptop → phone → carrier): 1 hop → target TTL 65
- Travel router (laptop → router → phone → carrier): 2 hops → target TTL 66

## Hardening toggles (optional)

Both toggles have an apply *and* a restore path, save the previous registry
value before touching it, and fail soft if the write is denied.

- **Disable Windows connectivity beacons (NCSI)** —
  `HKLM\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet\EnableActiveProbing`
  set to `0`. Stops the periodic `msftconnecttest.com` requests that identify
  the device as a Windows PC. Side effect: the Wi-Fi icon may stop showing
  "internet access".
- **Treat this Wi-Fi as metered** —
  `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\DefaultMediaCost\WiFi`
  set to `2`. Windows Update / Store / OneDrive stop pulling large transfers
  that would give away a desktop. This key is owned by TrustedInstaller; if the
  write is refused the UI surfaces a clean skip message instead of crashing.

## On-wire TTL verification

The Bypass tab has a **Verify on wire** button. It uses `pktmon` (Windows 10
1809+, requires admin — the app already runs elevated) to capture a real
outbound packet, decode the ETL, and read the TTL as it actually leaves the
NIC. This catches per-adapter overrides, VPNs, and failed `netsh` writes that
otherwise show up as "green but broken".

- ✓ **verified** — pktmon (or the optional probe) observed the exact expected
  TTL leaving the NIC.
- ✗ **mismatch** — observed TTL is different from the configured target.
- **could not verify** — pktmon didn't return a TTL and no probe URL is set.
  The app will *not* claim `verified` from the configured registry value.

**Carrier-side confirmation.** `pktmon` verifies what leaves the NIC, not what
the carrier sees. If you want end-to-end confirmation you can point
`ttl_probe_url` at a VPS you control that echoes the TTL of the incoming
packet as a bare integer or `{"ttl": N}` JSON. (Set it in Settings → USAGE /
DETECTION.)

## Per-SSID data usage tracking

A `DATA USAGE` card on the Bypass tab tracks bytes per SSID per billing cycle
using `Get-NetAdapterStatistics`. Counters are polled every 30 s and
accumulated as **deltas** so adapter disable / reboot resets are handled — a
current value lower than the previous snapshot is treated as a reset. Set the
billing cycle start day (1–28) in Settings → USAGE / DETECTION. When a
carrier profile is selected, the card also shows usage against the *low end*
of that carrier's typical allotment (amber ≥ 80%, red ≥ 95%).

## Stack masking

The Hardening card has a `Stack masking` sub-group that groups three
independently-toggleable TCP/IP stack tweaks — each saves its previous value
before touching it, and every apply has a matching restore:

1. **Disable TCP timestamps** —
   `netsh int tcp set global timestamps=disabled`, restore `enabled`.
2. **Match cellular MTU** —
   `netsh interface ipv4 set subinterface "<Wi-Fi>" mtu=1420 store=persistent`
   (spinbox 1280–1500), restore to the value read beforehand.
3. **Restrict receive-window auto-tuning** —
   `netsh int tcp set global autotuninglevel=restricted`, restore `normal`.
   **Off by default** and labelled with its cost: it reduces the receive
   window and can cut throughput on high-latency links. Not included in
   `Apply stack masking`.

`Apply stack masking` runs items 1 and 2 only. `Restore` undoes all three.

## Auto re-detect on network change

`network_signature()` returns `(ssid, gateway_ip, interface_alias)`. A 5-second
timer watches for changes and, when it sees one, re-runs `detect_carrier()` and
`detect_hop_count()` in a worker, writes the new `hop_count` to config, and if
the bypass is currently active re-applies `bypass_ttl(cfg)` with the new
value. A tray notification names the new target TTL. Guarded against thrash
(one change per 15 s). Toggle in Settings → USAGE / DETECTION.

## Travel-router rule export

A `ROUTER RULES` card on the Settings tab exports iptables/ip6tables mangle
rules for the case where the bypass has to live on the router:

```
iptables  -t mangle -I PREROUTING  -i usb0 -j TTL --ttl-set 65
iptables  -t mangle -I POSTROUTING -o usb0 -j TTL --ttl-set 65
ip6tables -t mangle -I PREROUTING  -i usb0 -j HL  --hl-set  65
ip6tables -t mangle -I POSTROUTING -o usb0 -j HL  --hl-set  65
…
```

Interfaces default to `usb0, usb1, eth1, eth2, wwan0, wlan0` and are editable.
`Copy` puts the block on the clipboard, `Save .txt` writes a file. Paste into
OpenWRT → Network → Firewall → Custom Rules (or the GL.iNet custom-rules box).
**The router adds a hop**, so the laptop behind it should go back to the
Windows default hop limit (128).

## Phone-resolver DNS

A `Use gateway as DNS (mimics phone-side resolver)` toggle in the Hardening
card points the current Wi-Fi adapter's DNS at the gateway
(`netsh interface ipv4 set dnsservers name="<Wi-Fi>" static <gw> primary`).
The previous DNS setting is stored so `Restore` puts it back to DHCP (or the
saved static list). Fails soft with a clear message if the adapter alias
can't be resolved.

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
Output lands in `dist\CarrierBypass.exe`.

**Start with Windows** — Settings → "Start with Windows" (launches minimized to tray).

## Usage

1. Open the app (it elevates to admin).
2. Hit **Detect** to pick up your carrier + hop count automatically (or select
   the carrier manually from the drop-down).
3. Tap **ENABLE BYPASS** → hop limit becomes phone_ttl + hops (65 for a
   one-hop tether).
4. Disconnect + reconnect your phone's hotspot.
5. Confirm with **⚡ Bypass + Test** — the verdict line will read `Full-speed`
   once the bypass is holding.
6. Paste download URLs in the **Downloads** tab and hit **Download all**.

## Honest notes

- This only defeats the **hotspot** cap. It does **not** bypass
  deprioritization of on-device data after your priority allotment, and a VPN
  won't either.
- May violate your carrier's terms of service. Only affects your own
  connection.
- Bypass TTL = phone_ttl (64) + laptop→phone hop count. The packet leaves the
  laptop with that TTL so it arrives at the carrier with 64 — indistinguishable
  from on-phone traffic.
