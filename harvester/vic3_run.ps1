<#
Victoria 3 run with autosave collection (started by harvest.bat).

Launches the game (sound off), loads a start save, unpauses at speed 5, minimizes the window,
then copies every new autosave into OutDir as NNNNNN__autosave.v3 and appends manifest.csv.

Usage:
  powershell -ExecutionPolicy Bypass -File vic3_run.ps1 -OutDir <folder> [-StartSave <file.v3>] [-Saves 100]
             [-GameBin <Victoria 3\binaries>] [-KeepRunning] [-Attach] [-FirstWait 900] [-GameVersion 1.15.1]

  -StartSave   save to continue from (copied into "save games\run_start.v3"); default: newest save in "save games"
  -Saves       stop after this many autosaves (0 = until the game is closed)
  -GameBin     folder with victoria3.exe (default: Steam location)
  -KeepRunning don't close the game when -Saves is reached
  -Attach      game already running and loaded: just collect its autosaves
  -GameVersion game version written to continue_game.json (default 1.15.1)
  -FirstWait   seconds to wait for the first autosave before asking for help (yearly autosaves take a while)
Stop any time with Ctrl+C or by closing the window; files already copied stay.
#>
param(
    [Parameter(Mandatory)] [string]$OutDir,
    [string]$StartSave,
    [int]$Saves = 0,
    [string]$GameBin = 'C:\Program Files (x86)\Steam\steamapps\common\Victoria 3\binaries',
    [switch]$KeepRunning,
    [switch]$Attach,
    [int]$FirstWait = 900,
    [string]$GameVersion = '1.15.1'   # written to continue_game.json (as the game writes it)
)
$ErrorActionPreference = 'Stop'
$UserDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Paradox Interactive\Victoria 3'
$SaveDir = Join-Path $UserDir 'save games'   # note: space, not underscore

Add-Type @"
using System; using System.Runtime.InteropServices;
public class V3Win {
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h,uint m,IntPtr w,IntPtr l);
  [DllImport("user32.dll")] static extern uint MapVirtualKey(uint c,uint t);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int c);
  public static void Press(IntPtr h,uint vk){ uint sc=MapVirtualKey(vk,0);
    PostMessage(h,0x0100,(IntPtr)vk,(IntPtr)(1|(sc<<16))); System.Threading.Thread.Sleep(80);
    PostMessage(h,0x0101,(IntPtr)vk,(IntPtr)(1|(sc<<16)|(3u<<30))); }
}
"@

function Log($m) { Write-Host "[$(Get-Date -Format HH:mm:ss)] $m" }

New-Item -ItemType Directory -Force $SaveDir, $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
Log "Saves will be copied to $OutDir"

if ($Attach) {
    $p = Get-Process victoria3 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $p) { throw 'No running Victoria 3 to attach to.' }
    Log "Attached to running game (pid $($p.Id))"
} else {
    if (Get-Process victoria3 -ErrorAction SilentlyContinue) { throw 'Victoria 3 is already running; close it first (or use attach).' }
    $exe = Join-Path $GameBin 'victoria3.exe'
    if (-not (Test-Path $exe)) { throw "victoria3.exe not found in $GameBin - edit GAME_BIN at the top of harvest.bat." }

    # 1. Start save + continue_game.json (what -continuelastsave reads)
    if ($StartSave) {
        if (-not (Test-Path $StartSave)) { throw "Start save not found: $StartSave" }
        Copy-Item $StartSave (Join-Path $SaveDir 'run_start.v3') -Force
        $name = 'run_start'
    } else {
        $latest = Get-ChildItem $SaveDir -Filter *.v3 | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $latest) { throw "No saves in $SaveDir; give a start save." }
        $name = $latest.BaseName
    }
    $json = @{ title = $name; desc = ''; date = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
               filename = "save games/$name.v3"; rawGameVersion = $GameVersion } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $UserDir 'continue_game.json'), $json)
    Log "Start save: $name.v3"

    # 2. Launch and wait until the save is loaded
    $launch = Get-Date
    $p = Start-Process $exe -WorkingDirectory $GameBin -ArgumentList '-gdpr-compliant','-nosounds','-continuelastsave' -PassThru
    Log "Game started; waiting for the save to load..."
    $stats = Join-Path $UserDir 'logs\custom_automated_stats.log'
    while ($true) {
        Start-Sleep 5
        if ($p.HasExited) { throw 'Game exited during loading.' }
        $f = Get-Item $stats -ErrorAction SilentlyContinue
        if ($f -and $f.LastWriteTime -gt $launch -and (Get-Content $stats -Raw) -match 'ingame-idler') { break }
        if (((Get-Date) - $launch).TotalMinutes -gt 10) { Log 'Still loading? Continuing anyway.'; break }
    }
    Log "In game after $([int]((Get-Date)-$launch).TotalSeconds)s; letting the game finish loading (60s, keep the window visible)"
    Start-Sleep 60
}

# 3. Unpause + speed 5 (keys sent to the window). Confirmed by the first autosave.
$p.Refresh(); $h = $p.MainWindowHandle
$src = Join-Path $SaveDir 'autosave.v3'
$before = if (Test-Path $src) { (Get-Item $src).LastWriteTime } else { [datetime]::MinValue }
function WaitAutosave($secs) {
    $deadline = if ($secs) { (Get-Date).AddSeconds($secs) } else { [datetime]::MaxValue }
    while ((Get-Date) -lt $deadline) {
        Start-Sleep 5
        if ($p.HasExited) { throw 'Game exited.' }
        if ((Test-Path $src) -and (Get-Item $src).LastWriteTime -gt $before) { return $true }
    }
    return $false
}
$running = $false
if (-not $Attach) {
    # one automatic try; Space toggles pause, so it is sent only once
    [V3Win]::ShowWindow($h, 9) | Out-Null; Start-Sleep 2
    [V3Win]::Press($h, 0x20); Start-Sleep 1; [V3Win]::Press($h, 0x35)
    Log "Sent Space + 5. Waiting for the first autosave (up to $([int]($FirstWait/60)) min)."
    Log 'Look at the game window: if the in-game date is NOT moving, click the window, press SPACE and then 5.'
    $running = WaitAutosave $FirstWait
}
if (-not $running) {
    [V3Win]::ShowWindow($h, 9) | Out-Null
    [console]::Beep(800, 400)
    Log '>>> CHECK THE GAME: if the date is not moving, click the Victoria 3 window, press SPACE (unpause) and 5 (max speed).'
    Log '    If it is moving, do nothing. Waiting for the first autosave...'
    $running = WaitAutosave 0
}
[V3Win]::ShowWindow($h, 6) | Out-Null
Log 'Game is running; window minimized. Collecting autosaves (close this window to stop).'

# 4. Collect autosaves
$manifest = Join-Path $OutDir 'manifest.csv'
if (-not (Test-Path $manifest)) {
    'seq,copied_at,source_mtime,size_bytes,sha1,archive_file' | Set-Content $manifest -Encoding ascii
}
$seq = @(Import-Csv $manifest).Count
$lastSeen = $before   # so the autosave that confirmed the run is collected too
$collected = 0
while (-not $p.HasExited) {
    Start-Sleep 2
    # autosave.v3 briefly disappears while the game renames it to autosave_1..4: skip that check
    $f = Get-Item $src -ErrorAction SilentlyContinue
    if (-not $f -or $f.LastWriteTime -le $lastSeen) { continue }
    # wait until the game has finished writing
    do { $size = $f.Length; Start-Sleep 2; $f = $null; while (-not $f) { $f = Get-Item $src -ErrorAction SilentlyContinue; if (-not $f) { Start-Sleep 1 } } } while ($f.Length -ne $size -or $f.LastWriteTime -gt (Get-Date).AddSeconds(-2))
    $lastSeen = $f.LastWriteTime
    $seq++
    $dest = '{0:D6}__autosave.v3' -f $seq
    Copy-Item $src (Join-Path $OutDir $dest)
    $sha1 = (Get-FileHash (Join-Path $OutDir $dest) -Algorithm SHA1).Hash.ToLower()
    "$seq,$(Get-Date -Format s),$($f.LastWriteTime.ToString('s')),$($f.Length),$sha1,$dest" | Add-Content $manifest -Encoding ascii
    $collected++
    Log "Saved #$seq ($([math]::Round($f.Length/1MB,1)) MB)"
    if ($Saves -gt 0 -and $collected -ge $Saves) {
        if (-not $KeepRunning) { Stop-Process -Id $p.Id -Force; Log 'Target reached; game closed.' }
        else { Log 'Target reached; game left running.' }
        break
    }
}
Log "Done: $collected autosaves in $OutDir"
