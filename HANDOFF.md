# HANDOFF — 2026-11-18, этап 4.16 завершён

**Источник:** `vl-ktk/ai-agent-engine`, `main`. **Единый план:** `IMPLEMENTATION_ROADMAP.md` (11 этапов, 169 подпунктов). **Следующий — 4.17 (хранение секретов и контроль чтения); не начинать без нового «Дальше» Владислава.** 4.15 выполнен 17.11; 4.14 принят; 4.2–4.13 проверены. Старые открытые 1.x/2.x сохраняют статус.

## Результат 4.16

- GitHub исходник `0.3.9a0`: `ai_agent_engine/task_db.py`, `tests/test_task_db.py`, `infra/storage/TASK_DB.md`; commit `71907236cf0aa15bfedac6b776a213c84a915383`; предыдущий `main` для отката исходной правки `7a03d14c86adfb5dbe52910f999249e8f3f86474` (откат только отдельным совместимым коммитом, не затирать чужие изменения).
- В исходнике SQLite миграции `0→1→2` (schema version via `user_version` and migration table), task/project/dependency/event tables, explicit project-scoped CRUD and optimistic revisions; `2→1` rollback разрешён только при пустой таблице событий; `1→0` разрушительный rollback запрещён.
- Проверки локально: `unittest` **218 OK (1 skipped)**, source scan, workflow permissions, compileall, smoke, wheel сборка — PASS; CI [#44189539204](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44189539204) SUCCESS.
- 18.11.2026 на существующем Netcup VPS `v2202611460959561466` скачан exact pinned GitHub `task_db.py` во временный путь `/tmp/ae-task-db-71907236.py`. Его SHA-256 `6d60ec9d8513d75a7e5f5323415f5b1a8b1d01b47bcf37fa9d8a958630f366` совпал с локальным источником. Владелец подтвердил запуск.
- Live под `ae-runtime`: `smoke` на disposable базе **PASS** (CRUD, migration, rollback, integrity); `init` на `/var/lib/ai-agent-engine/projects/engine.sqlite3` → `schema=2, integrity=ok`; `check` → `schema=2, integrity=ok`. `stat` → `ae-runtime:ae-runtime`, mode **0600**; каталог `projects` ранее проверен mode **0700**. Реальные project/task rows не создавались, live rollback не выполнялся.
- По завершении Netcup `ae-controller.service` **active**, `systemctl --failed` → **0 failed**. Новых платных ресурсов, изменений ключей или чужих данных нет.

## Фактически развернуто и ограничения

- На Netcup текущий установленный контроллер **0.3.8a0**, release `85375103800845eeff696e53f510c66c428408ed`, путь `/opt/ai-agent-engine/current`. Этот релиз **не обновлялся до 0.3.9a0**; БД создана самостоятельной проверенной утилитой из GitHub. Сервис `enabled` / `active`. Для остановки первой установки: `systemctl disable --now ae-controller.service`; для восстановления: `systemctl enable --now ae-controller.service` (без удаления данных); более раннего controller release нет.
- Каталоги `projects`, `workspaces`, `artifacts` существуют с `ae-runtime:ae-runtime` mode `0700` — доказательства `infra/storage/PROJECT_LAYOUT.md`. БД внутри `projects` общая для движка; project_id scoping **не полноценная граница авторизации**, до будущих grants и прав API.
- **Откат SQLite в production не делать:** он не проверялся на данных, задача резервных копий/восстановления будет в 4.18–4.19. Файл БД нельзя удалять ради отката GitHub; сохранённая схема v2 уже корректна и пуста. Тест backward migration делался только на disposable базе. Новые секреты и очередь не добавлялись (этапы 4.17 и 5).
- Следующая работа **строго 4.17** после команды владельца. Сначала проверить фактический main, live Controller release и HANDOFF; при новой публикации сделать source/CI/live проверки, сохранить версию и rollback id.
