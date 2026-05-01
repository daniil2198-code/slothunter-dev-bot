# Slot Hunter dev-bot

Telegram-бот, через который ты разрабатываешь Slot Hunter BY с телефона.
Под капотом — Claude Code на VPS, работает как локальная разработка
(читает `/opt/slot-hunter`, делает коммиты, пушит в git).

- Один пользователь (whitelist по Telegram `user_id`)
- Подписка Claude.ai (НЕ API-ключ — авторизация через `claude /login`)
- Стейтфул сессия с автоматическим resume через рестарты бота
- Inline-кнопки разрешения для опасных операций (Bash, deploy, push)
- Длинные ответы — файлами

## Один раз: подготовка VPS

```bash
ssh root@<your-vps>
```

### 1. Node + Claude Code CLI

```bash
# Node 20 (Claude Code требует ≥20)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs

# Claude Code сам по себе
npm install -g @anthropic-ai/claude-code
claude --version    # проверка
```

### 2. **Активация подписки Claude** ← важный шаг

OAuth-flow привязывает CLI к твоему аккаунту claude.ai (Pro/Max), после
этого бот пользуется подпиской без API-ключей.

```bash
# Внутри tmux (важно — окно держим открытым):
tmux new -s claude-login
claude
# В Claude CLI набери:
/login
```

Будет URL — открой в браузере, залогинься в claude.ai тем же
аккаунтом, что и подписка, подтверди. Code из браузера → вставить в
терминал. Токен ляжет в `/root/.claude/`. Выйти из CLI: `Ctrl-C`,
закрыть tmux: `Ctrl-B` затем `D`.

Проверить что авторизация осталась:

```bash
claude /status     # должен сказать что-то про active subscription
```

### 3. uv (если ещё не стоит)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"
echo 'export PATH="/root/.local/bin:$PATH"' >> ~/.bashrc
```

### 4. Git push на VPS должен работать

Бот будет коммитить от имени `slot-hunter` репо и пушить. Проверка:

```bash
cd /opt/slot-hunter
git push --dry-run
```

Если ошибка — варианты:
- HTTPS: `git config --global credential.helper store` и один раз
  залогиниться через `git push` с PAT-ом из github
- SSH: положить `~/.ssh/id_ed25519` с deploy-key репо

## Установка бота

```bash
git clone https://github.com/<you>/slothunter-dev-bot.git /opt/slothunter-dev-bot
cd /opt/slothunter-dev-bot

cp .env.example .env
nano .env       # заполнить TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID

bash scripts/deploy.sh
```

Скрипт:
- ставит зависимости через `uv sync`
- создаёт `/var/lib/slothunter-dev-bot` (state directory)
- ставит и стартует systemd-сервис `dev-bot`
- хвост логов: `journalctl -u dev-bot -f`

## Переменные `.env`

| Переменная           | Что                                          |
|----------------------|----------------------------------------------|
| `TELEGRAM_BOT_TOKEN` | от @BotFather                                |
| `ALLOWED_USER_ID`    | твой numeric Telegram id (см. @userinfobot)  |
| `DEFAULT_WORKDIR`    | `/opt/slot-hunter` по умолчанию              |
| `MODEL`              | `claude-opus-4-7` (или другая модель)        |
| `CLAUDE_BETAS`       | `context-1m-2025-08-07` для 1M контекста     |
| `LOG_LEVEL`          | `INFO`                                       |

## Команды бота

| Команда             | Что делает                                              |
|---------------------|---------------------------------------------------------|
| `/start`            | Приветствие, текущая директория                         |
| `/status`           | Текущая папка, session id, есть ли pending approval     |
| `/reset`            | Забыть разговор, следующее сообщение — с нуля           |
| `/compact`          | Ужать историю в summary; контекст продолжается          |
| `/cancel`           | Best-effort прервать текущий ход                        |
| `/cd <path>`        | Сменить cwd Claude (требует существующей директории)    |
| `/help`             | Список команд                                           |
| _любой текст_       | Передаётся Claude как user-message                      |

## Permission model

- **Auto** (без подтверждения): Read, Glob, Grep, Edit, Write,
  MultiEdit, NotebookRead, TodoWrite
- **Ask** (inline-кнопки в TG): Bash, WebFetch, WebSearch, Task, MCP
  tools — всё, что не в auto-списке
- Деструктивные bash-команды (`rm -rf`, `git push --force`,
  `git reset --hard`, `DROP TABLE`, etc.) получают
  ⚠️-предупреждение в запросе

Таймаут на ответ — 120 сек. Нет ответа = deny.

## Логи

```bash
journalctl -u dev-bot -f         # tail
journalctl -u dev-bot --since=1h # последний час
journalctl -u dev-bot -n 100     # последние 100 строк
```

Формат — JSON через structlog. Если работаешь с локальным `jq`:

```bash
journalctl -u dev-bot -n 50 -o cat | jq -c
```

## Вернуться к чату с локальной машины

Бот сохраняет `session_id` в `/var/lib/slothunter-dev-bot/chat_<id>.json`.
Этот id можно использовать с локальным Claude Code: `claude --resume <id>`.
Контекст — общий, потому что Anthropic хранит сессии серверно (для
подписки).

## Обновление бота

Когда правишь его код:

```bash
# на ноуте:
git push origin main
# на VPS (или пишешь самому себе в TG):
ssh root@vps 'cd /opt/slothunter-dev-bot && bash scripts/deploy.sh'
```

(После настройки бот может деплоить сам себя через TG: попросишь его
«обнови dev-bot и перезапустись» → он закоммитит, запушит, выполнит
deploy.sh и сервис перезапустит сам себя.)

## Ограничения сейчас

- Один stored-сеанс на чат. `/reset` стирает старый, history теряется;
  `/compact` ужимает без потери. Восстановить старую сессию вручную
  можно из `state_dir/chat_<id>.json` (ручная правка).
- Длинные ответы режутся на 3500-символьные куски; >14k → файлом.
- Bot не умеет принимать файлы от тебя (только текст). Если нужно
  что-то положить в проект — попроси Claude `Write` его в нужное место.
- При срабатывании fail2ban / отключении интернета на VPS — бот падает,
  systemd рестартует через 5 секунд.
