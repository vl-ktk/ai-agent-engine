# 4.23 — admin GitHub settings и итоговая проверка безопасности действующего VPS

Дата: **18.11.2026**. Объект только **AI Agent Engine**: GitHub [`vl-ktk/ai-agent-engine`](https://github.com/vl-ktk/ai-agent-engine), `main`, уже работающий Netcup VPS `v2202611460959561466`. Это **аудит, а не утверждение об отсутствии уязвимостей**. Настройки, права, секреты, рабочие данные и код действующего сервера во время 4.23 не изменяли.

## GitHub: реально просмотренные административные страницы

Проверено в авторизованном веб-интерфейсе GitHub как `@vl-ktk` с правами администратора; прежний **403 API-интеграции** не считается результатом этой проверки. Доступ и состояние подтверждены на 18.11.2026, а не только исходниками workflows.

| Где | Подтверждённое состояние | Оценка |
| --- | --- | --- |
| [`Settings → Branches`](https://github.com/vl-ktk/ai-agent-engine/settings/branches) | В списке нет classic branch protection rules; предлагается только создать правило. | **Риск**: нет classic-защиты `main`. |
| [`Settings → Rules → Rulesets`](https://github.com/vl-ktk/ai-agent-engine/settings/rules) | GitHub показывает `You haven't created any rulesets`. | **Риск**: нет ruleset-защиты `main`; нельзя считать ветку защищённой. |
| [`Settings → Actions → General`](https://github.com/vl-ktk/ai-agent-engine/settings/actions) | Включено `Allow all actions and reusable workflows`. Обязательный pin на полный commit SHA **выключен**. | **Риск**: разрешены произвольные сторонние Actions без административного требования SHA-pin. |
| Там же | `GITHUB_TOKEN` по умолчанию — **read-only** (`Read repository contents and packages`), разрешение Actions на создание/одобрение PR **выключено**. | Положительные ограничения. |
| Там же | Утверждение PR из fork требуется для **first-time contributors**; срок хранения Actions — **90 дней**. | Зафиксированная политика, не проверка каждого отдельного workflow. |
| [`Settings → Collaborators`](https://github.com/vl-ktk/ai-agent-engine/settings/access) | Просмотр списка остановлен GitHub `Confirm access` с повторной проверкой через email. | **Не проверено**: полный состав участников/приглашений. Не обходили reauthentication. |

Через GitHub-интеграцию независимо подтверждены метаданные: репозиторий **public**, ветка по умолчанию **main**, у `vl-ktk` — **admin**. Вход в administrative settings через веб-интерфейс позволяет снять прежний общий блокер «403 = админские настройки не проверены», но не означает проверки страницы, защищённой дополнительной аутентификацией.

## Netcup: живые read-only проверки

В авторизованной существующей панели Netcup SCP и root-консоли проверено:

- VPS `v2202611460959561466` **RUNNING**; предыдущее live-инвентаризованное окружение: Debian **13.7**, контроллер `0.3.8a0`, релиз `/opt/ai-agent-engine/releases/85375103800845eeff696e53f510c66c428408ed`. В 4.23 релиз **не меняли**.
- `ss -ltn`: только **`0.0.0.0:22`** в состоянии `LISTEN` на момент проверки. Это снимок TCP-слушателей в гостевой ОС, не внешний port scan.
- Эффективное `sshd -T`: `permitrootlogin no`, `passwordauthentication no`, `kbdinteractiveauthentication no`, `pubkeyauthentication yes`.
- `systemctl is-active ssh.service ae-controller.service ae-backup.timer ae-host-check.timer`: **все четыре `active`**.
- Netcup SCP → Firewall: **`Firewall active` включён**; применяется пользовательская политика **AI Agent Engine VPS - SSH and NTP ingress** (входящий SSH TCP/22, входящий UDP-ответ NTP с source 123), а также стандартные правила Netcup SMTP block (исходящие 25/465/587) и разрешения ping IPv4/IPv6. Последними неявными правилами являются **drop all incoming** / **accept all outgoing**. Не меняли порядок и правила.
- `systemctl --failed --no-pager` показывает **четыре FAILED временных `run-p…-i….service` от `systemd-run`**, относящихся к python/backup-командам. Это **не нулевое число отказов**; по одному списку unit **нельзя доказать причину** и нельзя назвать их успешными. При этом постоянные `ae-controller.service` и оба проверенных таймера активны. Не выполняли `reset-failed`, удаление записей или опасные исправления.
- В предыдущем live-шаге 4.22 отдельно проверены 5 root-owned systemd units, исходные SHA-256 backup-кода и units, **7 локальных снимков** и `backup verify PASS bytes=53834 retained=7`. В 4.23 этот backup verify **не запускали повторно**. Независимой off-host копии **нет**; это открытый DR-риск при потере VPS/диска.

## Итог, границы и возможные решения

**Сам пункт 4.23 «провести отдельную проверку» выполнен.** Проверка обнаружила открытые вопросы, а не устранила их:

1. **Высокий приоритет для отдельного решения владельца:** включить защиту `main` (ruleset/branch protection) с согласованными обязательными проверками и правилами прямой записи. Это **изменение прав и порядка публикации**, в 4.23 не разрешалось и не делалось.
2. **Требует отдельного решения:** ограничить разрешённые GitHub Actions и/или обязательный SHA-pin после проверки совместимости workflows. Политики Actions **не изменяли**.
3. **Неполная видимость:** полный список collaborators/приглашений не просмотрен из-за GitHub sudo email challenge; можно отдельно завершить при повторном подтверждении владельца.
4. **Незакрытые операционные наблюдения:** четыре FAILED временных units — установить причины по журналам при отдельной диагностике; один первый отказ резервирования из 4.18 остался без точной причины, но после него были успешные запуски. Полная off-host восстановимость отсутствует, несмотря на успешные локальные проверки.

**Не проверялось:** внешний скан всего адресного диапазона/IPv6, pentest, каждый установленный GitHub Action, состав collaborators после reauthentication, восстановление с независимого устройства, полное удаление VPS. Нет оснований объявлять «сервер полностью безопасен».

**Исходное состояние для отката документации:** GitHub `main` до 4.23 — `d95fccaeed0df3e69578b771416f08f903f5f6a7`; отмена только точечным обратным коммитом поверх актуального `main`, без force push. Контроллерный релиз `85375103800845eeff696e53f510c66c428408ed` не менялся, серверный rollback не требуется. Следующий пункт плана — **4.24**, только по отдельной команде владельца.

## Продолжение по отдельному разрешению владельца — 18.11.2026

Исходный раздел выше — **снимок выполненного аудита**, но не окончательное состояние после доработки. Владелец прямо разрешил исправить обнаруженные проблемы GitHub и сервера. Поэтому единый план возвращает 4.23 в **`[ ] PARTIAL/BLOCKED`** до успешного сохранения admin-политик. 4.24 не начинать.

- **Исходники защищены от mutable Action tags:** после `git ls-remote` официальных GitHub `actions/*` tag refs все **14 Action `uses` в 8 workflows** закреплены полными 40-символьными SHA. Дополнены `scripts/check_workflow_boundaries.py` и два regression tests — попытка использовать `@v*` снова падает в CI. GitHub commit **`d2ecc84036742fb416e1cddaedb44e78a50e09ce`**; локально 255 unittest OK (1 skipped), compileall и source/workflow security checks PASS. GitHub Actions [CI #44294405496](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44294405496) и [VPS automation config #44294405379](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44294405379) **SUCCESS**. **Это не означает, что административная настройка SHA-pin уже включена.**
- **Причина системных transient failures найдена в live journal:** три разовые команды `systemd-run` получили `status=2/INVALIDARGUMENT`; четвёртая пыталась выполнить `/usr/local/libexec/ae-backup.py` напрямую и получила `Exec format error`/`status=203/EXEC`. Последующие пробы `python3 ... ae-backup.py create` успешно завершались. После изучения журнала очищены только флаги исторических временных units: `systemctl reset-failed 'run-p*'`; live `systemctl --failed --no-pager` → **`0 loaded units listed`**. Записи журнала остались, рабочие файлы/данные/systemd-unit definitions не трогали. Это не доказательство причины более раннего штатного `ae-backup.service` FAIL из 4.18.
- **Ruleset GitHub не удалось сохранить:** через `Settings → Rulesets → New branch ruleset` составлено правило `Protect main from deletion and force-push` с `Active`, include `main`, пустым bypass, `Restrict deletions` и `Block force pushes`. Обязательные PR/status checks не выбраны, чтобы сохранить существующий owner-controlled прямой fast-forward deployment исходников. Нажатие `Create` вызвало `Confirm access`; GitHub отправил email OTP, его защищённый ввод завершился сообщением **`Sudo authentication failed`**. Применённого ruleset **нет**; обхода auth и повторных попыток с произвольными кодами не было.
- **Осталось после завершения sudo email-верификации владельца:** фактически создать и отдельно перепроверить Active ruleset; открыть `Settings → Actions`, ограничить allowlist до GitHub-owned Actions (текущий source использует `actions/*`) и включить `Require actions to be pinned to a full-length commit SHA`; проверить, что CI и разрешённые owner-manual workflows не сломаны; прочитать collaborators/access и зафиксировать фактический состав без раскрытия секретов. После повторно проверить состояние `main` и источник, обновить `HANDOFF.md` и план. Без этих живых проверок пункт 4.23 **не закрывать**.

Откат **только кода SHA-pin** — адресный reverse commit для `d2ecc84036742fb416e1cddaedb44e78a50e09ce` поверх текущего `main`, без force/reset. Код контроллера на VPS не обновлялся, откат серверного релиза не нужен. Off-host backup ещё не создан, расходные операции не запускались.
