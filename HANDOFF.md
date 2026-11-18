# HANDOFF — 2026-11-18, 4.18 IN PROGRESS (ожидается живой запуск)

**Единственный источник:** `vl-ktk/ai-agent-engine`, `main`; единый план `IMPLEMENTATION_ROADMAP.md` (11 этапов / 169 подпунктов). **Текущий — 4.18**, ещё не завершён. 4.19 не начинать.

## Выполнено

- Исправлено прежнее повреждение плана, унаследованное от фиксации 4.17: полный текст восстановлен GitHub commit `d4d759d7b5864aa7270e4dee1b2dbd58a0749c89`; CI #44262903997 SUCCESS. 4.17 завершён.
- Проверены Netcup `v2202611460959561466`: контроллер `ae-controller.service` **active**, установленная версия `0.3.8a0`, release `85375103800845eeff696e53f510c66c428408ed`. Рабочая SQLite `/var/lib/ai-agent-engine/projects/engine.sqlite3` принадлежит `ae-runtime`, `0600`, `integrity=ok`; рабочие каталоги `0700`.
- Перед 4.18 свободно **149G** из **157G** на `/`; используемые источники: `projects 56K`, `workspaces 4K`, `artifacts 4K`. `/var/backups` существует root-owned, но `/var/backups/ai-agent-engine`, `/usr/local/libexec/ae-backup.py`, `/etc/systemd/system/ae-backup.{service,timer}` отсутствуют.
- В `main` коммите **`1cf77a2c594914399d1d8a2204eca58489868f41`** реализованы автономные приватные копии SQLite (online backup) + файлов (`tar.gz`) и проверка (`integrity_check`, SHA-256), ротация 7 копий/2 GiB, блокировка параллельных запусков, CLI create/verify, hardened systemd service + daily timer 03:00 UTC, инструкция `infra/backup/README.md`, версия Python исходника `0.3.11a0`.
- Локально: **252 unittest PASS (1 skipped)**, 18 тестов модуля резервирования PASS, `compileall` PASS, wheel build PASS, `systemd-analyze verify` PASS, source secret scan PASS, workflow boundaries PASS. [GitHub CI #44265372327](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44265372327) **SUCCESS**.

## Что осталось для критерия 4.18

- **Живые изменения ещё не делались**. На Netcup установить root-owned файл `ai_agent_engine/local_backup.py` из точно зафиксированного SHA `1cf77a2...` с SHA-256 `0ba673121ba68a77d8b08307d85679db9ce768af280173b46611aa8528204c2b`; установить 2 systemd unit, создать отдельный `ae-runtime /0700` каталог копий. Затем `systemd-analyze verify`, запустить первое резервирование, проверить целостность, размер, ротацию и активный timer; не менять контроллер и рабочую БД.
- **Computer Use action-time consent требуется** непосредственно перед установкой/запуском нового кода и изменением OS systemd. Копии содержат приватные данные. Ротация при накоплении автоматически удаляет только старые проверенные снимки, **не рабочие данные**.
- Только после успешной live проверки: сменить 4.18 с `[ ]` на `[x]` в `IMPLEMENTATION_ROADMAP.md`, обновить `HANDOFF.md` на завершённый и сохранить исходник в `main` с CAS; отчёт с новым commit SHA и rollback.

**Откат при неудачном live запуске:** `systemctl disable --now ae-backup.timer` и при необходимости `systemctl stop ae-backup.service`; вновь созданные копии **не удалять автоматически**. Предыдущий SHA GitHub до кода 4.18 — `d4d759d7b5864aa7270e4dee1b2dbd58a0749c89`. Рабочий controller release не менялся.
