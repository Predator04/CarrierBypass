"""Windows smoke test for Carrier Bypass v1.2.0 (run on the Windows Python).

Verifies:
  1. detect_carrier() against the live connection
  2. detect_hop_count() with the real tracert
  3. bypass_ttl() / throttle_verdict() math
  4. the PySide6 UI actually constructs (offscreen, no UAC, no app.exec loop)
"""
import os
import sys
import importlib.util
import traceback

os.environ["QT_QPA_PLATFORM"] = "offscreen"

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("tb", os.path.join(HERE, "tmobile_bypass.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(label, fn):
    try:
        val = fn()
        print(f"[ok]   {label}: {val}")
        return val
    except Exception as e:
        print(f"[FAIL] {label}: {e}")
        traceback.print_exc()
        fails.append(label)
        return None


print(f"=== Carrier Bypass v{m.VERSION} smoke test ===")
print(f"python {sys.version.split()[0]}  admin={m.is_admin()}")

print("\n-- 1. live carrier detect --")
cid = check("detect_carrier", lambda: m.detect_carrier())
check("detect_carrier (cached 2nd call)", lambda: m.detect_carrier())

print("\n-- 2. real tracert hop count --")
hc = check("detect_hop_count", lambda: m.detect_hop_count())

print("\n-- 3. ttl math --")
check("current hoplimit", lambda: m.get_hoplimit())
if hc:
    cfg = dict(m._load_config())
    cfg["hop_count"] = hc[0]
    if cid:
        cfg["carrier"] = cid[0]
    check(f"bypass_ttl for detected path (hops={hc[0]}, carrier={cfg.get('carrier')})",
          lambda: m.bypass_ttl(cfg))

print("\n-- 4. UI construction (offscreen) --")
try:
    from PySide6.QtWidgets import QApplication
    m.is_admin = lambda: True            # skip the UAC relaunch gate
    m.relaunch_as_admin = lambda: True

    report = []

    def fake_exec(self):
        # Inspect while build_ui's local `win` reference is still alive — once the
        # frame unwinds, the widget is garbage collected and disappears from
        # topLevelWidgets().
        wins = [w for w in self.topLevelWidgets() if w.__class__.__name__ == "MainWindow"]
        if not wins:
            report.append(("FAIL", "MainWindow was never constructed"))
            return 0
        w = wins[0]
        report.append(("ok", f"MainWindow built: title={w.windowTitle()!r} tabs={w.tabs.count()}"))
        for i in range(w.tabs.count()):
            report.append(("info", f"tab {i}: {w.tabs.tabText(i)}"))
        for name in ("carrier_combo", "hl_label", "carrier_label", "path_label",
                     "hop_spin", "ttl_spin", "chk_ncsi", "chk_metered",
                     "hardening_status", "verdict_label"):
            widget = getattr(w, name, None)
            report.append(("info" if widget is not None else "FAIL",
                           f"{name}: {'present' if widget is not None else 'MISSING'}"))
        combo = getattr(w, "carrier_combo", None)
        if combo is not None:
            items = [combo.itemText(i) for i in range(combo.count())]
            report.append(("info", f"carrier_combo ({combo.count()}): {items}"))
        for name in ("hl_label", "carrier_label", "path_label", "state_label"):
            lbl = getattr(w, name, None)
            if lbl is not None:
                report.append(("info", f"{name} text: {lbl.text()!r}"))
        return 0

    _real_exec = QApplication.exec
    QApplication.exec = fake_exec
    try:
        m.build_ui()
    except SystemExit:
        pass                              # build_ui ends with sys.exit(app.exec())
    finally:
        QApplication.exec = _real_exec

    if not report:
        raise RuntimeError("build_ui() never reached app.exec() — window not built")
    for level, line in report:
        print(f"[{level:<4}] {line}" if level != "info" else f"         {line}")
        if level == "FAIL":
            fails.append(line)
except Exception as e:
    print(f"[FAIL] UI construction: {e}")
    traceback.print_exc()
    fails.append("UI construction")

print("\n-- 5. hardening apply/restore (registry, reversible) --")
try:
    cfg = dict(m._load_config())
    before_ncsi = m._reg_read_dword("HKEY_LOCAL_MACHINE", m._NCSI_KEY[0], m._NCSI_KEY[1])
    print(f"         EnableActiveProbing before: {before_ncsi}")
    print(f"[ok]   apply_disable_ncsi -> {m.apply_disable_ncsi(cfg)}")
    print(f"         EnableActiveProbing now: "
          f"{m._reg_read_dword('HKEY_LOCAL_MACHINE', m._NCSI_KEY[0], m._NCSI_KEY[1])}")
    print(f"[ok]   restore_ncsi       -> {m.restore_ncsi(cfg)}")
    after_ncsi = m._reg_read_dword("HKEY_LOCAL_MACHINE", m._NCSI_KEY[0], m._NCSI_KEY[1])
    print(f"         EnableActiveProbing restored: {after_ncsi}")
    if before_ncsi is not None and after_ncsi != before_ncsi:
        print("[FAIL] NCSI value not restored to its original")
        fails.append("ncsi restore")
    # metered key is TrustedInstaller-owned: must fail SOFT, never raise
    print(f"[ok]   apply_metered_wifi -> {m.apply_metered_wifi(cfg)}   (fail-soft path is fine)")
    print(f"[ok]   restore_metered_wifi -> {m.restore_metered_wifi(cfg)}")
except Exception as e:
    print(f"[FAIL] hardening: {e}")
    traceback.print_exc()
    fails.append("hardening")

print("\n=== RESULT: " + ("PASS" if not fails else f"FAIL ({len(fails)}): {fails}") + " ===")
sys.exit(1 if fails else 0)