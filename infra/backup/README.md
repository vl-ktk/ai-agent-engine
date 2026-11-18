# 4.18 — локальные копии AI Agent Engine (Netcup VPS)

## Назначение и границы

- Один автономный процесс Python из `ai_agent_engine/local_backup.py`, без новых сервисов, платных API и сетевых отправок. Не зависит от версии запущенного контроллера.
- Источники фиксированы в коде: **живой** `/var/lib/ai-agent-engine/projects/engine.sqlite3` и обычные файлы из `/var/lib/ai-agent-engine/{projects,workspaces,artifacts}`.
- Для SQLite используется **online backup API**; живой файл БД никогда не копируется через `cp`, не удаляется и не перезаписывается. WAL/SHM/journal исключены из tar.
- Каталог `/etc/ai-agent-engine/secrets`, другие части `/etc` и журнал systemd не копируются.
- Назначение: `/var/backups/ai-agent-engine`, владелец `ae-runtime`, права `0700`, файлы копий `0600`. Установленный код и юниты — `root:root`.
- Снимок: `engine.sqlite3`, `files.tar.gz`, `manifest.json` (хеши SHA-256, размеры, число файлов). Проверка `PRAGMA integrity_check`, SHA-256 и полное чтение архива до подтверждения успеха.
- При успешном создании — атомарная публикация снимка; неудачные временные файлы удаляются. `flock` запрещает одновременные запуски.
- Ротация: максимум **7 последних** снимков и **2 GiB** суммарно. Удаляются **только старые успешно проверенные снимки** внутри отдельного каталога копий; рабочие данные никогда не удаляются. При размере одного снимка более 2 GiB процесс завершится с ошибкой без удаления источника.
- Расписание: `ae-backup.timer`, ежедневно **03:00 UTC** (05:00 по Киеву в зимнее время / 06:00 в летнее), допустимое случайное отклонение до 5 минут; `Persistent=true` запускает пропущенный цикл после включения машины.
- Резервирование **на том же диске** защищает от случайной порчи/удаления отдельных файлов, но **не спасает при утрате всего VPS/диска**. Отдельная внешняя копия не входит в 4.18; восстановление отрабатывается только в 4.19.

## Порядок установки без изменения контроллера

1. Проверить источник `main`, записать SHA коммита и SHA работающего контроллера; сделать `df -h /var/lib/ai-agent-engine`, `du -sh /var/lib/ai-agent-engine/{projects,workspaces,artifacts}` и `ls -ld /var/backups/ai-agent-engine` (если есть).
2. Проверить `sha256sum` файла из точного коммита до его запуска. Установить как `root:root /0755` в `/usr/local/libexec/ae-backup.py` и прочитать исходник. Установить оба `.service/.timer` как `root:root /0644` в `/etc/systemd/system/`. Не менять `ae-controller`.
3. Создать каталог `/var/backups/ai-agent-engine` (`ae-runtime:ae-runtime /0700`), только если ранее не существовал. Если уже есть — сверить состав и права, не перезаписывать и не очищать без аудита.
4. `systemd-analyze verify /etc/systemd/system/ae-backup.service /etc/systemd/system/ae-backup.timer`; `systemctl daemon-reload`; `systemctl enable --now ae-backup.timer`; `systemctl start ae-backup.service`.
5. После одного живого резервирования: `systemctl status ae-backup.service --no-pager`, `systemctl list-timers ae-backup.timer --no-pager`, `runuser -u ae-runtime -- python3 /usr/local/libexec/ae-backup.py verify`, `du -sh /var/backups/ai-agent-engine`, `df -h /var/lib/ai-agent-engine`, `systemctl is-active ae-controller.service`. Зафиксировать результат и время следующего запуска.

## Проверка и откат

- Безопасная проверка от имени сервиса: `runuser -u ae-runtime -- python3 /usr/local/libexec/ae-backup.py verify`. Она **ничего не меняет**.
- Откат расписания, если требуется: `systemctl disable --now ae-backup.timer`; `systemctl stop ae-backup.service`. Затем возврат только ранее сохранённых root-owned юнитов и standalone-кода. **Не удалять созданные копии автоматически** — это пользовательские данные.
- Следующий подпункт `4.19` — независимая проверка восстановления в **отдельный тестовый путь**; в 4.18 рабочую БД не трогать.
