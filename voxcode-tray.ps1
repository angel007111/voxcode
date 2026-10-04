# помощник в трее: запускает помощника в отдельном окне Windows Terminal
# и прячет в трей только это окно (при сворачивании).
param([switch]$Minimized)
$Root = $PSScriptRoot

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class W {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string c, string t);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@

$Title = 'VoxCode Core'
# Второй экземпляр трея не запускаем
$mutex = New-Object System.Threading.Mutex($false, 'Global\VoxCodeTray')
if (-not $mutex.WaitOne(0)) { exit }

function Find-Core { [W]::FindWindow('CASCADIA_HOSTING_WINDOW_CLASS', $Title) }

# Сессия всегда продолжается: ID сохраняет хук SessionStart (save-session.ps1) в session.id.
# Есть ID и его транскрипт — --resume именно её, иначе --continue (последняя в папке).
# Начать с чистого листа — /clear в самой сессии (хук запишет новый ID).
# -Hidden: окно сразу в трей (видно только на миг, когда жмём Enter)
function Start-Core([switch]$Hidden) {
  $cmd = 'claude --dangerously-load-development-channels server:voxcode'
  # Telegram — если плагин настроен (/telegram:configure)
  if (Test-Path "$env:USERPROFILE\.claude\channels\telegram\.env") { $cmd = $cmd.Replace('claude ', 'claude --channels plugin:telegram@claude-plugins-official ') }
  $sid = (Get-Content "$Root\session.id" -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($sid -and (Test-Path "$env:USERPROFILE\.claude\projects\$(($Root -replace '[^A-Za-z0-9]', '-'))\$($sid.Trim()).jsonl")) { $cmd += " --resume $($sid.Trim())" }
  else { $cmd += ' --continue' }
  Start-Process "$env:LOCALAPPDATA\Microsoft\WindowsApps\wt.exe" -ArgumentList @(
    '-w', 'voxcode', '--title', "`"$Title`"", '--suppressApplicationTitle',
    '-d', $Root,
    'powershell', '-NoExit', '-Command', $cmd
  )
  $h = [IntPtr]::Zero
  for ($i = 0; $i -lt 60 -and $h -eq [IntPtr]::Zero; $i++) { Start-Sleep -Milliseconds 100; $h = Find-Core }
  if ($h -ne [IntPtr]::Zero) {
    if ($Hidden) { [W]::ShowWindow($h, 0) | Out-Null }
    Confirm-DevChannel $h -Hidden:$Hidden
  }
  return $h
}

# На старте Claude показывает предупреждение про development channel;
# первый пункт — «I am using this for local development», Enter его выбирает.
# Лишний Enter в пустую строку ввода безвреден. Жмём только если активно окно помощника.
# SendKeys требует активного окна, поэтому при -Hidden показываем его на миг и прячем обратно.
function Confirm-DevChannel($h, [switch]$Hidden) {
  $ws = New-Object -ComObject WScript.Shell
  foreach ($delay in 5, 4) {
    Start-Sleep -Seconds $delay
    [W]::ShowWindow($h, 9) | Out-Null
    [W]::SetForegroundWindow($h) | Out-Null
    Start-Sleep -Milliseconds 150
    if ([W]::GetForegroundWindow() -eq $h) { $ws.SendKeys('{ENTER}') }
    if ($Hidden) { Start-Sleep -Milliseconds 100; [W]::ShowWindow($h, 0) | Out-Null }
  }
}

# Панель помощника (voice\voxcode_app.py) — главное окно, в ней же слушатель микрофона.
# Повторный запуск панели просто показывает её окно. Запущена ли — смотрим по её порту
# (CommandLine панели, запущенной от администратора, отсюда не видно).
. "$Root\panel.ps1"
# quit.flag ставит панель кнопкой «⏻ Закрыть всё»: трей закрывает сессию и выходит сам
$QuitFlag = "$Root\quit.flag"
Remove-Item $QuitFlag -ErrorAction SilentlyContinue
if (-not (Test-Panel)) { Start-Panel }

$hwnd = Find-Core
if ($hwnd -eq [IntPtr]::Zero) {
  $hwnd = Start-Core -Hidden:$Minimized
  if ($hwnd -eq [IntPtr]::Zero) { exit 1 }
}

$icon = New-Object System.Windows.Forms.NotifyIcon
$ico = "$Root\assets\voxcode.ico"
$icon.Icon = if (Test-Path $ico) { New-Object System.Drawing.Icon($ico, 32, 32) } else { [System.Drawing.Icon]::ExtractAssociatedIcon("$env:SystemRoot\System32\cmd.exe") }
$icon.Text = 'VoxCode'
$icon.Visible = $true

function Show-Core { [W]::ShowWindow($hwnd, 9) | Out-Null; [W]::SetForegroundWindow($hwnd) | Out-Null }
function Hide-Core { [W]::ShowWindow($hwnd, 0) | Out-Null }

# Закрыть всё: сессию Claude (окно «VoxCode Core»), панель и сам трей
function Close-All {
  $script:stopping = $true
  Remove-Item $QuitFlag -ErrorAction SilentlyContinue
  [W]::PostMessage($script:hwnd, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null  # WM_CLOSE
  Stop-Panel | Out-Null
  $icon.Visible = $false; [System.Windows.Forms.Application]::Exit()
}

# Всё управление — в панели (терминал открывается оттуда); в трее только открыть и закрыть
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$menu.Items.Add('Открыть панель', $null, { Start-Panel }) | Out-Null
$menu.Items.Add('-') | Out-Null
$menu.Items.Add('Закрыть всё', $null, {
  $r = [System.Windows.Forms.MessageBox]::Show('Закрыть VoxCode полностью: панель и терминал? Бот в Telegram перестанет отвечать.', 'VoxCode', 'YesNo', 'Question')
  if ($r -eq 'Yes') {
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$Root\compact.ps1" -Check
    if ($LASTEXITCODE -eq 10) {
      $icon.ShowBalloonTip(3000, 'VoxCode', 'Сжимаю контекст сессии и закрываюсь…', 'Info')
      & powershell -NoProfile -ExecutionPolicy Bypass -File "$Root\compact.ps1"
    }
    Close-All
  }
}) | Out-Null
$icon.ContextMenuStrip = $menu
$icon.add_MouseClick({ param($s, $e) if ($e.Button -eq 'Left') { Start-Panel } })

if ($Minimized) { Hide-Core }

# Свернули окно помощника — прячем в трей; закрыли крестиком — перезапускаем
$script:stopping = $false
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 500
$timer.add_Tick({
  # флаг проверяем раньше окна: панель ставит его до того, как закрыть терминал
  if (-not $script:stopping -and (Test-Path $QuitFlag)) { $timer.Stop(); Close-All; return }
  if (-not [W]::IsWindow($hwnd)) {
    if ($script:stopping) { return }
    $timer.Stop()
    # restart.flag ставит restart.ps1: помощник перезапускает себя сам — поднимаем скрыто
    $self = Test-Path "$Root\restart.flag"
    Remove-Item "$Root\restart.flag" -ErrorAction SilentlyContinue
    if (-not $self) { $icon.ShowBalloonTip(3000, 'VoxCode', 'Окно закрыто — перезапускаю с продолжением диалога', 'Info') }
    $script:hwnd = Start-Core -Hidden:$self
    if ($script:hwnd -eq [IntPtr]::Zero) { $icon.Visible = $false; [System.Windows.Forms.Application]::Exit(); return }
    $timer.Start()
    return
  }
  if ([W]::IsWindowVisible($hwnd) -and [W]::IsIconic($hwnd)) { Hide-Core }
})
$timer.Start()

[System.Windows.Forms.Application]::Run()
$icon.Dispose()
