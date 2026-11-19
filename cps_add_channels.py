"""
Add channels from channels_to_program.csv to a zone in MOTOTRBO CPS 2.0,
by driving the CPS window (pywinauto / UI Automation).

For each channel it does what you'd do by hand:
  + (Add)  ->  Choose Set Type: Analog/Digital  ->  OK
  select the new row  ->  pencil (Edit)  ->  fill the form  ->  back to the zone

Before running:
  * CPS 2.0 is open with your codeplug loaded.
  * Don't touch the mouse/keyboard while it runs.

Usage (relaunches itself as Administrator if needed, because CPS runs as admin):
    python cps_add_channels.py                # add every channel not already in the zone
    python cps_add_channels.py --limit 1      # add just one, to test
    python cps_add_channels.py --dry-run      # only show what it would add

Safety: before typing into a form it checks the form shows the row it expects,
and it stops at the first problem. It never saves the codeplug and never writes
to the radio: check the result in CPS, then click Save / Write yourself.

Every step is timed in cps_add_channels.log; a step that runs longer than
30 s gets a "still waiting" line, so a stall shows exactly where it is.
"""
import argparse
import csv
import ctypes
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "cps_add_channels.log"

ANALOG_BANDWIDTH = "25"     # kHz. Ham FM repeaters are wideband (5 kHz deviation). Use "12.5" to match narrow.
STEP_WARN_SECONDS = 30


# ------------------------------------------------------------------ logging
def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


_current = {"step": None, "since": 0.0, "warned": 0.0}


@contextmanager
def step(name):
    """Time one action; the watchdog reports it if it hangs."""
    _current.update(step=name, since=time.time(), warned=0.0)
    t0 = time.time()
    try:
        yield
    finally:
        dt = time.time() - t0
        _current["step"] = None
        log(f"      {dt:5.1f}s  {name}")


def _watchdog():
    while True:
        time.sleep(5)
        s = _current["step"]
        if s and time.time() - _current["since"] > STEP_WARN_SECONDS \
                and time.time() - _current["warned"] > STEP_WARN_SECONDS:
            _current["warned"] = time.time()
            log(f"      ... still waiting on '{s}' ({time.time() - _current['since']:.0f}s)")


def ensure_admin():
    if ctypes.windll.shell32.IsUserAnAdmin():
        return
    # cmd /k keeps the elevated window open so you can read the result
    inner = " ".join(f'"{a}"' for a in [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])
    log("CPS runs as Administrator, relaunching this script elevated (approve the UAC prompt)...")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/k "{inner}"', str(HERE), 1)
    sys.exit(0)


# ------------------------------------------------- fast UI Automation lookups
from pywinauto import Desktop                                      # noqa: E402
from pywinauto.controls.uiawrapper import UIAWrapper               # noqa: E402
from pywinauto.uia_element_info import UIAElementInfo              # noqa: E402
from pywinauto.uia_defines import IUIA                             # noqa: E402
from pywinauto.keyboard import send_keys                           # noqa: E402

_UIA = IUIA()
_DESC = _UIA.tree_scope["descendants"]
_CHILD = _UIA.tree_scope["children"]


def _wrap(el):
    return UIAWrapper(UIAElementInfo(el))


def _elem(x):
    return x.element_info.element if hasattr(x, "element_info") else x


def find_id(root, auto_id, scope=_DESC):
    """One UIA FindFirst by AutomationId (pywinauto's child_window walks the whole tree in Python)."""
    cond = _UIA.iuia.CreatePropertyCondition(_UIA.UIA_dll.UIA_AutomationIdPropertyId, auto_id)
    el = _elem(root).FindFirst(scope, cond)
    return _wrap(el) if el else None


def find_name(root, name, scope=_CHILD):
    cond = _UIA.iuia.CreatePropertyCondition(_UIA.UIA_dll.UIA_NamePropertyId, name)
    el = _elem(root).FindFirst(scope, cond)
    return _wrap(el) if el else None


def wait_for(fn, timeout, interval=0.15):
    end = time.time() + timeout
    while True:
        r = fn()
        if r or time.time() > end:
            return r
        time.sleep(interval)


_main = {"w": None}


def cps_main():
    w = _main["w"]
    try:
        if w is not None and w.is_visible():
            return w
    except Exception:
        pass
    w = Desktop(backend="uia").window(title_re=r".*MOTOTRBO CPS 2\.0$").wrapper_object()
    _main["w"] = w
    return w


# ------------------------------------------------------------- zone grid
class ZoneGrid:
    """The Zone Items grid. Column positions are read once; the grid element itself
    is re-found cheaply after CPS rebuilds the view (e.g. after the edit form)."""

    def __init__(self):
        self.cols = None
        self.refresh()

    def refresh(self):
        main = cps_main()
        if not wait_for(lambda: find_id(main, "ZoneItems"), 5):
            raise RuntimeError("Zone Items grid not found")
        presenter = find_id(main, "dgrSets")
        cond = _UIA.iuia.CreatePropertyCondition(_UIA.UIA_dll.UIA_ClassNamePropertyId, "ViewableRecordCollection")
        self.table = _wrap(_elem(presenter).FindFirst(_CHILD, cond))
        self.add_btn = find_id(main, "btnAdd")
        self.edit_btn = find_id(main, "btnEdit")
        if self.cols is None:
            header = [c for c in self.table.children() if c.element_info.control_type == "Header"][0]
            self.cols = {h.window_text(): i for i, h in enumerate(header.children(control_type="HeaderItem"))}

    @property
    def rows(self):
        return self.table.iface_grid.CurrentRowCount

    def scroll_to(self, row):
        """Rows are virtualized: scroll so `row` exists as an element."""
        try:
            sp = self.table.iface_scroll
            if sp.CurrentVerticallyScrollable:
                pct = 100.0 * row / max(1, self.rows - 1)
                sp.SetScrollPercent(-1, min(100.0, pct))     # -1 = leave horizontal alone
                time.sleep(0.15)
        except Exception:
            pass

    def cell(self, row, col_name):
        for _ in range(4):
            el = self.table.iface_grid.GetItem(row, self.cols[col_name])
            try:
                el.CurrentControlType        # raises if the row isn't realized (NULL)
                return _wrap(el)
            except (ValueError, OSError, AttributeError):
                self.scroll_to(row)
        raise RuntimeError(f"row {row + 1} not reachable in the grid")

    def get(self, row, col_name):
        try:
            return self.cell(row, col_name).iface_value.CurrentValue
        except Exception:
            return ""

    def existing(self):
        out = []
        for r in range(self.rows):
            try:
                rx = round(float(self.get(r, "RX:Frequency (MHz)")), 4)
            except ValueError:
                rx = None
            out.append((self.get(r, "Channel Type"), rx, self.get(r, "Channel Name")))
        return out

    def row_element(self, row, expected_name):
        """The on-screen DataItem for `row`, identified by its name (never by guesswork)."""
        for _ in range(3):
            self.scroll_to(row)
            name_cell = self.cell(row, "Channel Name")
            parent = _wrap(_UIA.iuia.ControlViewWalker.GetParentElement(_elem(name_cell)))
            if parent.element_info.control_type == "DataItem" and name_cell.window_text() == expected_name:
                return parent
        raise RuntimeError(f"couldn't find row {row + 1} ({expected_name}) on screen")


def open_zone(zone_name):
    """Show the zone's Zone Items grid (clicks the zone in the Set Categories tree)."""
    main = cps_main()
    if find_id(main, "ZoneItems"):
        return
    node = find_id(main, zone_name)
    if node is None:
        raise RuntimeError(f"zone {zone_name!r} not found in the tree")
    (find_id(node, "tblName") or node).click_input()
    if not wait_for(lambda: find_id(main, "ZoneItems"), 8):
        raise RuntimeError(f"couldn't open zone {zone_name!r}")
    time.sleep(0.3)


def find_dialog(title, timeout):
    """'Choose Set Type' shows up as a top-level window or as a child of CPS."""
    root = _UIA.root

    def look():
        return find_name(root, title) or find_name(cps_main(), title, _CHILD)
    return wait_for(look, timeout)


def add_row(grid, kind):
    before = grid.rows
    dlg = find_dialog("Choose Set Type", 0)               # maybe left open
    if dlg is None:
        grid.add_btn.click_input()
        dlg = find_dialog("Choose Set Type", 6)
    if dlg is None:
        raise RuntimeError("'Choose Set Type' dialog didn't appear (zone full?)")
    combo = dlg.descendants(control_type="ComboBox")[0]
    try:
        combo.select(kind)
    except Exception:
        combo.click_input()
        time.sleep(0.3)
        send_keys(kind[0] + "{ENTER}")
    ok = [b for b in dlg.descendants(control_type="Button") if b.window_text() == "OK"][0]
    ok.click_input()
    if not wait_for(lambda: grid.rows > before, 8):
        raise RuntimeError("no new row appeared after OK (zone may be full)")
    # the new row has a default name like "Channel138"; normally the last one
    for r in range(grid.rows - 1, max(-1, grid.rows - 4), -1):
        if re.fullmatch(r"Channel\d+", grid.get(r, "Channel Name")):
            return r
    raise RuntimeError("couldn't identify the new row")


# ------------------------------------------------------- pencil (edit form)
# (channel type) -> {(section, label or id): automation id}; read once per channel type
_FIELD_IDS = {}


def scan_form():
    """Full scan of the edit form (slow, so done once per channel type and cached)."""
    form = find_id(cps_main(), "scwSetEditor")
    section, ids = "general", {}
    for e in form.descendants():
        ci = e.element_info
        if not (ci.class_name.startswith("RM") and ci.class_name.endswith("Editor")):
            continue
        if ci.automation_id == "CP_RXFREQ":
            section = "rx"
        elif ci.automation_id == "CP_TXFREQ":
            section = "tx"
        label = next((k.window_text() for k in e.children() if k.element_info.control_type == "Text"), "")
        ids[(section, ci.automation_id)] = ci.automation_id
        ids.setdefault((section, label), ci.automation_id)
    return ids


def field(ctype, section, key):
    """The editor for a field, looked up by its automation id (fast)."""
    form = find_id(cps_main(), "scwSetEditor")
    ids = _FIELD_IDS.get(ctype)
    if ids is None or (section, key) not in ids:
        ids = _FIELD_IDS[ctype] = scan_form()       # first time, or a field that just appeared
    aid = ids.get((section, key))
    return find_id(form, aid) if aid else None


def open_edit_form(grid, row):
    """Open `row` in the pencil form. Refuses (raises) unless the form shows that row's name."""
    expected = grid.get(row, "Channel Name")
    if not expected:
        raise RuntimeError(f"row {row + 1} has no readable name")
    item = grid.row_element(row, expected)
    kids = item.children()
    selector = [c for c in kids if c.element_info.class_name == "RecordSelector"]
    cells = [c for c in kids if c.element_info.class_name == "Cell"]
    attempts = [
        lambda: selector[0].click_input(),                 # the row-header arrow on the left
        lambda: item.iface_selection_item.Select(),
        lambda: cells[1].click_input(),                    # Position cell
    ]
    for attempt in attempts:
        try:
            attempt()
        except Exception:
            continue
        if wait_for(lambda: grid.edit_btn.is_enabled(), 1.5):
            break
    else:
        raise RuntimeError("couldn't select the row (pencil stayed disabled)")
    grid.edit_btn.click_input()
    name_ed = wait_for(lambda: find_id(cps_main(), "CP_CNVPERSALIAS"), 8)
    if not name_ed:
        raise RuntimeError("edit form didn't open")
    time.sleep(0.3)
    shown = read_field(name_ed)
    if shown != expected:
        raise RuntimeError(f"SAFETY STOP: form opened {shown!r}, expected {expected!r}; nothing was changed")


def _input_of(editor):
    for k in editor.children():
        if k.element_info.control_type in ("Edit", "ComboBox"):
            return k
    raise RuntimeError(f"no input in {editor.element_info.automation_id}")


def read_field(editor):
    k = _input_of(editor)
    if k.element_info.control_type == "ComboBox":
        try:
            return k.selected_text() or ""
        except Exception:
            pass
    try:
        return k.iface_value.CurrentValue
    except Exception:
        return k.window_text()


def _matches(have, want):
    try:
        return abs(float(have) - float(want)) < 0.0005
    except ValueError:
        return have.strip().lower() == want.strip().lower()


def write_field(editor, value):
    value = str(value)
    if _matches(read_field(editor), value):
        return True                                       # already right (e.g. CSQ default)
    k = _input_of(editor)
    try:
        k.iface_scroll_item.ScrollIntoView()
    except Exception:
        pass
    if k.element_info.control_type == "ComboBox":
        try:
            k.select(value)
        except Exception:
            k.click_input()
            time.sleep(0.3)
            items = [i for i in k.descendants(control_type="ListItem") if i.window_text() == value]
            if items:
                items[0].click_input()
            else:
                send_keys("{ESC}")
    else:
        try:
            k.iface_value.SetValue(value)
        except Exception:
            pass
        if not _matches(read_field(editor), value):
            k.click_input()
            send_keys("^a{BACKSPACE}" + value + "{TAB}", pause=0.02)
    return wait_for(lambda: _matches(read_field(editor), value), 1.0, 0.1)


def program(ch):
    """Fill the open edit form for one channel. Returns a list of problems."""
    analog = ch["Type"] == "Analog"
    plan = [("general", "CP_CNVPERSALIAS", ch["Name"])]
    if analog:
        plan.append(("general", "Channel Bandwidth (kHz)", ANALOG_BANDWIDTH))
    else:
        plan += [("general", "CP_COLORCODE", ch["Color Code"]),
                 ("general", "CP_SLTASSGMNT", ch["Timeslot"])]
    plan += [("rx", "CP_RXFREQ", ch["RX MHz"]), ("tx", "CP_TXFREQ", ch["TX MHz"])]
    if analog:
        for side, tone in (("rx", ch["RX Tone"]), ("tx", ch["TX Tone"])):
            if tone and tone != "None":
                plan += [(side, "Squelch Type", "TPL"), (side, "TPL Frequency (Hz)", tone)]
            else:
                plan.append((side, "Squelch Type", "CSQ"))
    else:
        plan.append(("tx", "CP_UKPPERS", ch["Contact"]))

    problems = []
    for section, key, val in plan:
        with step(f"set {section}:{key} = {val}"):
            ed = field(ch["Type"], section, key)
            if ed is None:
                problems.append(f"{section}:{key} not found on form")
                continue
            if not write_field(ed, val):
                problems.append(f"{section}:{key} wanted {val!r}, CPS shows {read_field(ed)!r}")
    return problems


def same_freq(a, b):
    return a is not None and abs(a - b) < 0.0005


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(HERE / "channels_to_program.csv"))
    ap.add_argument("--zone", default="4. Canada, BC", help="zone name exactly as in the CPS tree")
    ap.add_argument("--limit", type=int, default=0, help="add at most N channels (0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    if not args.dry_run:
        ensure_admin()
    LOG.write_text("", encoding="utf-8")
    threading.Thread(target=_watchdog, daemon=True).start()

    with open(args.csv, newline="", encoding="utf-8") as f:
        wanted = list(csv.DictReader(f))

    cps_main().set_focus()
    with step("open zone + read existing channels"):
        open_zone(args.zone)
        grid = ZoneGrid()
        have = grid.existing()
    log(f"Zone currently has {len(have)} channels.")

    todo = []
    for ch in wanted:
        rx = float(ch["RX MHz"])
        # same type + same frequency + same call sign (several repeaters share 444.825 etc.)
        if any(t == ch["Type"] and same_freq(f, rx) and n.upper().startswith(ch["Call"].upper())
               for t, f, n in have):
            log(f"  skip (already in zone): {ch['Name']} {rx}")
            continue
        todo.append(ch)
    if args.limit:
        todo = todo[:args.limit]
    log(f"Will add {len(todo)} channels.")
    if args.dry_run:
        for ch in todo:
            log(f"  {ch['Type']:7} {ch['Name']:16} RX {ch['RX MHz']} TX {ch['TX MHz']}")
        return

    # blank rows left over from an interrupted run (default name "Channel123") get reused
    leftovers = {}
    for r, (t, _, n) in enumerate(have):
        if re.fullmatch(r"Channel\d+", n):
            leftovers.setdefault(t, []).append(r)
    if leftovers:
        log(f"Blank rows to reuse: {sum(len(v) for v in leftovers.values())}")

    added, failed, t_start = 0, [], time.time()
    for i, ch in enumerate(todo, 1):
        t_ch = time.time()
        log(f"[{i}/{len(todo)}] {ch['Type']} {ch['Name']}")
        try:
            with step("back to zone list"):
                open_zone(args.zone)
                grid.refresh()
            spare = leftovers.get(ch["Type"], [])
            if spare:
                row = spare.pop(0)
            else:
                with step("add row (+ / Choose Set Type / OK)"):
                    row = add_row(grid, ch["Type"])
            if grid.get(row, "Channel Type") != ch["Type"]:
                raise RuntimeError(f"SAFETY STOP: row {row + 1} is {grid.get(row, 'Channel Type')}, not {ch['Type']}")
            with step(f"open row {row + 1} in edit form"):
                open_edit_form(grid, row)
            problems = program(ch)
            with step("back to zone list (commits channel)"):
                open_zone(args.zone)
            added += 1
            status = "OK" if not problems else "CHECK: " + "; ".join(problems)
            log(f"    -> row {row + 1}: {status}  ({time.time() - t_ch:.0f}s)")
            if problems:
                failed.append(ch["Name"])
                log("Stopping so you can look at this channel before anything else changes.")
                break
        except Exception as e:
            import traceback
            log(f"    STOPPED at {ch['Name']}: {e}")
            log(traceback.format_exc())
            failed.append(ch["Name"])
            break
    total = time.time() - t_start
    log(f"\nDone: {added} added in {total / 60:.1f} min"
        f" ({total / max(1, added):.0f}s per channel), {len(failed)} need attention: {', '.join(failed) or 'none'}")
    log("Nothing was saved. Review the zone in CPS, then Save / Write yourself.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        log(traceback.format_exc())
