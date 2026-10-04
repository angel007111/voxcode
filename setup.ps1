# Установка VoxCode: имя помощника, ключи, зависимости. Повторный запуск безопасен —
# покажет текущие значения, Enter оставляет как есть.
#   powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Voice = "$Root\voice"
$utf8 = New-Object Text.UTF8Encoding $false

function Ask($q, $def) {
  $a = Read-Host "$q$(if ($def) { " [$def]" })"
  if ([string]::IsNullOrWhiteSpace($a)) { $def } else { $a.Trim() }
}
function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "`n=== VoxCode: установка ===`n" -ForegroundColor Yellow

# 1. Что нужно заранее
$miss = @()
foreach ($c in 'claude', 'python', 'bun', 'wt') { if (-not (Has $c)) { $miss += $c } }
if ($miss) {
  Write-Host "Не найдено: $($miss -join ', ')" -ForegroundColor Red
  Write-Host '  claude — https://claude.com/claude-code   python 3.11+ — python.org'
  Write-Host '  bun — https://bun.sh                      wt — Windows Terminal из Microsoft Store'
  if ((Ask 'Продолжить без них? (y/n)' 'n') -ne 'y') { exit 1 }
}

# 2. Имя помощника и слово-активатор
$cfgFile = "$Voice\assistant.json"
$cur = if (Test-Path $cfgFile) { Get-Content $cfgFile -Raw -Encoding utf8 | ConvertFrom-Json } else { $null }
# имя обязательно: на него помощник откликается и так себя называет
do { $name = Ask 'Как зовут помощника (на это имя он будет откликаться)' $(if ($cur) { $cur.name }) } while (-not $name)
# имя целиком и с окончаниями («Джарвис», «Джарвиса»); варианты — как его может услышать распознавание
$wake = @([regex]::Escape($name.ToLower()) + '\w*')
$extra = Ask 'Как ещё распознавание может услышать имя (через запятую, например латиницей; можно пусто)' ''
if ($extra) { $wake += $extra.Split(',') | ForEach-Object { [regex]::Escape($_.Trim().ToLower()) + '\w*' } | Where-Object { $_ -ne '\w*' } }
[IO.File]::WriteAllText($cfgFile, (@{ name = $name; wake = $wake } | ConvertTo-Json), $utf8)
# характер и правила — из шаблона, с новым именем
$tpl = [IO.File]::ReadAllText("$Root\CLAUDE.template.md", [Text.Encoding]::UTF8)
[IO.File]::WriteAllText("$Root\CLAUDE.md", $tpl.Replace('{{NAME}}', $name), $utf8)
Write-Host "  имя: $name (assistant.json, CLAUDE.md)" -ForegroundColor Green

# 3. Голос: Google Cloud TTS (необязательно, иначе бесплатный edge-tts)
$keyFile = "$Voice\.google_tts_key"
Write-Host "`nГолос Google Cloud TTS (лучше звучит). Нужен API-ключ с доступом к Cloud Text-to-Speech API:"
Write-Host '  console.cloud.google.com → APIs & Services → включить Cloud Text-to-Speech API →'
Write-Host '  Credentials → Create credentials → API key → ограничить ключ этим API.'
$has = Test-Path $keyFile
$key = Read-Host "Ключ$(if ($has) { ' (Enter — оставить сохранённый)' } else { ' (Enter — без Google, будет edge-tts)' })"
if ($key) { [IO.File]::WriteAllText($keyFile, $key.Trim(), $utf8); Write-Host '  ключ сохранён в voice\.google_tts_key' -ForegroundColor Green }

# 4. Telegram (необязательно): плагин ставится в самом Claude Code
Write-Host "`nTelegram: в Claude Code выполни /plugin install telegram@claude-plugins-official и /telegram:configure."
$chat = Ask 'Твой chat_id для сообщений о сбоях (Enter — не нужно)' $(if (Test-Path "$Root\tg-chat.txt") { (Get-Content "$Root\tg-chat.txt" -TotalCount 1) })
if ($chat) { [IO.File]::WriteAllText("$Root\tg-chat.txt", $chat, $utf8) }

# 5. Зависимости
if ((Ask "`nПоставить зависимости Python и Bun сейчас? (y/n)" 'y') -eq 'y') {
  if (Has 'python') { python -m pip install -r "$Root\requirements.txt" }
  if (Has 'bun') { Push-Location $Voice; bun install; Pop-Location }
}

# 6. Claude: ключ не нужен — работает по подписке, нужно один раз войти
Write-Host "`nClaude работает по подписке (Pro/Max), API-ключ не нужен." -ForegroundColor Yellow
Write-Host "Если ещё не входил: открой терминал в $Root, запусти claude и войди (/login)."

Write-Host "`nГотово. Запуск: двойной клик по voxcode.vbs." -ForegroundColor Green
Write-Host "Голос владельца $name запомнит сам после 3 команд.`n"
