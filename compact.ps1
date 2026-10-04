# Сжать контекст сессии помощника (/compact) перед перезапуском или выходом.
# Вставляем «/compact» в окно «VoxCode Core» и ждём compact.done от хука save-session.ps1.
# Вставка через буфер и виртуальные клавиши — от раскладки не зависит.
# Если помощник занят, команда встанет в очередь и выполнится после его ответа — поэтому ждём до 3 мин.
# Вызывают restart.ps1, панель (⏻) и трей («Закрыть всё»).
# -Check: ничего не делать, только ответить кодом выхода: 10 — сжатие нужно, 0 — нет
# (панель и трей по нему решают, показывать ли «Сжимаю контекст…»).
param([switch]$Check)
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class WC {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string c, string t);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
}
'@

$Root = $PSScriptRoot
function Log($m) { Add-Content -Encoding utf8 "$Root\restart.log" "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" }
function Key($vk, $up) { [WC]::keybd_event($vk, 0, $(if ($up) { 2 } else { 0 }), [UIntPtr]::Zero) }

$h = [WC]::FindWindow('CASCADIA_HOSTING_WINDOW_CLASS', 'VoxCode Core')
if ($h -eq [IntPtr]::Zero) { if (-not $Check) { Log 'сжатие: окна нет, пропускаю' }; exit 0 }

# Сжимаем, только если контекст заполнен на 60%+ (окно модели — 200k токенов).
# Заполненность берём из последнего ответа в транскрипте сессии: input + cache_creation + cache_read.
$Window = 200000; $Threshold = 0.6
$sid = (Get-Content "$Root\session.id" -ErrorAction SilentlyContinue | Select-Object -First 1)
$jsonl = if ($sid) { "$env:USERPROFILE\.claude\projects\$(($Root -replace '[^A-Za-z0-9]', '-'))\$($sid.Trim()).jsonl" }
if ($jsonl -and (Test-Path $jsonl)) {
  $last = Get-Content -Encoding utf8 $jsonl -Tail 300 | Where-Object { $_ -match '"usage"' } | Select-Object -Last 1
  if ($last) {
    $u = ($last | ConvertFrom-Json).message.usage
    $used = [int]$u.input_tokens + [int]$u.cache_creation_input_tokens + [int]$u.cache_read_input_tokens
    $pct = [math]::Round($used * 100 / $Window)
    if ($used -lt $Window * $Threshold) { if (-not $Check) { Log "сжатие: контекст $pct% (< $($Threshold * 100)%), не нужно" }; exit 0 }
    if (-not $Check) { Log "сжатие: контекст $pct%, сжимаю" }
  }
}
if ($Check) { exit 10 }

$done = "$Root\compact.done"
Remove-Item $done -ErrorAction SilentlyContinue
$clip = [Windows.Forms.Clipboard]::GetText()
[Windows.Forms.Clipboard]::SetText('/compact')
$wasVisible = [WC]::IsWindowVisible($h)
[WC]::ShowWindow($h, 9) | Out-Null
[WC]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 300
$sent = [WC]::GetForegroundWindow() -eq $h
if ($sent) {
  Key 0x11 $false; Key 0x56 $false; Key 0x56 $true; Key 0x11 $true  # Ctrl+V
  Start-Sleep -Milliseconds 300
  Key 0x0D $false; Key 0x0D $true  # Enter
}
Start-Sleep -Milliseconds 300
if ($clip) { [Windows.Forms.Clipboard]::SetText($clip) } else { [Windows.Forms.Clipboard]::Clear() }
if (-not $wasVisible) { [WC]::ShowWindow($h, 0) | Out-Null }
if (-not $sent) { Log 'сжатие: окно не стало активным, пропускаю'; exit 0 }

Log 'сжатие: отправил /compact'
for ($i = 0; $i -lt 180 -and -not (Test-Path $done); $i++) { Start-Sleep -Seconds 1 }
if (Test-Path $done) { Log 'сжатие: готово'; Remove-Item $done }
else { Log 'сжатие: не дождался за 3 мин, закрываю как есть' }
