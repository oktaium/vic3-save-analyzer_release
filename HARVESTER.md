# Save harvester — tutorial

Victoria 3 keeps only the last few autosaves (`autosave.v3`, `autosave_1.v3` … `autosave_4.v3`) and overwrites
older ones. The harvester starts the game, lets it run at full speed, and **copies every new autosave** into its
own folder, so you end up with a complete series for `compare.py`.

Windows only. Nothing to install (it uses PowerShell, which is part of Windows).

## Before the first run (once)

1. **Set the autosave interval.** In Victoria 3: *Settings → Gameplay → Autosave* (wording may differ by version).
   **Yearly** gives one save per in-game year — recommended. Monthly works too, but makes 12× more files
   (each save is 20–40 MB).
2. **Game in another folder?** If Victoria 3 is not installed in
   `C:\Program Files (x86)\Steam\steamapps\common\Victoria 3`, open `harvester\harvest.bat` in Notepad and change
   the `GAME_BIN` line to your `Victoria 3\binaries` folder.
3. **Power mode:** set Windows power mode to *Best performance* — the game runs about twice as fast.
4. **Make a start save.** Start or load the game you want to record (for example an observer game, or any
   campaign at its start date), save it, and quit the game. The harvester continues from this save.

## Run it

1. Close Victoria 3 if it is running.
2. Double-click **`harvester\harvest.bat`** and answer three questions:

   ```
   Run name (folder for the saves, e.g. my_run): run_A
   Stop after how many autosaves? (Enter = until you close the game): 50
   Start save - drag a .v3 file here, or Enter = your newest save:
   ```

   *Run name* becomes the folder `saves\run_A\`. With yearly autosaves, `50` means 50 in-game years.
   For the start save, drag your save file from Explorer into the window, or press Enter to use your newest save.
3. The game starts (sound off) and loads the save. **Leave the window visible** until the script says
   *"window minimized"*.
4. The script tries to unpause and set speed 5 by itself. **Look at the game window once:** if the in-game date
   is **not** moving, click the window and press **Space** (unpause), then **5** (max speed).
   If the date is moving, do nothing — pressing Space would pause it again.
5. The script beeps if no autosave has appeared after 15 minutes. Check the window as in step 4.
6. From now on it runs by itself. Each copied save is shown:

   ```
   [14:02:31] Saved #1 (24.3 MB)
   [14:08:10] Saved #2 (24.9 MB)
   ```

7. After the number of saves you asked for, the game is closed automatically. To stop earlier, just close the
   harvester window (and the game); everything copied so far stays.

## Result

```
saves\run_A\
  000001__autosave.v3
  000002__autosave.v3
  ...
  manifest.csv          one line per save: when it was copied, size, checksum
```

Then make the report (see [README.md](README.md)):

```bash
python compare.py saves/run_A saves/run_B
```

## Good to know

- **Two runs to compare** should start from the same start save (or the same start date) and use the same autosave
  interval, so they have the same in-game dates.
- **Continue a run later:** run the harvester again with the same run name. New saves are added after the existing
  ones (numbering continues). Use the run's last save as the start save.
- **Game already running?** Answer the start-save question with `attach`: the harvester then only collects the
  autosaves of the game that is open.
- **Command line:** `harvester\harvest.bat <run_name> [saves] [start_save.v3 | attach]`, for example
  `harvester\harvest.bat run_A 50 "C:\saves\start.v3"`.
- Minimizing the game doesn't slow it down; the simulation speed depends on your CPU.
- The harvester writes two small files in `Documents\Paradox Interactive\Victoria 3\`: `save games\run_start.v3`
  (a copy of your start save) and `continue_game.json` (tells the game which save to load).
