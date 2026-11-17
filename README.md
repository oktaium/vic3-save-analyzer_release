# Victoria 3 Save Analyzer

Turn a series of **Victoria 3 saves** (for example one autosave per in-game year) into an interactive
**HTML report** that shows how countries, sectors and buildings develop over time — and compare two runs
side by side (for example a vanilla game against a game with an AI mod).

- Works with the game's normal binary saves (`.v3`). Tested with **Victoria 3 1.15.1**.
- Needs only **Python 3.10 or newer**. Nothing else to install.
- The report is one self-contained `.html` file: open it in any browser, no internet needed, easy to share.

## What you need

1. **Python 3.10+** — download from [python.org](https://www.python.org/downloads/). On Windows, tick
   *"Add python.exe to PATH"* during setup. Check it works: open a command prompt and type `python --version`.
2. **This tool** — download `vic3-save-analyzer-<version>.zip` from the
   [latest release](https://github.com/oktaium/vic3-save-analyzer_release/releases/latest) and unpack it anywhere
   (or `git clone https://github.com/oktaium/vic3-save-analyzer_release.git`).
3. **Saves** — one folder of `.v3` files per game you want to look at. You can collect them yourself, or let the
   included **save harvester** keep every autosave of a running game for you: see [HARVESTER.md](HARVESTER.md).
4. *(Optional)* **Victoria 3 installed** — used only to show the game's own building names. Without it the
   report still works but shows internal keys (`building_iron_mine` instead of *Iron Mine*).

## Quick start

**1. Put your saves in `saves/`, one folder per run:**

```
vic3-save-analyzer/
  compare.py
  saves/
    run_A/      000001__autosave.v3, 000002__autosave.v3, ...
    run_B/      ...
```

File names don't matter — saves are ordered by their in-game date, which is read from each file.
Use **autosaves** (the harvester collects them; the game writes them to `Documents\Paradox Interactive\Victoria 3\save games\`).
Manual saves may be stored in a format the tool can't read yet.

**2. Open a command prompt in this folder** (in Explorer: click the address bar, type `cmd`, press Enter) **and run:**

```bash
python compare.py saves/run_A saves/run_B
```

**3. Open the report** that is printed at the end:

```
Report:       ...\vic3-save-analyzer\reports\run_A_vs_run_B.html
```

That's it. Only one run? Leave the second folder out:

```bash
python compare.py saves/run_A
```

## Input and output

| | Where | What |
|---|---|---|
| **Input** | `saves/<run>/` (any folder works) | `.v3` save files of one game, any number. Yearly autosaves are a good choice: small, fast, enough detail. Monthly also works. |
| **Output** | `reports/<A>_vs_<B>.html` or `reports/<A>.html` | The finished report. Open it in a browser. |
| Details | `outputs/` | The data behind every report (SQLite databases you can open with [DB Browser for SQLite](https://sqlitebrowser.org/), Python or R) and a log of each step. `outputs/INDEX.md` lists the latest runs. You can delete this folder at any time. |

Your save files are only read, never changed.

## Options

```bash
python compare.py saves/run_A saves/run_B --label-a Vanilla --label-b "My AI mod"   # names shown in the report
python compare.py saves/run_A saves/run_B --limit 3     # quick test: only the first 3 saves of each run
python compare.py saves/run_A saves/run_B --open        # open the report in the browser when done
python compare.py saves/run_A saves/run_B --game "D:/SteamLibrary/steamapps/common/Victoria 3/game"
python compare.py saves/run_A saves/run_B --workers 2   # read fewer saves at once (less memory)
python compare.py --help
```

**Tip: test small first.** Run with `--limit 3` to check everything works (about a minute), then run the full set.

## What's in the report

- **Countries** — GDP, population, building levels and employment of the great and major powers over time, with growth.
- **Sectors** — agriculture, extraction, manufacturing, services, … per country and for the whole world.
- **Building types** — how many levels of each building every country has, grouped like in the game.
- **Building economics** — sales, input costs, value added and wages per building type.
- **Production methods** — which production methods countries use, per production-method group.

When comparing two runs, a **View** switch at the top shows run A, run B, or **A vs B** together
(B drawn dashed or lighter). The two runs are compared on the **in-game dates both have**: saves on other dates are
left out. So for a fair comparison, start both runs from the same date and use the same autosave interval.

## How long does it take?

Reading one save takes about 15–30 seconds and up to ~2 GB of memory; several saves are read at once if your PC
has the memory. 50 yearly saves per run: roughly 5–10 minutes per run. A run that has already been read is
reused, so comparing run A with a new run C later only reads C.

## Problems?

| Message | What to do |
|---|---|
| `'python' is not recognized` | Install Python and tick *Add to PATH*, or use `py` instead of `python`. |
| `no .v3 save files in ...` | Check the folder path; the saves must end in `.v3`. |
| `the two series share 0 save date(s)` | The two runs have no in-game dates in common. Use saves from runs that started on the same date with the same autosave interval. |
| `not a Victoria 3 save` / `save type ... is not binary` | The file is a manual or debug-mode save in another format. Use normal autosaves. |
| `NOTE: game folder not found` | Only affects names. Add `--game "<your Victoria 3 folder>/game"`. |
| `WARNING: no token dictionary for game x.y` | Your saves are from another game version. The tool still tries with the closest dictionary; some values may be missing or wrong. |
| Out of memory / PC very slow | Use `--workers 1`. |

## Notes on accuracy

Save files store data as numbered fields without names. The field names used here are the game's own,
read from a debug-mode text save of version 1.15.1. Most values (GDP, population, building levels, employment,
sales) are direct copies of what the game saved; a few report columns are derived (for example value added and
wages). Saves from other game versions may number fields differently.

This is a fan-made tool, not affiliated with Paradox Interactive.
