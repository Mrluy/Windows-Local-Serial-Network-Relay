$ErrorActionPreference = "Stop"

python -m pip install -r .\requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pip install -r .\build-requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$iconIco = Join-Path ([System.IO.Path]::GetTempPath()) "SerialTcpRelay-app.ico"

@"
from pathlib import Path
from PIL import Image
import sys

source = Path("img") / "app.png"
target = Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)

image = Image.open(source).convert("RGBA")
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
image.save(target, format="ICO", sizes=sizes)
"@ | python - "$iconIco"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name SerialTcpRelay `
  --icon $iconIco `
  --version-file ".\version_info.txt" `
  --add-data "img\app.png;img" `
  --hidden-import pystray._win32 `
  --hidden-import serial.tools.list_ports_windows `
  --hidden-import serial.tools.list_ports_common `
  .\serial_tcp_relay_gui.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$sourceExe = ".\dist\SerialTcpRelay.exe"
$targetName = [string]::Concat(
  [char]0x672C,
  [char]0x5730,
  [char]0x4E32,
  [char]0x53E3,
  [char]0x7F51,
  [char]0x7EDC,
  [char]0x4E2D,
  [char]0x7EE7,
  ".exe"
)
$targetExe = Join-Path ".\dist" $targetName
if (Test-Path $targetExe) {
  Remove-Item -LiteralPath $targetExe -Force
}
Move-Item -LiteralPath $sourceExe -Destination $targetExe -Force

Write-Host ""
Write-Host "Build complete: $((Resolve-Path $targetExe).Path)"
