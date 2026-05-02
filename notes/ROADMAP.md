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
- ✅ **M1.5** — voice через Groq Whisper (handler в боте, env уже стояли)
- ✅ **M2** — `/history`, `/show`, `/resume` — журнал разговоров с автосохранением summary при `/reset`
- ✅ **M3.1** — Playwright MCP подключён, browser_* tools работают, скриншоты авто-приходят
- ✅ **M3.2** — dev-token bypass для Mini App (см. slot-hunter `app/api/deps.py`), Claude видит реальные данные
- ✅ **M3.3** — e2e-сценарии в `slot-hunter/notes/e2e/` (home, paywall, api-health)
- ✅ **M3.4** — `/test [<scenario>]` команда + file-based триггер для авто-теста после деплоя
- ✅ **VPS push keys** (2026-05-02) — два per-repo Ed25519 + SSH-config (см. `notes/decisions.md`)
- ✅ **Расширенный auto-list** — `cp/mv/tar/make/git commit/systemctl restart/uv sync` без prompt'ов
- ✅ **`/yolo on|off`** — broker-level bypass работает под root (без SDK `bypassPermissions`)
- ✅ **`/thinking on|off`** — surface ThinkingBlocks отдельным `💭` сообщением
- ✅ **Catastrophic safety net** — `rm -rf /`, `dd`, форкбомба, `shutdown` и т.п. остаются за approval даже в YOLO
- ✅ **Watchdog** — `OnFailure=` + `dev-bot-watchdog.timer` (5-мин cadence) с recovery-уведомлением через slot-hunter bot

## 🔥 Planned next

### M3.5 — Регрессия скриншотов (визуальный baseline)
**Цель:** ловить визуальные регрессии без участия человека. После
первого прогона e2e-сценария сохраняем «эталонные» скриншоты как
baseline; на повторных прогонах сравниваем по пикселям, расхождения
выше порога — фейлим тест с приложенным diff-overlay.

**Что нужно:**
- Папка `slot-hunter/notes/e2e/baseline/<scenario>/<step>.png` —
  эталонные скриншоты, под git.
- Команда `/test --update-baseline <name>` — пересохраняет эталоны
  (для сознательных UI-изменений).
- В каждом сценарии — указать какие скриншоты считать baseline'ом
  (имена/селекторы).
- Pixel-diff через Pillow или OpenCV — настраиваемый порог (`5%`
  default), визуализация diff-overlay в файле, бот аттачит.

**Зачем не сегодня:**
- Pixel-diff — rabbit hole с false-positives на anti-aliasing,
  виртуальном курсоре, рандомных гифках. Лучше дать M3.1-3.4
  отстояться неделю и понять, какие сценарии реально надо защищать
  визуально.
- `M3.4` плюс ручная проверка скриншотов через TG-чат уже даёт 80%
  ценности.

**Срок:** ~2-3 часа когда дойдёт.

### Критичные риски M3 (что мониторить)

- **OOM на 4GB VPS.** Playwright headless Chromium ест ~300-500MB
  per browser. Параллельные прогоны = сжигание памяти. Сейчас
  `/test` сериализует через `_lock` сессии — больше 1 сценария
  одновременно не уйдёт. Если будем добавлять параллельные тесты —
  поставить семафор.
- **Dev-token leak** через TG breadcrumbs. Закрыто `_redact_secrets`
  в `claude_session.py`, но если добавятся новые места где Claude
  цитирует URL — пройдись по ним и проверь, что redaction
  применяется.

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
