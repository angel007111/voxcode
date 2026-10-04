# Хук SessionStart: запоминает ID сессии Зевса в session.id, чтобы трей после выхода
# или перезапуска поднимал именно её (--resume), а не «последнюю в папке» (--continue).
# После /compact (source=compact) ставит compact.done — его ждёт restart.ps1 перед закрытием сессии.
# Пишем только для сессии Зевса (claude.exe с каналом zews-voice), а не для любой Claude в этой папке.
$in = [Console]::In.ReadToEnd() | ConvertFrom-Json
if (-not $in.session_id) { exit 0 }

$p = $PID
for ($i = 0; $i -lt 8 -and $p; $i++) {
  $x = Get-CimInstance Win32_Process -Filter "ProcessId=$p"
  if (-not $x) { break }
  if ($x.Name -eq 'claude.exe') {
    if ($x.CommandLine -match 'server:zews-voice') {
      Set-Content -NoNewline -Encoding ascii "$PSScriptRoot\session.id" $in.session_id
      if ($in.source -eq 'compact') { New-Item -ItemType File -Force "$PSScriptRoot\compact.done" | Out-Null }
    }
    break
  }
  $p = $x.ParentProcessId
}
exit 0
