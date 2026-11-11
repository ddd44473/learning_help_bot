# Docker-развёртывание Coliseum of Knowledge

Документ описывает первый релизный вариант: **один процесс бота, одна реплика, Telegram long polling, без входящих портов и без веб-сервера**. Приложение и все Python-зависимости собираются в образ; на VPS не нужно устанавливать пакеты Python вручную.

> Границы: это runbook для будущего VPS. В текущей staging-среде реальный образ не собирался (Docker в WSL-дистрибутиве недоступен), VPS не выбран, живой бот не запускался. Не выполняйте команды из этого документа на staging-данных без отдельного решения владельца.

## 1. Что именно запускается

- Точка входа: `python -B main.py`.
- Один экземпляр aiogram `Dispatcher` с long polling. Входящий HTTP-порт не нужен.
- Приложение вызывает `load_dotenv()` и может прочитать `.env` из текущего каталога, если он там есть. В образе `.env` отсутствует, поэтому в контейнере используются только переменные окружения, переданные Compose.
- При старте: проверка конфигурации, `init_db()`, `run_migrations()`, `init_chat_archive()`, `init_access_control()`, восстановление незавершённых публикаций тем заказов.
- Фоновые задачи внутри того же процесса: архивный воркер (2 consumer-задачи) и цикл обслуживания реферальных бонусов.
- База: SQLite. Путь по умолчанию — `database/database.db`, но в контейнере переопределяется через `STUDY_BOT_DB_PATH=/data/database.db`.
- Изменяемые данные: `/data` (SQLite и sidecar-файлы `-wal`/`-shm`) и `/app/storage` (`order_archives/`, `export_tmp/`).
- Неизменяемые ресурсы: код, `catalog/`, `imgs/` — внутри образа, только для чтения.
- Время: приложение использует фиксированное московское время UTC+3; отдельный `tzdata` не нужен.
- Healthcheck намеренно отсутствует: у бота нет локального readiness-эндпоинта, а опрос Telegram из healthcheck создавал бы ложную нагрузку и не проверял бы бизнес-готовность.

## 2. Требования к VPS

- Linux VPS (x86_64/amd64 — наиболее простой вариант), 1–2 vCPU, 1–2 ГБ RAM, диск с запасом под SQLite и вложения. Для архива заложите минимум 10–20 ГБ свободного места.
- Docker Engine 24+ и Compose plugin v2 (`docker compose`, не старый `docker-compose`).
- Python 3.10+ на хосте для `deploy/scripts/*.py` (только стандартная библиотека, pip-пакеты не нужны).
- SSH-доступ, желательно только по ключу; root-доступ через `sudo`.
- Исходящий HTTPS (443) к Telegram API. Входящие порты приложению не нужны; оставьте только SSH.
- Синхронизация времени (systemd-timesyncd/NTP) — для корректных логов и временных меток.
- Отдельный пользователь для эксплуатации, например `deploy`, состоящий в группе `docker` (или запуск Compose через `sudo`). Помните, что членство в группе `docker` фактически эквивалентно root-доступу к хосту; после `usermod -aG docker deploy` нужен повторный вход.

Проверка:

```bash
docker version
docker compose version
df -h
free -h
```

## 3. Архитектура образа и политика безопасности

- Базовый образ: `python:3.12.16-slim-bookworm`, закреплён по digest в `Dockerfile`. Debian slim выбран вместо Alpine из-за предсказуемости manylinux-колёс и совместимости с принятым runtime.
- Зависимости ставятся на этапе сборки из `requirements.txt`. Файл в принятом дереве имеет кодировку UTF-16LE с BOM и CRLF; `Dockerfile` конвертирует его только для `pip`, не изменяя owner-файл.
- Runtime-пользователь: UID/GID `10001`, непривилегированный.
- Корневая ФС контейнера — `read_only: true`; запись разрешена только в `/data`, `/app/storage` и tmpfs `/tmp`.
- `cap_drop: ALL`, `no-new-privileges:true`, без `privileged`, без `network_mode: host`, без публикации портов.
- `init: true` (tini) и `stop_grace_period: 30s` для корректной доставки SIGTERM и остановки фоновых задач.
- Образ не содержит `.env`, SQLite, `storage/`, `.git`, `.venv`, тестов, отчётов, логов и recovery-данных — это обеспечивает `.dockerignore`.

## 4. Подготовка каталогов и секретов на VPS

Создайте каталоги. Владелец данных — числовой UID/GID `10001`, чтобы совпадать с пользователем внутри образа. Каталог секретов должен быть доступен пользователю, который запускает `docker compose` (в примерах — `deploy`); если Compose всегда запускается через `sudo`, можно оставить `root:root`.

```bash
sudo install -d -o 10001 -g 10001 -m 750 /opt/coliseum/data
sudo install -d -o 10001 -g 10001 -m 750 /opt/coliseum/storage
sudo install -d -o root -g root -m 700 /opt/coliseum/backups
sudo install -d -o deploy -g deploy -m 700 /opt/coliseum/secrets
```

Скопируйте на VPS `compose.yaml` и каталог `deploy/` (скрипты бэкапа/восстановления). Пример:

```bash
sudo install -d -o root -g root -m 755 /opt/coliseum/deploy
sudo cp -a deploy/scripts deploy/bot.env.example /opt/coliseum/deploy/
sudo cp compose.yaml /opt/coliseum/compose.yaml
```

Создайте приватный env-файл **только из шаблона** и заполните его на сервере:

```bash
sudo install -o deploy -g deploy -m 600 deploy/bot.env.example /opt/coliseum/secrets/bot.env
sudo -u deploy editor /opt/coliseum/secrets/bot.env   # или войдите как deploy и отредактируйте
sudo chown deploy:deploy /opt/coliseum/secrets/bot.env
sudo chmod 600 /opt/coliseum/secrets/bot.env
```

Если `docker compose` запускается от `root`, замените владельца на `root:root`; главное — файл должен читаться тем пользователем, который выполняет Compose, и не должен быть доступен остальным.

Требования к env-файлу:

- `BOT_TOKEN`, `ADMIN_IDS`, `MASTER_GROUP_ID` обязательны.
- `TOPUP_ADMIN_USERNAME`, `SUPPORT_USERNAME` необязательны.
- `STUDY_BOT_DB_PATH` в файл не добавляйте: `compose.yaml` принудительно задаёт `/data/database.db` и перекрывает случайное значение из старого `.env`.
- Не коммитьте заполненный файл, не копируйте его в образ, бэкап или отчёт, не вставляйте его содержимое в чаты и тикеты.

**Модель угроз для env-файла.** Docker env-переменные доступны администраторам Docker (`docker inspect`, привилегированный доступ к `/proc/<pid>/environ`). Это простой первый релизный механизм, а не внешний secrets manager. Ограничьте доступ к хосту и к каталогу `/opt/coliseum/secrets`. Compose `secrets` с файлом `/run/secrets/...` — возможное развитие, но исходный файл всё равно остаётся на VPS, а приложению потребуется обёртка/доработка для экспорта переменных; в этом релизе такой механизм не включён, чтобы не менять принятый Python-код.

## 5. Сборка и передача образа

### Вариант A: локальная сборка и `docker save` (без registry)

На машине, где доступен Docker и есть этот checkout:

```bash
# На машине сборки нет /opt/coliseum/secrets; для build достаточно шаблона.
COLISEUM_ENV_FILE=deploy/bot.env.example \
  docker compose -f compose.yaml -f compose.local.yaml build

docker tag coliseum-bot:local coliseum-bot:1.0.0
docker save coliseum-bot:1.0.0 | gzip > coliseum-bot-1.0.0.tar.gz
sha256sum coliseum-bot-1.0.0.tar.gz
```

Передача на VPS по SSH (пример):

```bash
scp coliseum-bot-1.0.0.tar.gz deploy@<VPS>:/tmp/
```

На VPS:

```bash
gunzip -c /tmp/coliseum-bot-1.0.0.tar.gz | docker load
docker image inspect coliseum-bot:1.0.0 --format '{{.Id}} {{.RepoDigests}}'
```

### Вариант B: приватный GHCR (только с явного разрешения владельца)

Публикация образа — отдельное действие владельца; автоматического CI/push в этом релизе нет.

```bash
docker tag coliseum-bot:1.0.0 ghcr.io/<OWNER>/coliseum-bot:1.0.0
docker push ghcr.io/<OWNER>/coliseum-bot:1.0.0
```

На VPS:

```bash
echo '<GHCR_TOKEN>' | docker login ghcr.io -u <OWNER> --password-stdin
docker pull ghcr.io/<OWNER>/coliseum-bot:1.0.0
```

Не используйте тег `latest` в production. `COLISEUM_IMAGE` в `compose.yaml` должен указывать на конкретный тег/дайджест.

## 6. Первый запуск

Если база и storage уже подготовлены (см. раздел 8), проверьте владельца и права:

```bash
sudo chown -R 10001:10001 /opt/coliseum/data /opt/coliseum/storage
sudo chmod 750 /opt/coliseum/data /opt/coliseum/storage
sudo find /opt/coliseum/storage -type d -exec chmod 750 {} \;
sudo find /opt/coliseum/storage -type f -exec chmod 640 {} \;
[ -f /opt/coliseum/data/database.db ] && sudo chmod 600 /opt/coliseum/data/database.db
```

Проверка конфигурации Compose **без вывода секретов** — используйте синтетический env-файл, а не реальный:

```bash
tmp_env=$(mktemp)
cat >"$tmp_env" <<'EOF'
BOT_TOKEN=123456:synthetic-compose-check
ADMIN_IDS=10001
MASTER_GROUP_ID=-1001234567890
EOF
COLISEUM_ENV_FILE="$tmp_env" \
COLISEUM_DATA_DIR=/tmp/coliseum-compose-check/data \
COLISEUM_STORAGE_DIR=/tmp/coliseum-compose-check/storage \
docker compose -f /opt/coliseum/compose.yaml config --quiet
rm -f "$tmp_env"
```

Команда `docker compose config` без `--quiet` может вывести значения переменных. Не сохраняйте и не публикуйте такой вывод.

Запуск:

```bash
cd /opt/coliseum
export COLISEUM_IMAGE=coliseum-bot:1.0.0
# при необходимости: export COLISEUM_ENV_FILE=/opt/coliseum/secrets/bot.env
docker compose up -d
docker compose ps
docker compose logs --tail=100 --no-log-prefix bot
```

Ожидаемо: контейнер `running`, в логах нет ошибок конфигурации/миграций, бот начинает polling. Не запускайте второй экземпляр с тем же `BOT_TOKEN`: Telegram не допускает два long-polling клиента, а приложение при старте вызывает `delete_webhook(drop_pending_updates=True)`.

Остановка/перезапуск:

```bash
docker compose stop bot
docker compose restart bot
docker compose down          # контейнер и сеть; bind-mount данные остаются
```

Автозапуск после перезагрузки VPS обеспечивают Docker daemon и политика `restart: unless-stopped`; отдельный systemd-unit для Python-приложения не нужен:

```bash
sudo systemctl enable --now docker
docker compose ps
```

**Никогда не используйте `docker compose down -v`, `docker system prune`, `docker volume rm` и не удаляйте `/opt/coliseum/data`** без отдельного подтверждения и свежего бэкапа. В этой конфигурации данные лежат в bind-mount, но ошибочная команда всё равно может уничтожить данные на хосте.

## 7. Проверка образа без запуска живого бота

Офлайн-проверка импортов и конфигурации (синтетические значения, сеть отключена):

```bash
docker run --rm --network none \
  -e BOT_TOKEN=123456:synthetic-container-smoke \
  -e ADMIN_IDS=10001 \
  -e MASTER_GROUP_ID=-1001234567890 \
  -e STUDY_BOT_DB_PATH=/tmp/smoke/database.db \
  --tmpfs /tmp:rw,size=64m \
  --entrypoint python \
  coliseum-bot:1.0.0 -B -c "import main; print('imports ok')"
```

Проверка содержимого образа (не показывать содержимое файлов, только имена):

```bash
docker run --rm --entrypoint sh coliseum-bot:1.0.0 -c \
  'test ! -e /app/.env && test ! -e /app/database/staging.db && test -z "$(find /app/storage -mindepth 1 -print -quit)" && find /app -maxdepth 3 \( -name "*.db" -o -name ".env" -o -name "*.log" \) -print'
docker run --rm --entrypoint sh coliseum-bot:1.0.0 -c 'ls /app/imgs | wc -l; id'
```

Ожидаемо: `uid=10001`, 10 PNG в `/app/imgs`, никаких `.env`/SQLite/логов. Если проверка находит runtime-данные — образ собран с неверным `.dockerignore`, сборку нужно остановить.

Локальная (не контейнерная) проверка layout на синтетических данных:

```bash
.venv/bin/python -B deploy/scripts/smoke_check.py
```

## 8. Перенос существующих staging-данных (отдельное решение владельца)

**Сейчас перенос не выполняется.** Этот раздел — чек-лист на будущее. Не копируйте реальные данные без явного разрешения владельца, не запускайте два polling-экземпляра и не используйте staging-базу как тестовую.

1. Согласуйте окно обслуживания и сделайте свежий бэкап источника.
2. Остановите источник (старый бот/контейнер), чтобы не было двух polling-клиентов.
3. Снимите согласованный снимок SQLite через backup API или после полной остановки — через `sqlite3 .backup`; не копируйте живой `database.db` обычным `cp` как единственный бэкап.
4. Скопируйте `storage/` с сохранением структуры (`rsync -a`), отдельно проверьте наличие `-wal`/`-shm` и переносите их вместе с основной БД, если они есть.
5. Передайте данные по SSH/зашифрованному каналу, сверьте SHA-256.
6. На VPS остановите целевой контейнер. Разместите БД как `/opt/coliseum/data/database.db`, storage — как `/opt/coliseum/storage/`.
7. Выставьте владельца `10001:10001`: БД `600`, каталоги `750`, файлы `640`.
8. Проверьте копию БД на одноразовом пути: `PRAGMA integrity_check` должен вернуть `ok`. Не запускайте приложение против проверочной копии с живым токеном.
9. Запустите контейнер, проверьте логи, известный заказ и отсутствие второго экземпляра.
10. Исходные данные не удаляйте до приёмки; храните их в защищённом месте.

## 9. Резервное копирование

Используйте **maintenance window**: сначала остановите контейнер, затем снимайте согласованный снимок SQLite + storage. Скрипт использует SQLite Backup API, а не `cp` живого файла, и требует флаг `--confirm-stopped`.

```bash
cd /opt/coliseum
docker compose stop bot
sudo python3 deploy/scripts/backup_runtime.py \
  --db /opt/coliseum/data/database.db \
  --storage /opt/coliseum/storage \
  --out /opt/coliseum/backups \
  --confirm-stopped
```

Что делает скрипт:

- копирует БД через `sqlite3.Connection.backup()`;
- проверяет `PRAGMA integrity_check` на копии;
- копирует storage с сохранением структуры, отклоняя symlink и special files;
- пишет `manifest.json`, `SHA256SUMS`, `MANIFEST.sha256`;
- не читает и не включает `bot.env`;
- при ошибке оставляет `INCOMPLETE.txt` — такой каталог нельзя использовать для restore.

Проверка целостности бэкапа:

Бэкап содержит персональные данные, поэтому каталоги бэкапов намеренно создаются root-only (`700`). Проверяйте их через `sudo`:

```bash
sudo bash -c 'cd /opt/coliseum/backups/<backup-dir> && sha256sum -c SHA256SUMS && sha256sum -c MANIFEST.sha256'
sudo python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/opt/coliseum/backups/<backup-dir>/database.db')
print(c.execute('PRAGMA integrity_check').fetchone()[0])
c.close()
PY
```

После успешной проверки верните бота в работу:

```bash
cd /opt/coliseum
docker compose start bot
docker compose ps
```

Рекомендации:

- храните зашифрованную копию **вне сервера** (например, `age`/`gpg` + объектное хранилище или защищённый NAS); не настраивайте облачные ключи без разрешения владельца;
- retention: например, 7 ежедневных + 4 недельных, но проверяйте restore минимум раз в месяц;
- бэкап содержит персональные данные пользователей и вложения — ограничьте доступ, не пересылайте в мессенджеры;
- не запускайте restore в живые данные как тест. Тренируйтесь на `/tmp/...` или отдельном disposable-каталоге.

## 10. Восстановление и rollback

### Тренировочный restore (безопасно)

```bash
sudo python3 deploy/scripts/restore_runtime.py \
  --backup /opt/coliseum/backups/<backup-dir> \
  --db /tmp/coliseum-restore/data/database.db \
  --storage /tmp/coliseum-restore/storage \
  --confirm-stopped
```

Скрипт проверит manifest, все SHA-256, `integrity_check` и только потом копирует. Для существующей цели без `--force` он откажется работать. Цели-симлинки и пересечение backup/target отклоняются до изменений.

### Боевой restore

1. Сделайте свежий бэкап текущего состояния.
2. `docker compose stop bot`.
3. Запустите restore с `--force`; старые БД/storage будут **переименованы** в `*.pre-restore-<stamp>`, а не удалены.
4. Восстановите владельца и права: restore обычно запускается через `sudo`, поэтому новые файлы принадлежат root.

   ```bash
   sudo chown -R 10001:10001 /opt/coliseum/data /opt/coliseum/storage
   sudo chmod 750 /opt/coliseum/data /opt/coliseum/storage
   sudo find /opt/coliseum/storage -type d -exec chmod 750 {} \;
   sudo find /opt/coliseum/storage -type f -exec chmod 640 {} \;
   sudo chmod 600 /opt/coliseum/data/database.db
   ```

5. Проверьте `integrity_check` и права (`10001:10001`, БД `600`).
6. `docker compose up -d`, проверьте логи и известный заказ.
7. Не удаляйте `pre-restore` каталоги, пока приёмка не завершена.

### Rollback образа

```bash
cd /opt/coliseum
export COLISEUM_IMAGE=coliseum-bot:<previous-tag>
docker compose up -d
```

Данные в bind-mount остаются на месте. Важно: rollback **образа** не откатывает схему БД. Если новый образ уже выполнил несовместимую миграцию, старый код может не понять схему; в этом случае нужен restore БД из бэкапа, а не только смена тега. Поэтому бэкап перед обновлением обязателен.

## 11. Обновление

1. Сделайте бэкап (раздел 9).
2. Получите новый immutable-тег: `docker pull ...` или `docker load`.
3. `COLISEUM_IMAGE=<new-tag> docker compose up -d`.
4. Проверьте `docker compose ps`, логи, ключевой сценарий.
5. При проблеме — rollback на предыдущий тег; при несовместимой схеме — restore.

Обновление базового образа: измените в `Dockerfile` и тег, и digest, пересоберите, прогоните `deploy/scripts/smoke_check.py` локально и офлайн-проверку импортов в контейнере. Не используйте плавающий `latest`.

## 12. Диагностика

| Симптом | Что проверить |
|---|---|
| `Permission denied` при записи в `/data` или `/app/storage` | владелец `10001:10001`, права каталогов `750`, файлов `640`/`600` |
| `database is locked` | не запущен ли второй экземпляр; не лежит ли БД на NFS; остановите лишний процесс |
| `409 Conflict` / конфликт polling | два бота с одним токеном или остался webhook; остановите старый экземпляр |
| Контейнер перезапускается | `docker compose logs --tail=200 bot`; проверьте env-файл, права, свободное место |
| `BOT_TOKEN`/`ADMIN_IDS` ошибка | заполните env-файл; приложение печатает только имя проблемы, не значение |
| Нет фото в разделах | проверьте `/app/imgs` в образе и что `.dockerignore` не исключил `imgs/` |
| Мало места | `df -h`; архивный воркер имеет лимиты и требует запас свободного места |
| Ошибки записи при `read_only: true` | приложение пишет вне `/data`, `/app/storage`, `/tmp`; проверьте путь |

Логи не должны содержать токен. Не публикуйте полные логи с персональными данными без необходимости.

## 13. Минимальный чек-лист приёмки

- [ ] Docker Engine и Compose plugin установлены, `docker version` и `docker compose version` успешны.
- [ ] Образ собран/загружен, тег immutable, `docker image inspect` показывает ожидаемый ID.
- [ ] `.dockerignore` не пропустил `.env`, SQLite, `storage`, `.git`, `.venv`, тесты/отчёты.
- [ ] Env-файл лежит в `/opt/coliseum/secrets/bot.env`, `600`, вне репозитория/образа/бэкапа.
- [ ] `/opt/coliseum/data` и `/opt/coliseum/storage` принадлежат `10001:10001`.
- [ ] `docker compose config --quiet` с синтетическим env проходит.
- [ ] Офлайн import-smoke в контейнере проходит, polling не запускается.
- [ ] Первый запуск: логи без ошибок, ровно один экземпляр, входящие порты не опубликованы.
- [ ] Бэкап снят и проверен на disposable-пути; restore-тренировка успешна.
- [ ] Rollback-тег и процедура restore понятны владельцу.
