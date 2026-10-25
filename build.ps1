# Builds the installer: self-tests, the one-folder app (PyInstaller) into build\app, then Inno Setup.
# Output: dist\BarracudaBattery-Setup-<version>.exe (version in installer\BarracudaBattery.iss).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Check($step) { if ($LASTEXITCODE) { throw "$step failed ($LASTEXITCODE)" } }

python barracuda.py --selftest; Check 'barracuda selftest'
python history.py --selftest; Check 'history selftest'

python -m PyInstaller --noconfirm --onedir --windowed --name BarracudaBattery `
    --icon "$PSScriptRoot\assets\icon.ico" --add-data "$PSScriptRoot\ui;ui" `
    --exclude-module numpy --exclude-module customtkinter --exclude-module tkinter `
    --distpath build\app --workpath build\tmp --specpath build app.pyw
Check 'PyInstaller'

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 not found (winget install JRSoftware.InnoSetup)' }
& $iscc /Q installer\BarracudaBattery.iss; Check 'Inno Setup'
Get-ChildItem dist\BarracudaBattery-Setup-*.exe | Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }
