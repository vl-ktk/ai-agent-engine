# HANDOFF — 2026-11-17, глобальный подпункт 4.2

**Источник:** `vl-ktk/ai-agent-engine`, `main`. **Текущая работа:** только глобальный **4.2**. Техническая выкладка и основные живые проверки **PASS**, ожидается итоговая приёмка Владислава; **4.3 не начинать** до его явного «Давай дальше».

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

- Это **только жизненный цикл контроллера**; реальный LLM/очередь/задачи/хранилище/backup остаются для последующих глобальных подпунктов. VPS не тестировался полной перезагрузкой; verified `enabled` и живые `stop/start/restart/auto-recovery`.
- `systemd-ssh-generator` иногда печатает нефатальное `Failed to query local AF_VSOCK CID` при `daemon-reload/enable/disable`; запуск и здоровье служб это не нарушило.
- Установок новых платных услуг, изменений паролей/ключей/системных прав или потери данных **не было**.
- Релизный откат первой установки: SCP → root console → `systemctl disable --now ae-controller.service`; оставляет остальные службы активными. Восстановить `systemctl enable --now ae-controller.service`.
- Основной технический объём 4.2 выполнен; **оставить чекбокс [ ] до итоговой приёмки владельца**, затем зафиксировать [x]. Никаких действий с 4.3 без отдельного явного поручения. Предшествующая GitHub версия документации для отката: `9e32e44a7b76c2c7d11491628fa1ec294a7e984f`.
