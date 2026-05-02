# Slot Hunter dev-bot

Telegram-бот, через который ты разрабатываешь Slot Hunter BY с
телефона. Под капотом — Claude Code на VPS, работает как локальная
разработка (читает `/opt/slot-hunter`, делает коммиты, пушит в git,
гоняет Playwright-тесты Mini App).

## Что умеет

- ✅ Один пользователь (whitelist по Telegram `user_id`)
- ✅ Подписка Claude.ai (НЕ API-ключ — авторизация через `claude /login`)
- ✅ Stateful сессия с автоматическим resume через рестарты
- ✅ Inline-кнопки разрешения для опасных операций
- ✅ Smart Bash auto-approve для read-only команд (git status / log /
  diff, ls, cat, pytest, ruff, uv, …)
- ✅ Auto-pull cwd перед каждой задачей
- ✅ `/menu` — quick-action клавиатура (Status / Diff / Tests / Deploy /
  Roadmap / Logs)
- ✅ **Голосовые** через Groq Whisper (`F.voice` → транскрипт →
  Claude). Бот сначала показывает «🎤 _распознанный текст_», потом
  ответ.
- ✅ **Картинки** — фото с подписью или без; Claude видит через
  `Read` tool, авто-аттач путей в ответах.
- ✅ **Утренний дайджест** в 09:00 Минск: коммиты за сутки, ROADMAP,
  что брать дальше, статус прода, ошибки в логах.
- ✅ **Журнал разговоров** — `/history` / `/show <id>` / `/resume <id>`,
  автосохранение summary при `/reset`.
- ✅ **Playwright MCP** — Claude умеет в headless-браузер: navigate,
  click, type, screenshot, console / network logs.
- ✅ **Mini App dev-mode** — через `?dev_token=<secret>` Claude
  заходит как залогиненный юзер, видит реальные алерты, прокликивает
  wizard.
- ✅ **e2e-тесты** — markdown-сценарии в `slot-hunter/notes/e2e/`,
  запуск через `/test <name>`.
- ✅ **Auto-test после деплоя** — slot-hunter `deploy.sh` дропает
  trigger-файл, бот в течение ~60с прогоняет smoke-тест.

## Один раз: подготовка VPS

### 1. Node + Claude Code CLI

```bash
# Node 20+ (Claude Code требует ≥20)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs

npm install -g @anthropic-ai/claude-code
claude --version
```

### 2. **Активация подписки Claude** ← обязательный шаг

OAuth-flow привязывает CLI к твоему claude.ai-аккаунту (Pro/Max).

```bash
tmux new -s claude-login
claude
# В Claude CLI:
/login
```

URL → браузер → залогиниться → получить code → вставить в терминал.
Токен ляжет в `/root/.claude/`. Выйти: `Ctrl-C`, отвязать tmux:
`Ctrl-B D`.

### 3. uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"
echo 'export PATH="/root/.local/bin:$PATH"' >> ~/.bashrc
```

### 4. Git push на VPS

Бот будет коммитить в `/opt/slot-hunter` и пушить:

```bash
cd /opt/slot-hunter
git push --dry-run
# Если ошибка: git config --global credential.helper store + один git push с PAT
```

### 5. Playwright (для M3)

Один раз — ставит chromium-headless-shell + системные зависимости:

```bash
cd /opt/slothunter-dev-bot
bash scripts/install_playwright.sh
```

## Установка бота

```bash
git clone https://github.com/daniil2198-code/slothunter-dev-bot.git /opt/slothunter-dev-bot
cd /opt/slothunter-dev-bot

cp .env.example .env
nano .env

bash scripts/deploy.sh
```

`deploy.sh`:
- `uv sync` зависимости
- создаёт `/var/lib/slothunter-dev-bot/` (state) и `triggers/`
- ставит и стартует systemd-сервис `dev-bot`
- pre-flight: рефузит старт без `~/.claude/` auth

Логи: `journalctl -u dev-bot -f`.

## Переменные `.env`

| Переменная | Зачем |
|---|---|
| `TELEGRAM_BOT_TOKEN` | от @BotFather (для самого dev-бота, не для Slot Hunter) |
| `ALLOWED_USER_ID` | твой numeric Telegram id (см. `@userinfobot`) |
| `DEFAULT_WORKDIR` | `/opt/slot-hunter` |
| `MODEL` | `claude-opus-4-7` (или другая) |
| `CLAUDE_BETAS` | пусто (на подписке betas игнорируются — 1M контекст работает дефолтно) |
| `LOG_LEVEL` | `INFO` |
| `DIGEST_TIME` | `09:00` (HH:MM, Europe/Minsk; пусто = выключить) |
| `DIGEST_REPO` | `/opt/slot-hunter` |
| `DIGEST_HEALTHZ` | `http://127.0.0.1:8000/healthz` |
| `DIGEST_LOG_UNITS` | `slot-hunter-api,slot-hunter-bot,slot-hunter-worker,dev-bot` |
| `GROQ_API_KEY` | от console.groq.com (пусто = голосовые off) |
| `GROQ_WHISPER_MODEL` | `whisper-large-v3-turbo` |
| `VOICE_MAX_DURATION_SEC` | `600` (10 мин) |
| `PLAYWRIGHT_MCP_ENABLED` | `true` после `install_playwright.sh` |
| `DEV_AUTH_TOKEN` | тот же секрет что в slot-hunter `.env`; пусто = M3.2 off |
| `MINIAPP_URL` | `https://slothunter.space` |

## Команды

| | |
|---|---|
| `/start` | приветствие |
| `/help` | полная справка (6 секций) |
| `/menu` | inline-кнопки быстрых действий |
| `/status` | cwd, session id, модель |
| `/reset` | забыть разговор; перед стиранием сохранит summary |
| `/compact` | ужать историю в summary; контекст продолжается |
| `/cancel` | прервать текущий ход |
| `/cd <path>` | сменить cwd Claude |
| `/digest` | утренний дайджест по запросу |
| `/history` | список сохранённых сессий |
| `/show <id>` | открыть summary |
| `/resume <id>` | стартовать с этим контекстом как фон |
| `/test` | список e2e-сценариев |
| `/test <name>` | прогон конкретного через Playwright |

## Permission model

**Auto-approve:** Read, Glob, Grep, Edit, Write, MultiEdit,
NotebookRead, TodoWrite. Безопасный bash (git status / log / diff,
ls, cat, head, pytest, ruff, uv, docker ps/logs/inspect, systemctl
status, journalctl, …).

**Browser auto-approve** (когда `PLAYWRIGHT_MCP_ENABLED=true`):
navigate, snapshot, screenshot, console_messages, network_requests,
click, type, fill, drag, hover.

**Спрашивает inline-кнопкой:** любой Bash вне whitelist'а, WebFetch,
WebSearch, Task, MCP-tools (`browser_evaluate`,
`browser_run_code_unsafe`).

**Деструктивный bash** получает ⚠️ в запросе: `rm -rf`,
`git push --force`, `git reset --hard`, `DROP TABLE`, `TRUNCATE`,
`docker system prune`, `shutdown` / `reboot`.

Таймаут на approval — 120 сек. Молчание = deny.

## E2E-тесты (M3)

Markdown-сценарии лежат в `slot-hunter/notes/e2e/`:

| Файл | Что проверяет |
|---|---|
| `mini-app-home-loads.md` | Mini App грузится без console errors |
| `paywall.md` | Free-tier лимит → 402 + баннер |
| `api-health.md` | `/api/healthz` через браузер |

**Запуск:**

- Вручную: `/test <name>` в боте.
- Автоматически: `slot-hunter/scripts/deploy.sh` после успешного
  деплоя кладёт триггер в `/var/lib/slothunter-dev-bot/triggers/`,
  бот в течение ~60с прогоняет `mini-app-home-loads`.

**Добавить сценарий:** новый `*.md` файл в `notes/e2e/` со структурой
Goal / Setup / Steps / Pass criteria / Report (см.
`notes/e2e/README.md`).

## Утренний дайджест

В 09:00 Europe/Minsk бот сам присылает:

- 📦 коммиты за сутки в `/opt/slot-hunter`
- 🗺 ROADMAP: in-progress, новые Done, **что можно взять дальше**, blocked
- ✅/⚠️ статус прод-API через `/healthz`
- 📜 ERROR/WARN в `journalctl` с примерами

`/digest` — то же самое по запросу.

## Логи

```bash
journalctl -u dev-bot -f                 # tail
journalctl -u dev-bot --since='1h ago'   # последний час
journalctl -u dev-bot -n 100             # последние 100 строк
journalctl -u dev-bot -n 50 -o cat | jq  # JSON через jq
```

## Обновление бота

```bash
# Локально:
git push origin main
# На VPS (или попроси сам бот):
ssh root@vps 'cd /opt/slothunter-dev-bot && bash scripts/deploy.sh'
```

Бот может задеплоить сам себя — попроси «обнови dev-bot и
перезапустись», подтверди bash-кнопки.

## Безопасность

- **Один user_id whitelist** — silent drop для всех остальных.
- **Dev-token** в `.env` 600, никогда в git.
- **Logs redact** dev-token в browser-breadcrumbs (см.
  `_redact_secrets` в `claude_session.py`).
- Утечка токена = регенерация (`python -c "import secrets;
  print(secrets.token_urlsafe(32))"`) → замена в обоих `.env` →
  рестарт обоих сервисов.

## Ограничения сейчас

- Один stored-сеанс на чат. `/reset` сохраняет summary в `/history`,
  можно `/resume <id>`.
- Длинные ответы режутся на 3500-сим куски, >14k → файлом.
- Файлы (документы) от пользователя не принимаются.
- Видео/video-notes — friendly hint, не транскрибируется.
- Бот работает от root (как и slot-hunter). Non-root deploy user —
  отдельная задача в slot-hunter (#0019).
- Pixel-diff regression для скриншотов отложен (M3.5 в `notes/ROADMAP.md`).
