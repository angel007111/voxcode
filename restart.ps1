# Перезапуск Зевса без участия владельца.
#   restart.ps1            — перезапустить панель и сессию Зевса
#   restart.ps1 -Panel     — только панель (voice\zews_app.py)
#   restart.ps1 -Core      — только сессию Claude (окно «Zews Core»)
# Зевс вызывает его сам, отвязанным процессом:
#   Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<папка проекта>\restart.ps1'
# Сессию закрываем через WM_CLOSE, а трей сам поднимает её с --resume (ID из session.id) (скрыто — по флагу restart.flag).
# Перед закрытием сессии контекст сжимается (/compact, см. compact.ps1).
# В Telegram пишет только о сбоях (сессия не поднялась, старая панель не закрылась).
param([switch]$Panel, [switch]$Core)
if (-not $Panel -and -not $Core) { $Panel = $true; $Core = $true }

Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class WR {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string c, string t);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
}
'@

$Root = $PSScriptRoot
$Log = "$Root\restart.log"
function Log($m) { Add-Content -Encoding utf8 $Log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" }

# chat_id для сообщений о сбоях: переменная ZEWS_TG_CHAT или файл tg-chat.txt рядом; нет — не пишем
$TgChat = if ($env:ZEWS_TG_CHAT) { $env:ZEWS_TG_CHAT } else { (Get-Content "$Root\tg-chat.txt" -ErrorAction SilentlyContinue | Select-Object -First 1) }
function Send-Tg($text) {
  if (-not $TgChat) { return }
  $tok = (Get-Content "$env:USERPROFILE\.claude\channels\telegram\.env" | Where-Object { $_ -like 'TELEGRAM_BOT_TOKEN=*' }) -replace '^TELEGRAM_BOT_TOKEN=', '' -replace '"', ''
  $body = [Text.Encoding]::UTF8.GetBytes((@{ chat_id = $TgChat.Trim(); text = $text } | ConvertTo-Json))
  try { Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$($tok.Trim())/sendMessage" -ContentType 'application/json; charset=utf-8' -Body $body | Out-Null }
  catch { Log "telegram: $_" }
}

function Test-Channel {
  $t = (Get-Content "$Root\voice\.token" -ErrorAction SilentlyContinue | Select-Object -First 1)
  try { (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8790/ping' -Headers @{ 'X-Zews-Token' = "$t" } -TimeoutSec 2).StatusCode -eq 200 }
  catch { $false }
}

# Сессия Зевса — claude.exe с каналом zews-voice (по процессу надёжнее, чем по заголовку окна)
function Get-Core {
  Get-CimInstance Win32_Process -Filter "Name='claude.exe'" |
    Where-Object { $_.CommandLine -match 'server:zews-voice' } | Select-Object -First 1
}

Start-Sleep -Seconds 3  # дать Зевсу закончить ответ, который запустил перезапуск

. "$Root\panel.ps1"
$panelOk = $true
if ($Panel) {
  Log 'панель: перезапуск'
  if (Stop-Panel) { Start-Panel }
  else {
    # новая копия только показала бы окно старой — не запускаем
    $panelOk = $false
    Log 'панель: старая не закрылась'
    Send-Tg '⚠️ Старая панель Зевса не закрылась (видимо, запущена от администратора). Сними pythonw.exe в диспетчере задач — дальше перезапуск будет работать сам.'
  }
}

if ($Core) {
  Log 'сессия: перезапуск'
  & "$Root\compact.ps1"  # сжать контекст перед закрытием
  New-Item -ItemType File -Force "$Root\restart.flag" | Out-Null
  $old = Get-Core
  $h = [WR]::FindWindow('CASCADIA_HOSTING_WINDOW_CLASS', 'Zews Core')
  if ($h -ne [IntPtr]::Zero) { [WR]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null }  # WM_CLOSE
  elseif ($old) {
    # заголовок окна сменился — ищем сессию по процессу и закрываем её оболочку (вкладка закроется, трей поднимет заново)
    Log "окна «Zews Core» нет — закрываю сессию по процессу $($old.ProcessId)"
    taskkill /PID $old.ParentProcessId /T /F | Out-Null
  }
  Start-Sleep -Seconds 15
  # трей не запущен — некому поднять сессию: запускаем трей сами
  if (-not (Get-Core)) {
    Log 'сессии нет — запускаю трей'
    Remove-Item "$Root\restart.flag" -ErrorAction SilentlyContinue
    Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "$Root\zews-tray.ps1", '-Minimized'
  }
  # ждём именно новую сессию: старая тоже отвечает на ping, пока жива
  $isNew = { $c = Get-Core; $c -and (-not $old -or $c.ProcessId -ne $old.ProcessId) }
  for ($i = 0; $i -lt 90 -and -not ((& $isNew) -and (Test-Channel)); $i++) { Start-Sleep -Seconds 1 }
  # в Telegram — только когда что-то сломалось (об успешных перезапусках владелец просил не писать)
  if ((& $isNew) -and (Test-Channel)) { Log "сессия поднялась (PID $((Get-Core).ProcessId))" }
  else { Log 'сессия не поднялась за 90 с'; Send-Tg '⚠️ Зевс не поднялся после перезапуска за 1.5 минуты. Загляни в окно «Zews Core».' }
}
