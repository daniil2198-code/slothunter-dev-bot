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
- ✅ **M1** — утренний дайджест в 09:00 (commits / ROADMAP / прод / логи) + `/digest`
- ✅ **M2** — `/history`, `/show`, `/resume` — журнал разговоров с автосохранением summary при `/reset`

## 🔥 Planned next

### M1.5 — Voice transcription через Groq
**Статус:** в работе пользователем (env-переменные `GROQ_API_KEY`,
`GROQ_WHISPER_MODEL=whisper-large-v3-turbo`, `VOICE_MAX_DURATION_SEC` уже
проставлены на VPS). Осталось — handler.

**Что делать:**
- В `app/bot.py` на `F.voice` (заменить existing «не поддерживается»):
  - скачать `voice.file_id` через `bot.download_file`
  - проверить `duration <= VOICE_MAX_DURATION_SEC`
  - POST в `https://api.groq.com/openai/v1/audio/transcriptions`,
    multipart с `file=<bytes>`, `model=whisper-large-v3-turbo`,
    `language=ru`, `response_format=text`
  - получить текст → передать в `sess.query(text)` как обычное сообщение

**Срок:** ~30 мин.

### M3 — Автономное тестирование Mini App
**Цель:** Claude сам кликает, проверяет инварианты, читает консоль —
без открытия Mini App в Telegram руками.

После каждого деплоя сейчас приходится открывать `mini.slothunter.space`
в Telegram, гонять wizard, смотреть что не сломалось. С M3 — пишешь
боту «протестируй последний деплой» → он сам всё делает.

**Архитектура:**

```
TG → dev-bot → Claude (Opus 4.7)
                 │
                 ├─→ Read/Edit/Write/Bash (как сейчас)
                 ├─→ Playwright MCP (новое)
                 │     ├─ browser_navigate, browser_click,
                 │     │  browser_type, browser_snapshot,
                 │     │  browser_take_screenshot
                 │     ├─ browser_console_messages, browser_network_requests
                 │     └─ работает на VPS (headless Chromium)
                 └─→ Mini App backend (REST через Bash + curl)
```

**Этапы:**

1. **Playwright MCP подключён к Claude в dev-боте.** ~2-3 часа.
   - В `ClaudeAgentOptions` добавить
     `mcp_servers={"playwright": {"command": "npx", "args": ["-y",
     "@playwright/mcp"]}}`.
   - На VPS установить `npx` зависимости + `npx playwright install
     chromium --with-deps` (системные шрифты, кодеки).
   - Прокинуть `playwright_*` tools через `can_use_tool` —
     `browser_navigate`, `browser_take_screenshot` auto-approve;
     `browser_evaluate`, `browser_run_code_unsafe` спрашивают.
   - Smoke-тест: бот может зайти на `mini.slothunter.space` и сделать
     скриншот → ответить картинкой в чат. **Это закрывает изначальный
     M4 «скриншот после деплоя» бесплатно.**

2. **Dev-mode auth для Mini App.** ~1 час.
   - В `app/api/auth.py` добавить bypass: если в URL есть
     `?dev_token=<env DEV_TOKEN>` И `settings.app_env == "development"
     | "test"`, считаем юзером `ALLOWED_USER_ID`.
   - Без этого Playwright не пройдёт Telegram WebApp HMAC-валидацию.
   - **Никогда** не активировать в продовом env — гард по `app_env`.
   - Опц.: ограничить bypass per-IP (только loopback) если хочется
     защититься от случайного `app_env=test` на проде.

3. **Test-scenarios как YAML или Markdown.** ~3 часа.
   - Папка `tests/e2e/scenarios/` в slot-hunter:
     - `wizard-rw-by-create-alert.yml` — Минск → Витебск, дата
       завтра, выбрать поезд, сохранить, проверить что в `/api/alerts`
       появилось.
     - `wizard-atlas-create-alert.yml` — Минск → Брест, дата, рейс,
       сохранить.
     - `paywall.yml` — после 1 алерта попытка создать второй =
       402 + баннер «Подписка».
     - `mini-app-loads.yml` — открыть, проверить что сервис-карточки
       видны, лого видно, нет console errors.
   - Формат: декларативный список шагов («click selector», «type
     value», «assert text contains», «assert network 200 to URL pattern»).
   - Claude читает scenario как часть промпта и через Playwright tools
     прогоняет.

4. **Команда `/test` в боте + auto-test после deploy.** ~1 час.
   - `/test` без аргументов — прогон всех scenarios, отчёт в чат.
   - `/test <name>` — конкретный сценарий.
   - В `slot-hunter/scripts/deploy.sh` после успеха —
     `curl -X POST $DEV_BOT/test-trigger` (новый webhook в dev-боте).
     Бот прогоняет smoke-suite, репортит что прошло / упало.

5. **Регрессия скриншотов (опц.).** ~2 часа.
   - При первом прогоне scenario сохраняет «эталонные» скриншоты в
     `tests/e2e/baseline/`.
   - На последующих прогонах — pixel-diff. Расхождения > N% — падает
     с прикреплённой картинкой и diff-overlay.

**Срок:** ~7-9 часов суммарно. Делается после M1/M2.

**Критичные riski:**
- Playwright headless Chromium тяжёлый — на 4GB VPS будет жить рядом
  с Postgres / Redis / API / dev-bot. При прогоне 5-10 сценариев в
  параллель может OOM. Защита: gate в Claude что больше 1 сценария
  одновременно не запускать.
- TG WebApp `initData` HMAC завязан на bot token. dev-mode bypass
  МОЖЕТ случайно затечь в прод если ты вырубишь APP_ENV. Чек-лист
  при включении: env-проверка + IP-binding + чёткое логирование.

## 💤 Backlog

- **Прогресс-сообщения для долгих задач.** Claude думает 2 минуты —
  тишина. Раз в 30 сек обновлять status-message «работаю: запустил
  pytest…». ~1 ч.
- **Budget visibility.** Сколько turns/токенов потратили в этой сессии.
  Нужно для прозрачности подписочных лимитов. ~30 мин.
- **Multi-workspace.** Быстрое переключение между проектами (когда
  Slot Hunter не единственный) — `/workspace add <path> <name>`,
  `/workspace switch <name>`. ~2 ч.
- **Error-tracking forward.** Sentry / GlitchTip — пушить ошибки прода
  в чат сразу, без ожидания дайджеста. Зависит от #0024 в slot-hunter. ~1 ч после.
- **Бот деплоит сам себя.** Команда «обнови dev-bot» → коммит, пуш,
  ssh deploy.sh, рестарт. Сейчас частично работает через Bash, но
  не идиоматично. ~30 мин + хороший hand-over.
- **Web preview для Mini App.** Кроме скриншотов — присылать
  embed-ссылку которая открывается без Telegram (для proof-reading
  макетов с любого устройства).

## Ограничения сейчас

- Один stored-сеанс на чат. `/reset` сохраняет summary в `/history`,
  можно `/resume <id>`.
- Длинные ответы режутся на 3500-сим куски, >14k → файлом.
- Файлы от пользователя не принимаются (только текст и фото).
- Voice — пока на встроенную TG-транскрипцию (см. M1.5).
- Бот работает от root (как и slot-hunter). Non-root deploy user —
  отдельная задача в slot-hunter (#0019).
