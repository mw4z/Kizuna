# Create Kizuna shortcuts (Start menu + Desktop) that open the status page as an app window (Edge app mode).
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge not found" }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in [Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop')) {
    # WScript.Shell can't save to non-ASCII paths (the Desktop is "سطح المكتب"): build in TEMP, then move
    $tmp = Join-Path $env:TEMP 'Kizuna.lnk'
    $lnk = $shell.CreateShortcut($tmp)
    $lnk.TargetPath = $edge
    $lnk.Arguments = '--app=http://localhost:8097/ --window-size=1180,900'
    $lnk.IconLocation = (Join-Path $here 'app.ico')
    $lnk.Description = 'Kizuna'
    $lnk.WorkingDirectory = $here
    $lnk.Save()
    $dest = Join-Path $dir 'Kizuna.lnk'
    Move-Item -LiteralPath $tmp -Destination $dest -Force
    "created $dest"
}
