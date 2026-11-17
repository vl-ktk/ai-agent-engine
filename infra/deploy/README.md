# Этап 4 — живой контроллер Netcup: новые подпункты 4.8–4.13

**Состояние на 17.11.2026:** `ae-controller.service` реально установлен на Netcup `v2202611460959561466`, `active (running)`, `enabled`; версия `0.3.8a0`. Владелец 17.11 дал постоянное разрешение на бесплатные штатные действия внутри порученного подпункта. Изменения секретов, прав, платных услуг и опасное удаление остаются под отдельным согласием.

## Артефакт и воспроизводимость

- Точный source SHA: `85375103800845eeff696e53f510c66c428408ed`.
- Wheel SHA-256: `f1741dcaeeeca1f83447c283512e3a543d02e32730ad18f832991af562cab6cb`.
- GitHub owner-only сборка [#44174524655](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44174524655), artifact `14370485162`; owner-only доставка [#44177758050](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44177758050), обе SUCCESS.
- На VPS доверенные `promotion.py` и шаблон systemd сверены по SHA с source commit. `verify`, `stage`, `promote` успешно выполнены. `/opt/ai-agent-engine/current` → `releases/85375103800845eeff696e53f510c66c428408ed`. **Прежняя версия: NONE**. Установлено `python3.13-venv` с пакетами-зависимостями без новых платных ресурсов.
- Сервис использует `ae-runtime` без sudo и приватный runtime, systemd `Type=notify`, `WatchdogSec=30s`, hardening. Он является **lifecycle-контроллером**; сейчас **не** исполняет пользовательские/LLM-задачи и не открывает API-порт.

## Проверки LIVE

- `systemd-analyze verify` unit — PASS; `systemctl is-active ae-controller.service` → active; `systemctl is-enabled ae-controller.service` → enabled.
- `runuser -u ae-runtime -- /opt/ai-agent-engine/current/venv/bin/python -m ai_agent_engine.controller check --max-age 15` → `controller healthy` до и после тестов.
- `systemctl restart` успешно; `stop` → inactive; `start` → active.
- `WatchdogUSec=30s`, `WatchdogTimestamp` обновился. SIGKILL main процесса через `systemctl kill` — systemd восстановил процесс (другой MainPID, `NRestarts=1`).
- `sudo -n -l -U ae-deploy` и `ae-runtime` → не разрешено; оба только в собственной группе. `ssh` active, `systemctl --failed` 0, резервная root VNC-консоль Netcup SCP проверена, хостовый таймер active.
- При systemctl enable/disable были некритичные сообщения AF_VSOCK generator; рабочее состояние не нарушилось.

## Rollback (первый релиз и последующие)

**Первый релиз:** старого SHA ещё нет, переключать `current` некуда. Проверен безопасный откат функциональности `systemctl disable --now ae-controller.service` (`inactive`, `disabled`); затем успешное восстановление `systemctl enable --now ae-controller.service` (`active`, `enabled`), health PASS. Файлы релиза и данные остаются, других systemd-сервисов не затрагиваем.

**Последующие релизы:** до обновления записывать старый SHA и сверять `current`; проверять signed/trusted artifact; `stage` в отдельный неизменяемый каталог, `promote --source-sha NEW_SHA --expected-current OLD_SHA`; запуск/health. При отказе `promote --source-sha OLD_SHA --expected-current NEW_SHA` и `systemctl restart ae-controller.service`, затем health/журналы. `promotion.py` **сам сервис не перезапускает**. Не удалять пользовательские данные.

## Границы приёмки

Основные технические проверки **PASS** для новых подпунктов 4.8–4.13, но приёмка владельца **4.14** ещё не зарегистрирована в roadmap. Full VPS reboot не тестировался. Проверка GitHub source-level token permissions/guards PASS, но административные settings/branch protection вернули 403 — не называть их проверенными. **4.15–4.19** — следующие отдельные работы: хранилища, БД, секреты, backup и реальное восстановление данных; ещё не выполнялись. Подробные факты — [HANDOFF](../../HANDOFF.md).
