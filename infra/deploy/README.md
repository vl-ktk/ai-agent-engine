# 4.2 — проверяемая выкладка контроллера (инструкция, НЕ разрешение)

**Состояние:** код и офлайн-репетиция; **не запущено на Netcup**. Без отдельного явного разрешения Владислава запрещено выполнять команды, меняющие `/opt/ai-agent-engine`, systemd, права/секреты либо production. Это доказательная инструкция по текущему подпункту 4.2, не отдельный план.

## Границы готового ПО

- `ai_agent_engine.controller` — настоящий долгоживущий **lifecycle-процесс** с одним экземпляром, приватным heartbeat, `READY=1` и `WATCHDOG=1` для systemd. Сейчас он **не** исполняет задания/LLM и **не** является оркестратором этапа 5. Портов и внешних соединений не открывает.
- `infra/systemd/ae-controller.service.template` использует root-owned `/opt/ai-agent-engine/current/venv/bin/python`, `ae-runtime` без логина, приватный `/run/ai-agent-engine`, `Type=notify`, `WatchdogSec=30s` и sandboxing. Это **исходник шаблона**; на VPS не установлен.
- `infra/deploy/promotion.py` отдельно проверяет SHA-256 артефакта, создаёт новый неизменяемый каталог `releases/<commit>`, и по явной команде оператора атомарно меняет ссылку `current`. **Не** изменяет sudo/SSH/Netcup firewall и **не** перезапускает/включает сервис. Повторное создание существующего релиза отвергается.
- `.github/workflows/release-preparation.yml` только готовит wheel+manifest по закреплённому Git SHA, с повторной проверкой `main`. Не содержит деплоя. Живая ручная проверка этого workflow ещё не подтверждена.

## Условия перед первой реальной выкладкой

- Отдельное разрешение владельца на **конкретную** production/VPS-запись и установку/запуск unit, с известным окном и действием при сбое.
- Закрыть оставшиеся read-only аудиты минимально необходимых sudo/резервной SCP-консоли/права на запуск и доступ к CI; разрешить административные изменения только отдельно. `ae-deploy` не должен получать постоянный root.
- Проверить текущую версию VPS и права каталогов; сделать и проверить резервную копию нужного состояния. Сейчас `/opt/ai-agent-engine` и `releases` были root:root 0755, `/var/lib/ai-agent-engine` — `ae-runtime` 0700; это исторический аудит, не гарантия актуального состояния.
- Получить успешный **owner-triggered** release-preparation artifact по точному Git SHA. Независимо сверить commit, workflow run, wheel SHA-256 и исходник `promotion.py` с доверенным `main` перед переносом. **Одна только контрольная сумма из того же скачанного архива не доказывает происхождение.**

## Репетиция и последующая ручная операция

На безопасной тестовой машине создать пустой корень `.../ai-agent-engine/releases`, затем проверить артефакт и работу `verify → stage → promote → controller check`. CI делает такую репетицию **только в собственном временном каталоге runner**, без VPS. Для настоящего хоста нужен trusted `promotion.py` из утверждённого commit и защищённо доставленные wheel+manifest.

При отдельном разрешении и подтверждённых переменных оператора:

```bash
# Пример интерфейса, НЕ команда к автоматическому выполнению.
python3 promotion.py verify --bundle /SAFE/approved-artifact \
  --source-sha EXACT_40_HEX_SHA --expected-wheel-sha256 EXACT_64_HEX_DIGEST

python3 promotion.py stage --bundle /SAFE/approved-artifact \
  --root /opt/ai-agent-engine --source-sha EXACT_40_HEX_SHA \
  --expected-wheel-sha256 EXACT_64_HEX_DIGEST \
  --confirmation OWNER_APPROVED_RELEASE_OPERATION

python3 promotion.py promote --root /opt/ai-agent-engine \
  --source-sha EXACT_40_HEX_SHA --expected-current NONE \
  --confirmation OWNER_APPROVED_RELEASE_OPERATION
```

`NONE` допустимо **только** при первой выкладке без предыдущей ссылки `current`; впоследствии указывать фактически проверенный предыдущий SHA. Скрипт не даёт произвольных shell-команд и откажет при несовпадении ожидаемой версии.

После проверки подготовленного unit доверенным оператором: установить его от root в `/etc/systemd/system/ae-controller.service`, выполнить `systemctl daemon-reload`, `systemctl enable --now ae-controller`, убедиться в `active`, а затем **вживую** проверить `python -m ai_agent_engine.controller check` под `ae-runtime`, 2+ успешных watchdog-пульса, `systemctl show ...`, рестарт, автозапуск после перезагрузки (только с разрешения владельца), отсутствие ошибок, сохранность SSH и готовность отката. Не открывать новый сетевой порт.

## Откат

При обновлении сначала сохранить **предыдущий** SHA. Если запуск или health нового релиза не прошёл — под разрешённым оператором переключить `current` назад командой `promote --source-sha PREVIOUS_SHA --expected-current FAILED_SHA` и снова перезапустить сервис; проверить здоровье и журнал. Скрипт не перезапускает сервис автоматически. Для **первой** выкладки с `NONE` предыдущего работающего релиза нет: безопасное восстановление — остановить новый unit и оставить исходные серверные службы нетронутыми; дальнейшее удаление ссылки/данных требует отдельного решения. Нельзя удалять данные пользователя или изменения других проектов при откате.

**Требования к приёмке 4.2:** фактические SSH/sudo/SCP проверки, GitHub run ID, release SHA + wheel digest, unit status/health/watchdog/restart, проверенный порядок rollback, прежняя версия и решение владельца. Офлайн-тесты, даже полностью зелёные, этого не заменяют.
