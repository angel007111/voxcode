# Управление панелью Зевса (voice\zews_app.py). Подключается через dot-source из restart.ps1 и zews-tray.ps1.
# Панель держит порт 8791: «quit» по нему закрывает её, даже если она запущена от администратора
# (Stop-Process и чтение CommandLine такого процесса из обычных прав не работают).

$PanelPort = 8791
$PanelRoot = $PSScriptRoot
$PanelPid = "$PanelRoot\voice\panel.pid"

function Test-Panel { [bool](Get-NetTCPConnection -LocalPort $PanelPort -State Listen -ErrorAction SilentlyContinue) }

function Send-Panel($cmd) {
  try {
    $c = New-Object Net.Sockets.TcpClient('127.0.0.1', $PanelPort)
    $b = [Text.Encoding]::ASCII.GetBytes($cmd)
    $c.GetStream().Write($b, 0, $b.Length)
    $c.Close()
  } catch {}
}

function Stop-Panel {
  if (Test-Panel) { Send-Panel 'quit' }
  for ($i = 0; $i -lt 50 -and (Test-Panel); $i++) { Start-Sleep -Milliseconds 100 }
  if (-not (Test-Panel)) { return $true }
  # старая панель не знает «quit» — пробуем по PID и по командной строке
  $ids = @()
  if (Test-Path $PanelPid) { $ids += [int](Get-Content $PanelPid -TotalCount 1) }
  $ids += Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'listener\.py|zews_app\.py' } | ForEach-Object { $_.ProcessId }
  foreach ($id in ($ids | Select-Object -Unique)) {
    try { Stop-Process -Id $id -Force -ErrorAction Stop } catch {}
  }
  Start-Sleep -Seconds 1
  return -not (Test-Panel)
}

function Start-Panel {
  $py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
  if (-not $py) { $py = Join-Path (Split-Path (Get-Command python.exe).Source) 'pythonw.exe' }
  Start-Process $py -ArgumentList "`"$PanelRoot\voice\zews_app.py`"" -WorkingDirectory "$PanelRoot\voice"
}
