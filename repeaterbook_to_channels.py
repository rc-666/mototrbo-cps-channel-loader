"""
Convert a RepeaterBook CSV export into a clean list of channels that a
MOTOTRBO radio can actually use (analog FM + DMR).

Usage:
    python repeaterbook_to_channels.py RepeaterBook_export.csv
    python repeaterbook_to_channels.py BC_70cm_Vancouver_nearest_first.csv [RB_export.csv]
      (CHIRP format; channel order is kept; the optional RepeaterBook file supplies DMR color codes)

Writes channels_to_program.csv next to this script. Review it, edit it if you
like (Excel is fine), then feed it to the CPS automation script.
"""
import csv
import html
import sys
from pathlib import Path

# ---- Settings you may want to change -------------------------------------
DMR_TIMESLOT = 1            # matches the existing DMR channels in "4. Canada, BC"
DMR_CONTACT = "BC 1"        # must match a Digital Contact name in your codeplug
NAME_MAX = 16               # CPS 2.0 channel alias limit
# ---------------------------------------------------------------------------

ANALOG_HINTS = {"FM", "IRLP", "ECHOLINK", "ALLSTAR"}   # linked-analog systems are FM


def tone(value: str) -> str:
    v = (value or "").strip()
    if not v or v.upper() == "CSQ":
        return "None"
    try:
        return f"{float(v):.1f}"
    except ValueError:
        return "None"


def make_name(call: str, location: str) -> str:
    # "Vancouver - Mt Seymour" -> "Vancouver"; keep the town, it's the useful part
    town = location.split(" - ")[0].strip()
    return f"{call} {town}"[:NAME_MAX].strip()


def is_chirp(src: Path) -> bool:
    with src.open(encoding="utf-8-sig") as f:
        return f.readline().startswith("Location,Name,Frequency,Duplex")


def color_codes_from_rb(rb: Path):
    """{(call, rx MHz rounded): color code} from a RepeaterBook export, if one is given."""
    out = {}
    if rb and rb.exists():
        with rb.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                cc = row.get("Digital Access", "").strip()
                if "DMR" in row["Modes"].upper() and cc.isdigit() and int(cc) <= 15:
                    out[(row["Call"].strip(), round(float(row["Output Freq"]), 4))] = cc
    return out


def convert_chirp(src: Path, rb: Path = None):
    """CHIRP export: Frequency = repeater output (your RX), Duplex/Offset give your TX.
    Tone mode: ''/none, 'Tone' = TX tone only (rToneFreq), 'TSQL' = TX+RX tone (cToneFreq).
    Mode FM = analog; DIG / DV = digital (DMR on MOTOTRBO)."""
    ccs = color_codes_from_rb(rb)
    channels, skipped = [], []
    with src.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            call = row["Name"].strip()
            location = html.unescape(row["Comment"]).strip()
            rx = float(row["Frequency"])
            off = float(row["Offset"] or 0)
            dup = row["Duplex"].strip()
            tx = rx + off if dup == "+" else rx - off if dup == "-" else rx
            if off > 10 or dup not in ("+", "-", ""):
                skipped.append((call, location, rx, "cross-band split, not supported"))
                continue
            mode = row["Mode"].strip().upper()
            town = location.split(",")[0].strip()
            name = f"{call} {town}"[:NAME_MAX].strip()
            if mode in ("FM", "NFM"):
                tmode = row["Tone"].strip()
                tx_tone = rx_tone = "None"
                if tmode == "Tone":
                    tx_tone = tone(row["rToneFreq"])
                elif tmode == "TSQL":
                    tx_tone = rx_tone = tone(row["cToneFreq"])
                channels.append({"Name": name, "Type": "Analog", "RX MHz": f"{rx:.5f}", "TX MHz": f"{tx:.5f}",
                                 "TX Tone": tx_tone, "RX Tone": rx_tone, "Color Code": "", "Timeslot": "",
                                 "Contact": "", "Call": call, "Location": location})
            elif mode in ("DIG", "DV", "DMR"):
                cc = ccs.get((call, round(rx, 4)), "1")
                channels.append({"Name": name, "Type": "Digital", "RX MHz": f"{rx:.5f}", "TX MHz": f"{tx:.5f}",
                                 "TX Tone": "", "RX Tone": "", "Color Code": cc, "Timeslot": str(DMR_TIMESLOT),
                                 "Contact": DMR_CONTACT, "Call": call, "Location": location})
            else:
                skipped.append((call, location, rx, f"mode {mode} not supported by MOTOTRBO"))
    return channels, skipped


def convert(src: Path):
    channels, skipped = [], []
    seen = set()
    with src.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            call = row["Call"].strip()
            if not call:
                continue
            location = html.unescape(row["Location"]).strip()
            modes = set(row["Modes"].upper().split())
            offset = row["Offset"].strip()
            rx = float(row["Output Freq"])
            tx = float(row["Input Freq"])

            if offset.lower() == "s" or abs(rx - tx) > 10:
                skipped.append((call, location, rx, "cross-band split, not supported"))
                continue

            kinds = []
            if modes & ANALOG_HINTS:
                kinds.append("Analog")
            if "DMR" in modes:
                kinds.append("Digital")
            if not kinds:
                skipped.append((call, location, rx, f"modes {' '.join(sorted(modes))} not supported by MOTOTRBO"))
                continue

            for kind in kinds:
                key = (call, rx, kind)
                if key in seen:
                    continue
                seen.add(key)
                name = make_name(call, location)
                if kind == "Digital" and "Analog" in kinds:
                    name = (name[:NAME_MAX - 2] + " D").strip()   # tell the two apart
                cc = row.get("Digital Access", "").strip()
                channels.append({
                    "Name": name,
                    "Type": kind,
                    "RX MHz": f"{rx:.5f}",
                    "TX MHz": f"{tx:.5f}",
                    "TX Tone": tone(row["Uplink Tone"]) if kind == "Analog" else "",
                    "RX Tone": tone(row["Downlink Tone"]) if kind == "Analog" else "",
                    "Color Code": (cc if cc.isdigit() and int(cc) <= 15 else "1") if kind == "Digital" else "",
                    "Timeslot": str(DMR_TIMESLOT) if kind == "Digital" else "",
                    "Contact": DMR_CONTACT if kind == "Digital" else "",
                    "Call": call,
                    "Location": location,
                })
    return dedupe_names(channels), skipped


def dedupe_names(channels):
    # CPS needs distinct names: "VE7ROX Burnaby" twice -> "VE7ROX Burnaby", "VE7ROX Burnab2"
    used = {}
    for ch in channels:
        base = ch["Name"]
        n = used.get(base, 0) + 1
        used[base] = n
        if n > 1:
            ch["Name"] = base[:NAME_MAX - len(str(n))] + str(n)
    return channels


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = Path(sys.argv[1])
    if is_chirp(src):
        # optional 2nd arg: a RepeaterBook export to pull DMR color codes from
        rb = Path(sys.argv[2]) if len(sys.argv) > 2 else None
        channels, skipped = convert_chirp(src, rb)
        channels = dedupe_names(channels)
    else:
        channels, skipped = convert(src)

    out = Path(__file__).with_name("channels_to_program.csv")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(channels[0].keys()))
        w.writeheader()
        w.writerows(channels)

    n_a = sum(c["Type"] == "Analog" for c in channels)
    n_d = len(channels) - n_a
    print(f"Wrote {len(channels)} channels ({n_a} analog, {n_d} DMR) to {out}")
    print(f"Skipped {len(skipped)} repeaters:")
    for call, loc, rx, why in skipped:
        print(f"  {call:8} {rx:.4f}  {loc[:35]:35}  {why}")


if __name__ == "__main__":
    main()
