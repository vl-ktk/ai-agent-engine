# HANDOFF — 2026-11-17, этап 4.15 завершён

**Источник истины:** `vl-ktk/ai-agent-engine`, `main`. **Единый план:** `IMPLEMENTATION_ROADMAP.md` (11 этапов / 169 подпунктов). **Завершены:** технические 4.2–4.13, приёмка 4.14, живой пункт **4.15**. **Следующий открытый пункт — 4.16 (БД и безопасные миграции)**; не начинать без новой команды Владислава. Предыдущий `main` до 4.15: `7754b087f49539d61f09542be4b2750ed99a4d33`.

## Новый результат — 4.15, приватные каталоги

- 17.11.2026 на живом Netcup VPS `v2202611460959561466` под ранее существовавшим приватным корнем `/var/lib/ai-agent-engine` созданы `projects`, `workspaces`, `artifacts`. Каждый: `ae-runtime:ae-runtime`, mode `0700`.
- Корень до работ: `ae-runtime:ae-runtime`, mode `0700`, пуст. Существующие код, служба, секреты и данные не менялись.
- Повторные `install -d -m 0700 -o ae-runtime -g ae-runtime` и итоговый `stat` на всех трёх PASS. `runuser -u ae-runtime -- ls -ld .../projects` PASS, `runuser -u ae-deploy -- ls .../projects` → `Permission denied`.
- После операции `systemctl is-active ae-controller.service` → `active`; `systemctl --failed --no-pager` → `0 loaded units listed`. Платных ресурсов/покупок не создавали.
- Источник воспроизведения и модель владения: `infra/storage/PROJECT_LAYOUT.md`. Будущих папок конкретных проектов **нет** (проектные ID не утверждены); логическая проектная изоляция ещё не реализована, один системный пользователь сам по себе не изолирует проекты. Следующий шаг — только `4.16`, без самовольного подключения проектов.
- Откат серверной разметки: только после проверки, что **каждый из трёх** точных новых каталогов пуст и не используется, и отдельного согласования удаления, точечным `rmdir`, без `rm -rf`; корень не трогать. Откат репозитория — к `7754b087f49539d61f09542be4b2750ed99a4d33`, сохраняя чужие изменения.

## Фактически опубликовано на Netcup

- VPS `v2202611460959561466` (`62.83.131.128`), Debian 13. Реальный `ai_agent_engine.controller` **0.3.8a0** активен как `ae-controller.service`, systemd `enabled`, `active (running)`, `Controller lifecycle ready`.
- Точный исходный commit: `85375103800845eeff696e53f510c66c428408ed`; `/opt/ai-agent-engine/current` указывает на `/opt/ai-agent-engine/releases/85375103800845eeff696e53f510c66c428408ed`. Предыдущая версия контроллера **NONE** — это первая установка.
- Wheel SHA-256: `f1741dcaeeeca1f83447c283512e3a543d02e32730ad18f832991af562cab6cb`. Ручная owner-triggered сборка [Actions #44174524655](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44174524655) SUCCESS, artifact `14370485162`; передача [#44177758050](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44177758050) SUCCESS. Проверка источника, SHA, `verify`, `stage` и compare-and-swap `promote` PASS.
- На VPS без доплаты установлен пакет `python3.13-venv` с обязательными `python3-pip-whl`, `python3-setuptools-whl`. Точный `ae-controller.service` установлен из верифицированного исходника; `systemd-analyze verify` PASS. Старый `ae-host-check.timer` остался active.

## Проведённые живые тесты

- `runuser -u ae-runtime -- /opt/ai-agent-engine/current/venv/bin/python -m ai_agent_engine.controller check --max-age 15` → **controller healthy**, повторно после отката.
- `systemctl restart` PASS; `stop` → `inactive`; `start` → `active`.
- `WatchdogUSec=30s`, живой `WatchdogTimestamp` зафиксирован. Controlled SIGKILL только main PID контроллера: systemd сам восстановил процесс, PID `4647`, `NRestarts=1`, `active (running)`; внешний SSH/хост не затронуты.
- **Rollback первой установки проверен:** `systemctl disable --now ae-controller.service` → `inactive` + `disabled`; затем восстановлено `systemctl enable --now ae-controller.service` → `active` + `enabled`; health повторно PASS. **Возврат к прежнему release SHA невозможен, потому что прежнего релиза не было**; при будущих обновлениях проверять `promote --source-sha PREVIOUS_SHA --expected-current FAILED_SHA` + restart/health. Не удалять файлы и пользовательские данные.
- `id ae-deploy` только собственная группа; `id ae-runtime` только собственная группа; `sudo -n -l -U` обоих → **not allowed to run sudo**. `systemctl is-active ssh` → active; `systemctl --failed` → **0 loaded units listed**. Netcup SCP root VNC console реально работала и оставлена как резервный путь.
- CI: source-level 8-workflow guard `permissions: contents: read`, ручные owner-only SSH workflows и offline CI ранее проверены (см. `infra/security/STAGE64.md`). **Настройки GitHub repository/admin/branch protection нельзя подтвердить:** интеграция возвращает 403; не утверждать их проверенными.

## Ограничения и передача

- Контроллер и структура приватных каталогов подготовлены; реальный LLM/очередь/задачи/БД/секретное хранилище/backup остаются для последующих глобальных подпунктов. VPS не тестировался полной перезагрузкой; verified `enabled` и живые `stop/start/restart/auto-recovery`.
- `systemd-ssh-generator` иногда печатает нефатальное `Failed to query local AF_VSOCK CID` при `daemon-reload/enable/disable`; запуск и здоровье служб это не нарушило.
- В 4.15 новых платных услуг, изменений паролей/ключей, **существующих** системных прав и потери данных не было. Созданным каталогам назначены предусмотренные права `0700`.
- Релизный откат первой установки: SCP → root console → `systemctl disable --now ae-controller.service`; оставляет остальные службы активными. Восстановить `systemctl enable --now ae-controller.service`.
- Основной технический объём старого 4.2 выполнен и разнесён по 4.2–4.13; приёмка 4.14 зафиксирована. **4.15 выполнен на VPS и в GitHub**. Следующий подпункт 4.16 только по новой команде. GitHub до обновления 4.15: `7754b087f49539d61f09542be4b2750ed99a4d33`.

## Новая запись передачи после переразбиения

- Дата: 17.11.2026; владелец прямо поручил увеличить число прямых подпунктов примерно вдвое во **всех** этапах и обновить единый план.
- 79 → **169** подпунктов, **11** глобальных этапов без третьего уровня; старая редакция плана остаётся в Git history (commit `3b34e25780529e785c165318d46fbbe95b51de48`).
- Правила синхронизированы в `AGENTS.md`. Только документация и план; работающий Netcup-контроллер/исходный релиз не изменялись.
- **4.14 принят владельцем, 4.15 выполнен и live-проверен. Следующий пункт: 4.16.** Ранние открытые 1/2 не объявлять закрытыми.
