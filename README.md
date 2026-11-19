# MOTOTRBO CPS 2.0 channel loader

Bulk-add amateur radio repeater channels to a zone in **Motorola MOTOTRBO CPS 2.0**
(tested with an R7, UHF) from a RepeaterBook or CHIRP CSV export.

CPS 2.0 has no channel import, so the script drives the CPS window the way you would
by hand: **+** → *Choose Set Type* (Analog/Digital) → **OK** → select row → **pencil** →
fill the form → back to the zone.

## What's here

| File | What it does |
|---|---|
| `repeaterbook_to_channels.py` | Converts a RepeaterBook CSV or CHIRP CSV into `channels_to_program.csv` (review/edit it in Excel). Keeps FM and DMR; skips D-STAR/Fusion/NXDN/P25-only and cross-band repeaters. |
| `cps_add_channels.py` | Adds the channels from `channels_to_program.csv` to a CPS zone. |

## How channels are built

- **RX** = repeater output, **TX** = input (or frequency ± offset for CHIRP).
- **Analog:** 25 kHz bandwidth; CHIRP `Tone` = TX tone only (RX CSQ), `TSQL` = same tone on TX and RX; no tone = CSQ.
- **DMR:** color code from RepeaterBook (default 1), timeslot 1, contact `BC 1`. Change `DMR_TIMESLOT` / `DMR_CONTACT` at the top of `repeaterbook_to_channels.py`.
- Names are `CALL Town`, cut to 16 characters and made unique.

## Usage

Needs Windows, Python 3.12+, and CPS 2.0 open with the codeplug loaded.

```bash
pip install -r requirements.txt
python repeaterbook_to_channels.py BC_70cm.csv [RepeaterBook_export.csv]
python cps_add_channels.py --dry-run      # show what would be added
python cps_add_channels.py --limit 1      # add one channel as a test
python cps_add_channels.py                # add the rest
```

Options: `--zone "4. Canada, BC"` (zone name as shown in the CPS tree), `--csv FILE`.

CPS runs as Administrator, so the script asks for elevation (UAC). Don't use the mouse
or keyboard while it runs.

## Safety

- Skips repeaters already in the zone (same type, frequency and call sign).
- Before typing into a form, checks the form shows the row it expects; stops at the first problem.
- Reuses blank `ChannelNNN` rows left by an interrupted run.
- **Never saves the codeplug or writes to the radio.** Review in CPS, then Save / Write yourself.
- Every step is timed in `cps_add_channels.log`; a step over 30 s is flagged.

## Known limits

- Speed: about 2–3 minutes per channel in early versions; the current version is
  optimised but not yet measured.
- Zone capacity depends on the radio model; the script stops when CPS won't add more.
- UI automation depends on the CPS 2.0 layout and may break with other CPS versions.

Use with a valid amateur radio licence and check every channel before transmitting.
