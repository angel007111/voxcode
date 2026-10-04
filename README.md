# VoxCode — голосовая оболочка для Claude Code

*Персонаж по умолчанию — «Зевс» (Zews): имя и характер меняются в `CLAUDE.md`, слово-активатор — `WAKE` в `voice/listener.py`.*

Голосовая панель для [Claude Code](https://claude.com/claude-code) под Windows: говоришь «Зевс, …» —
сессия Claude Code получает команду через канал, выполняет её и отвечает голосом. Работает по подписке
Claude (Pro/Max), без API-ключа: это обычная сессия Claude Code, к которой подключён голосовой канал.

*English summary below.*

## Что умеет
- **Слово «Зевс»** и **диктовка**: короткие просьбы уходят сразу после паузы, длинные — по «выполняй».
- **Распознавание** локально: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CUDA, если есть).
- **Озвучка**: Google Cloud TTS (если положить ключ) или бесплатный edge-tts.
- **Перебивание**: пока Зевс говорит, можно просто заговорить — панель узнаёт голос владельца
  (SpeechBrain ECAPA) и ставит речь на паузу; своё эхо из колонок она отличает. Промолчал — договаривает.
- **Живой диалог**: мгновенные реплики по смыслу («сейчас посмотрим», «записываю»), приветствие по времени суток.
- **Панель** (pywebview): сфера-индикатор, лента диалога, быстрые кнопки, варианты ответа кнопками,
  настройки микрофона с калибровкой.
- **Трей**: держит сессию Claude в скрытом окне терминала, поднимает её после падения с продолжением диалога
  (`--resume`), перед перезапуском и выходом сжимает контекст (`/compact`), если он заполнен на 60%+.

## Как устроено
```
микрофон → voice/listener.py (VAD, Whisper, «Зевс», голос владельца)
         → HTTP 127.0.0.1:8790 → voice/channel.ts (MCP-сервер, канал claude/channel)
         → сессия Claude Code → инструмент say → channel.ts → listener.py → колонки
voice/zews_app.py — панель (окно + ядро listener), zews-tray.ps1 — трей и сторож сессии
```

## Установка (Windows 10/11)
1. Нужны: [Claude Code](https://claude.com/claude-code), Python 3.11+, [Bun](https://bun.sh), Windows Terminal.
2. Установщик — спросит имя помощника, ключ Google для голоса, Telegram и поставит зависимости:
   ```
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```
   - **Имя** — на него помощник откликается (слово-активатор) и так себя называет (`CLAUDE.md` из
     `CLAUDE.template.md`). Хранится в `voice/assistant.json`; по умолчанию «Зевс».
   - **Google Cloud TTS** (необязательно): API-ключ с доступом к Cloud Text-to-Speech API → `voice/.google_tts_key`.
     Без ключа — бесплатный edge-tts.
   - **Claude**: ключ не нужен — работает по подписке; достаточно один раз войти в `claude`.
   - **Telegram** (необязательно): плагин `telegram@claude-plugins-official`; chat_id для сообщений о сбоях — `tg-chat.txt`.
3. Запуск: двойной клик по `zews.vbs` (можно положить ярлык в автозагрузку).
   Первый запуск Claude спросит про development channel — трей подтверждает сам.

Голос владельца запоминается сам: после 3 принятых команд в панели появится «Запомнил твой голос».
Начать заново — удалить `voice/owner_voice.npy`.

## Настройки (`voice/config.json`, создаётся панелью)
| ключ | по умолчанию | что |
|---|---|---|
| `barge_in` | `true` | перебивание во время речи |
| `voice_threshold` | `0.15` | минимальная похожесть на голос владельца |
| `voice_margin` | `0.1` | насколько звук ближе к владельцу, чем к голосу Зевса |
| `fillers` | `true` | реплики «угу / секунду» |
| `echo_tail` | `1.5` | сколько секунд после речи микрофон глух (эхо) |

Переменные окружения: `ZEWS_LISTEN_MODEL` (Whisper, по умолчанию `large-v3-turbo`), `ZEWS_GOOGLE_VOICE`,
`ZEWS_VOICE`, `ZEWS_GOOGLE_TTS_KEY`, `ZEWS_TG_CHAT`.

## Безопасность
Канал слушает только `127.0.0.1` и требует общий токен (`voice/.token`, создаётся сам), так что веб-страницы
команду не подсунут. Микрофон может услышать чужую речь — опасные действия Зевс должен подтверждать отдельно
(правила в `CLAUDE.md`).

## Лицензия
MIT

---

## English summary
**VoxCode** (default persona "Zews") is a Windows voice front-end for Claude Code: a floating panel with local speech recognition
(faster-whisper), wake word, dictation, TTS (Google Cloud TTS or edge-tts), barge-in by the owner's voice
(SpeechBrain speaker verification that tells your voice from the speaker echo), and a tray watchdog that
keeps a hidden Claude Code session alive with `--resume` and `/compact`. Commands reach the session through
a custom MCP channel (`voice/channel.ts`); replies come back through its `say` tool. Works on a regular
Claude subscription — no API key. The UI and prompts are in Russian.
