# dev-bot — Roadmap

Что планируется доделать. Сейчас бот живой и готов к ежедневной разработке;
всё ниже — улучшения качества жизни.

## 🟢 Done

- ✅ Single-user TG бот с whitelist по `user_id`
- ✅ Stateful `ClaudeSDKClient` + resume через рестарты
- ✅ Permission flow с inline-кнопками
- ✅ Smart Bash auto-approve (read-only commands)
- ✅ Auto-pull cwd перед каждой задачей
- ✅ `/menu` — quick-action inline keyboard (Status / Diff / Tests / Deploy / Roadmap / Logs)
- ✅ Image input — фото → Read tool → Claude видит
- ✅ `/help` объясняет сегментацию задач, картинки, smart bash
- ✅ Opus 4.7 + 1M context window
- ✅ systemd unit + `scripts/deploy.sh`

## 🔥 Planned next

### M1 — Утренний дайджест
**Цель:** в 09:00 локального времени бот сам пишет 5-7 строк:
- кол-во коммитов за вчера в `/opt/slot-hunter`
- задачи, изменившие статус в `notes/ROADMAP.md` (новые / закрытые)
- статус прод-API (`/healthz`)
- кол-во ERROR/WARN в `journalctl` за сутки

**Как:** APScheduler в процессе бота, cron `0 9 * * *` Europe/Minsk. Шлёт
сообщение через `bot.send_message(ALLOWED_USER_ID, ...)`.
**Срок:** ~1.5 часа.

### M2 — Журнал разговоров (`/history`)
**Цель:** не терять контекст когда делаешь `/reset`.
- Перед reset бот вызывает `/compact`, сохраняет результат в
  `state_dir/history/<chat>/<ts>.md` (короткое summary разговора).
- Команда `/history` — список последних 10 с превью (первая строка summary).
- Тап на запись → присылается полный summary в чат.
- Опц.: `/resume <id>` — открыть новую сессию с этим summary как
  system note (Claude знает «вот что обсуждали в прошлой сессии»).

**Как:** интегрировать в `cmd_reset()` + новый router. Файлы — обычный
markdown.
**Срок:** ~2 часа.

### M3 — Автономное тестирование Mini App (Playwright MCP)
**Цель:** Claude сам кликает, проверяет инварианты, читает консоль.

Без этого после каждого деплоя приходится самому открывать Mini App в
Telegram, тыкать wizard, смотреть что не сломалось. С этим — пишешь
боту «протестируй последний деплой» → он сам всё делает.

**Этапы:**

1. **Подключить Playwright MCP к Claude в dev-боте.** Конфиг через
   `mcp_servers={"playwright": {"command": "npx", "args": ["-y",
   "@playwright/mcp"]}}` в `ClaudeAgentOptions`. Tools `browser_*`
   станут доступны Claude через TG. ~2 часа.
2. **Dev-mode auth для Mini App.** В `app/api/main.py` добавить:
   `?dev_token=<env DEV_TOKEN>` обходит HMAC-проверку и логинит как
   `ALLOWED_USER_ID`. Только при `APP_ENV != prod`. ~1 час.
3. **Test-scenarios YAML.** Папка `tests/e2e/` в slot-hunter:
   `wizard-rw-by.yml`, `wizard-atlas.yml`, `paywall.yml` с описанием
   действий ("type 'Минск' in #from-input, click first suggestion,
   wait for #to-input, ..."). Claude читает их и прогоняет. ~3 часа.
4. **Auto-test после деплоя.** Опционально: `scripts/deploy.sh` в конце
   тригерит `/test-miniapp` в dev-боте, который прогоняет сценарии и
   репортит. ~1 час.

**Срок:** ~7-8 часов суммарно. Делается после M1/M2.

### M4 — Скриншот Mini App после деплоя
Покрывается M3.1 (как только Playwright MCP есть, Claude может сам
сделать `browser_take_screenshot` и прислать в чат). Отдельная команда
`/screenshot` для quick-check без полного теста.

## 💤 Backlog

- **Прогресс-сообщения для долгих задач.** Claude думает 2 минуты —
  тишина. Раз в 30 сек обновлять status-message «работаю: запустил
  pytest…».
- **Budget visibility.** Сколько turns/токенов потратили в этой сессии.
- **Multi-workspace.** Быстрое переключение между проектами (когда
  Slot Hunter не единственный).
- **Error-tracking forward.** Sentry / GlitchTip — пушить ошибки прода
  в чат сразу, без ожидания дайджеста.
- **TG voice → text.** Бот сам транскрибирует voice-сообщения через
  Whisper на VPS (faster-whisper / OpenAI API).
  Сейчас полагаемся на встроенную TG-транскрипцию.

## Ограничения сейчас

- Один stored-сеанс на чат. `/reset` стирает, `/compact` ужимает.
- Длинные ответы режутся на 3500-сим куски, >14k → файлом.
- Файлы от пользователя не принимаются (только текст и фото).
- Voice/video — friendly hint, не транскрибируется.
- Бот работает от root (как и slot-hunter). Non-root deploy user —
  отдельная задача в slot-hunter (#0019).
