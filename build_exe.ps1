$ErrorActionPreference = "Stop"

python -m pip install -r .\requirements.txt
python -m pip install -r .\build-requirements.txt

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name SerialTcpRelay `
  --hidden-import serial.tools.list_ports_windows `
  --hidden-import serial.tools.list_ports_common `
  .\serial_tcp_relay_gui.py

Write-Host ""
Write-Host "Build complete: $((Resolve-Path .\dist\SerialTcpRelay.exe).Path)"
