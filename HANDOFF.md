# HANDOFF — 19.11.2026: 6.14 GPT+Claude LIVE PASS, следующий 7.1

**Источник истины:** `vl-ktk/ai-agent-engine/main`, `IMPLEMENTATION_ROADMAP.md`. **Подпункт 6.14 [x] полностью завершён** и проверен живым запуском, следующий по плану — **7.1 [ ]**, не начинать без новой команды Владислава.

- **Живой результат:** [Netcup owner job #44511087941](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44511087941) `SUCCESS`, `STAGE6_14_OWNER_E2E {status:LIVE_PASS, gpt_claude_e2e_pass:true, offline_pass:true, live_request_attempted_once:true, retry:false}`. По одному личному реальному GPT+Claude вызову через официальные подписочные подключения, проверенные в [auth run #44509438297](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44509438297) (`GPT READY`, `Claude logged_in=true`). Никаких повторов.
- **Критерии подтверждены кодом acceptance:** `TaskInbox` → scoped `ActionGrant` → оба provider runner → `SUCCEEDED`/marker → permission/provider audit + ACK → SHA-256 тестового файла → Git baseline/source-version/локальный rollback → SQLite close/reopen/process-ledger. Всё в `TemporaryDirectory`, без изменения чужих репозиториев и рабочей task SQLite.
- **Технический блокер снят:** первая попытка [#44510339972](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44510339972) дала `OFFLINE_BLOCKED`, модели не вызывались; безопасная диагностика [#44510681523](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44510681523) установила `local_git_unavailable` на временном `git init`. Финальный run установил системный Debian Git (`git_installed_now=true`) и затем успешно выполнил offline+live.
- **Проверки:** локальный `py_compile` новых owner-admin скриптов PASS; GitHub owner Actions проверил source/security/workflow, полный Linux Python unittest и wheel до SSH; затем `LIVE_PASS`. Отдельный общий CI по Windows по прежней Linux-only `fcntl` причине может быть красным — не считать его исправленным.
- **Выкладка Engine отсутствует:** версия на Netcup осталась **`0.3.11a0`, release `0e04af007375d82daf8f7d3b36c71abb0c784e0b`**, проверена до/после; предыдущая версия для отката `85375103800845eeff696e53f510c66c428408ed`. Основные службы healthy. Секреты/ответы моделей не попали в Actions. Нового платного API-ключа, сетевого порта, фонового провайдерского daemon не создавали. Финансовые списания/Extra Usage отдельно не проверялись.
- **Исходники и протокол:** [Stage 6.14 acceptance](./infra/orchestrator/STAGE6_14_E2E_ACCEPTANCE.md), [roadmap](./IMPLEMENTATION_ROADMAP.md); owner-admin live script обновлён коммитом `db6ecdcf43195d1cc111e0fe8646fe9c0e250aa8`, владелец инициировал request `f4eba072a23275022d7a115fc57ddd334d6a4dce`. Протокол зафиксирован `182069633daa43f410166c620de8226af4c5b567`, roadmap `6a44fe27b1d69f01bcd14f61e4bd36bd46945887`. Откат исходников только адресным `git revert` после проверки актуального HEAD; не force/reset.
- **Границы:** выполнен личный одноразовый E2E, это не разрешение на unattended/24×7 или публичную маршрутизацию Claude Pro по подписке. **Следующий 7.1:** описать вход главного оркестратора и границы доступного контекста, проверить доступ; после прямого «Давай» владельца.

---

# HANDOFF — 19.11.2026: Stage 6.14 GPT SIWC dependencies PREP PASS / subscriptions NOT SIGNED IN

**Источник:** `vl-ktk/ai-agent-engine/main`; владелец поручил продолжать 6.14 самостоятельно, с минимальными обязательными действиями владельца. Не начинать 6.15/7.1 без реального `STAGE6_14_LIVE PASS`.

- **LIVE Netcup preflight #44482293034:** действующий `ae-runtime` имел код GPT SIWC, но **не имел `PyJWT` и `cryptography`** в установленном release venv; официальный Claude CLI уже готов. Только безопасные булевы результаты, `models_invoked=false`.
- **LIVE Netcup preparation #44482724531 PASS:** создан отдельный root-managed venv `/opt/ai-agent-engine/owner-siwc-6-14/venv/bin/python` с `PyJWT[crypto]>=2.10,<3` и копией `ai_agent_engine` **из действительно установленного release**. Проверено под `ae-runtime`: импорт `jwt`, `cryptography`, `ai_agent_engine.siwc` и команда `auth status`. Создано только изолированное окружение; **не** менялись работающий `/opt/ai-agent-engine/current`, `ae-controller`, timers, SQLite, права/секреты, рабочие данные. Авторизация и модельные запросы не выполнялись. Script commit `0bbd6e1c81c6783133c8b0a604d03ada71c9189f`, owner request commit `1727a0eb23c99badbb163f111b942d1da6bd5e2b`.
- **Выкладка:** осталась прежняя `0.3.11a0`, release `0e04af007375d82daf8f7d3b36c71abb0c784e0b`; rollback `85375103800845eeff696e53f510c66c428408ed`. Отдельный owner SIWC venv можно оставить без влияния на контроллер; удаление только адресным отдельным действием после проверки.
- **Проверенный блокер:** обе подписочные авторизации под `ae-runtime` ещё не выполнены. Для GPT официальный OpenAI SIWC open-source flow требует `http://127.0.0.1:<port>/auth/callback` **на том же хосте, что и браузер** (схема локальной регистрации); Codex CLI device-code login не является подменой для токенов этого SIWC клиента. Для Claude CLI подтверждён `claude auth login`. Не пересылать коды/токены через чат, GitHub/Actions или чужие учётные записи.
- **Следующий шаг:** организовать личный интерактивный браузер на VPS или локальный защищённый SSH-туннель для GPT callback, затем подписочно авторизовать GPT и Claude **под `ae-runtime`**. Выполнить ровно один `/opt/ai-agent-engine/owner-siwc-6-14/venv/bin/python -m ai_agent_engine.task_stage6_14_acceptance --live --owner-approved --provider both` только после проверки обеих авторизаций и с прямым разрешением владельца на реальные модельные вызовы. Проверить аудит, SQLite, rollback и явно отметить PASS/BLOCKED.
- **Тесты:** локальные `py_compile` обоих admin scripts, parser checks PASS; GitHub owner workflow перед SSH проверил исходники и Python unittest; оба owner-запуска SUCCESS. Общий Linux/Windows CI статус отдельно проверить, исторический Windows FAIL не считать исправленным.

---

# HANDOFF — 19.11.2026: Stage 6.14 Netcup Claude CLI PREP PASS / E2E BLOCKED

**Источник:** `vl-ktk/ai-agent-engine/main`. Владелец поручил готовить Netcup под новый личный вход GPT/Claude без переноса старых OAuth токенов. Смена действующего Netcup release и запуск моделей не требовались.

- **LIVE PASS:** [owner workflow #44424028255](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44424028255), result `STAGE6_14_CLAUDE_PREP`: `CLI_READY_FOR_OWNER_LOGIN`, `installed_now=true`, `claude_cli_usable=true`, `models_invoked=false`, `release_unchanged=true`, `services_healthy=true`.
- Установлен **официальный Claude Code CLI** под непривилегированным `ae-runtime`, системный entrypoint `/usr/local/bin/claude` ссылается на пользовательский `~/.local/bin/claude`; версия CLI проверена реальным запуском. Личные авторизации не трогались.
- Backend уже содержит `ai_agent_engine.auth` для GPT SIWC. **GPT OAuth не проводился**, Claude Pro login тоже **не проводился**. Для GPT текущий код использует `127.0.0.1` + динамический порт; открыть ссылку напрямую с iPhone нельзя. Нужны либо браузер на VM/правильное локальное туннелирование, либо официальный локальный SIWC-вход с защищённым переносом сессии на VM с отдельным host ID.
- Проверка перед привилегированным SSH: **664 Python unittest**, source scan, workflow scan, wheel manifest PASS. Сервис `ae-controller` и оба timer проверены до и после установки. **Production engine deploy отсутствует**, release `0e04af007375d82daf8f7d3b36c71abb0c784e0b` не менялся; прежний rollback release `85375103800845eeff696e53f510c66c428408ed`. Новый script commit `e65aabeacb869241a5b5a514486b2465bad0782c`, owner request commit `f4e3164d91c7a5c53b77f3e951edf14da85e5cee`.
- **Следующий шаг:** личная интерактивная авторизация обоих провайдеров под одной и той же VPS учётной записью `ae-runtime` (без вывода токенов в Actions/чат/GitHub); затем один `python3 -m ai_agent_engine.task_stage6_14_acceptance --live --owner-approved --provider both` под этой учётной записью, только при явной готовности двух сессий. После проверки status/audit/SQLite/Git отметить 6.14 PASS или оставить BLOCKED.
- Граница: этот успех **не доказывает** права на круглосуточное фоновое использование подписок. API/ключи/платные услуги не создавались. Временная авторизация для CI/Actions недопустима.
- Для отката установки (если понадобится) нужен отдельный адресный owner script: снять только наш симлинк `/usr/local/bin/claude` после проверки target, затем управлять пользовательской установкой отдельно; не удалять новые файлы/сессии владельца без разрешения. Исходники откатывать адресным revert после проверки нового main; не force-push.

---

# HANDOFF — 19.11.2026: Stage 6.14 LIVE provider auth preflight BLOCKED

**Источник истины:** `vl-ktk/ai-agent-engine/main`; административный канал Netcup уже работоспособен. Владелец поручил продолжить **именно 6.14**. Сам live E2E обеих моделей **НЕ выполнялся** и пункт остаётся [ ].

- Подготовлен безопасный `ops/admin-scripts/stage6_14_preflight.py`, тесты в `tests/test_stage6_14_netcup_preflight.py`; PR #61. Linux CI Python 3.11/3.12/3.13, isolated wheel PASS, Python 3.12 **664 unittest OK**. Windows CI прежний FAIL (`fcntl`).
- **Живой Netcup preflight:** [GitHub Actions #44421225951](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44421225951) **SUCCESS как выполнение административной диагностической задачи**, но результат провайдеров **`AUTH_NOT_READY`**: у `ae-runtime` и `ae-deploy` нет сохранённого личного GPT SIWC входа и `claude` CLI отсутствует. Подписка Claude не обнаружена. Токены/учётные данные в логи не выводились; **models_invoked=false**. Рабочие сервисы/SQLite/публикация не менялись.
- **Netcup release до и после проверки:** по контролю CAS ожидаемый `0e04af007375d82daf8f7d3b36c71abb0c784e0b`. Его механизм проверки сверил; точную текущую рабочую версию перед следующей выкладкой перепроверить. Предыдущий работающий rollback release: `85375103800845eeff696e53f510c66c428408ed`.
- **Следующее действие:** владельческий live E2E на **одной авторизованной Linux-машине с обоими входами GPT и Claude** (ранее Codespace `laughing disco` имел отдельные индивидуальные LIVE PASS; доступ к его shell из данного подключения не подтверждён). При необходимости настроить личные входы в подходящей owner среде через обычную интерактивную авторизацию, **без передачи токенов** и **без платных API**. Когда обе авторизации подтверждены, запустить ровно один `python3 -m ai_agent_engine.task_stage6_14_acceptance --live --owner-approved --provider both`, проверить два реальных ответа/аудит/SQLite/Git rollback. Если нет обеих, сохранить 6.14 [ ] BLOCKED; 7.1 не начинать.
- Не запускать live с root или неавторизованной VPS-учётной записью и не считать localhost mock настоящим GPT/Claude. Никаких повторов модельных запросов при отказе.
- **Исходный GitHub main до этой документальной записи:** `92934d560e64c5d448f50155be5e849b82230aaf`, откат только адресным revert после новой сверки HEAD. Нового release deploy на Netcup не было.

[Технический протокол](./infra/orchestrator/STAGE6_14_E2E_ACCEPTANCE.md).

---

# HANDOFF — 19.11.2026: Netcup full admin setup LIVE PASS (all 5 points)

**STOP:** the owner's five-point administrative task is complete. Do not start
roadmap **6.14** without a new instruction. Its GPT+Claude LIVE E2E remains
unperformed; no provider authorization, paid API or new service was introduced.

- **LIVE PASS:** [Actions #44416162976](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44416162976).
  Connected GitHub → hosted Actions → pinned SSH → ae-deploy sudo root.
  Package smoke, service smoke, deploy, actual rollback, final reactivation,
  health and unchanged SQLite/file inventories all PASS.
- **Installed:** package `0.3.11a0`, VPS release
  `0e04af007375d82daf8f7d3b36c71abb0c784e0b`.
  **Previous/rollback:** `85375103800845eeff696e53f510c66c428408ed`.
  Request commit `141f9362617621324066af6ceca7500eda16b110`.
- **Rights LIVE:** ae-deploy full NOPASSWD sudo, visudo PASS, root/password SSH
  remain disabled. Original configuration: `/root/ae-admin-20261119/`.
  Private online data backup/evidence:
  `/root/ae-admin-jobs/20261119005500000000000000000005/evidence/`.
- **Tests:** 657 hosted unittests OK; Linux 3.11/3.12/3.13 and isolated wheel PASS
  in [CI #44416162937](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44416162937).
  Whole CI remains red for pre-existing Windows fcntl/symlink issues.
- **Cloud autonomy:** the live request/retries, execution and result retrieval
  used only connected GitHub tools after bootstrap; no Windows files/processes,
  local SSH or provider console was part of execution. On-demand administration
  works independently of the PC. A continuously running GPT was not created.
- First startup failure exposed private-umask release permissions; automatic
  recovery to the old healthy version succeeded LIVE. Fixed and regression-tested.
  Seven local backup snapshots verified; working inventory unchanged. Off-host
  recovery remains unconfirmed.

**Full evidence:** [NETCUP_FULL_ADMIN_ACCESS_RESULT.md](./infra/deploy/NETCUP_FULL_ADMIN_ACCESS_RESULT.md).
**How to administer:** [NETCUP_OWNER_ADMIN.md](./infra/deploy/NETCUP_OWNER_ADMIN.md).
Original GitHub source before this task: `0c1c244f6bb2656c8d7c3bcafec981a2fb4a4294`.
Revert sources by addressed commits only; re-read GitHub and VPS before future
operations. Do not mistake later report/status commits for deployed versions.

---

# HANDOFF — 19.11.2026: Netcup baseline status verified, admin console BLOCKED

**Источник:** `vl-ktk/ai-agent-engine/main`. Владелец поручил начать пункт 1 [плана полной административной настройки](./infra/deploy/NETCUP_FULL_ADMIN_ACCESS_PLAN.md).

- **LIVE:** GitHub Actions [#44409546477](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44409546477) PASSED, actor `vl-ktk`, pinned SSH host PASS, `ae-controller.service` + `ae-backup.timer` + `ae-host-check.timer` active, Netcup current `releases/85375103800845eeff696e53f510c66c428408ed`. Исходник тестового status commit `fb4c57579f9c076b5b63c50cf3db948a0137557f`.
- **Блокер пункта 1:** доступна лишь непривилегированная SSH-проверка, нет действующей административной консоли/root в текущей инструментальной сессии. Состояние рабочих данных/backup-интегритет не проверено. Пункт 1 **PARTIAL**, права sudo не выдавались, Netcup не менялся.
- После административного входа проверить права, текущую версию, данные/бэкапы read-only; только потом пункт 2 (согласованная настройка админ-прав). После завершения пунктов 1–5 вернуться ровно к `6.14 [ ] LIVE BLOCKED` для GPT+Claude E2E.
- **Откат GitHub:** исходный main до документальной записи `fb4c57579f9c076b5b63c50cf3db948a0137557f`; не сбрасывать `main` силой, использовать адресный revert после повторной сверки HEAD. **Реальной VPS-публикации не было.**

---

# HANDOFF — 19.11.2026: отдельная задача полного админ-доступа Netcup

**Текущее решение владельца:** прежде чем продолжать разработку AI Agent
Engine, настроить **максимальный постоянный административный доступ** GPT к
существующему Netcup по уже работающей связке GitHub Actions → SSH.
Приоритет — быстро и просто, без нового сервера, отдельного MCP и
ограничений sudo на каждую команду. **Полный план и риски:** 
[NETCUP_FULL_ADMIN_ACCESS_PLAN.md](./infra/deploy/NETCUP_FULL_ADMIN_ACCESS_PLAN.md).

**Состояние:** план записан, root/sudo не настроены, production не менялся.
Живой read-only доступ GitHub → Netcup PASS [#44406398637](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44406398637);
три юнита active, наблюдавшаяся текущая VPS-версия
`85375103800845eeff696e53f510c66c428408ed`.
Следующее: один авторизованный вход в Netcup и настройка
sudo/канала команд, затем реальный административный smoke, проверенная
публикация и откат. **Перед правками** ещё раз сверить живую VPS-версию,
**перед публикацией** GitHub HEAD и VPS; не тереть чужую работу.
Изменения привилегий, секретов и рабочих данных требуют отдельного
согласия владельца. Не отправлять ключи в чат. Ключ для отката исходников
до этой записи: `7661a401f276319817901d2f34af1f2b29f3da23` (только адресный revert, не force).

**Где остановились:** `IMPLEMENTATION_ROADMAP.md` пункт **6.14 [ ] PARTIAL /
LIVE BLOCKED**. После подтверждения полного админ-доступа вернуться именно
к 6.14, провести живой GPT + Claude E2E и только после успешного теста
переходить к дальнейшим этапам.

**Фактические проверки:** live read-only PASS ранее; после этого
документального изменения тесты/деплой Netcup не проводились.

Исторические handoff и технический протокол до решения сохранены
в истории Git по коммиту `7661a401f276319817901d2f34af1f2b29f3da23` и
[старом release-протоколе](./infra/deploy/GITOPS_NETCUP_CONTROL.md).
