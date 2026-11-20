# HANDOFF — 20.11.2026, AI Agent Engine 7.7 / подключение к серверу ожидает инфраструктурной приёмки

## Текущее состояние — отдельное разрешение на Netcup получено

Владелец 20.11.2026 **разрешил адресные изменения существующего Netcup для подключения 7.7**. Прежние указания ниже «нет разрешения на Netcup» относятся к состоянию **до** нового согласия. Windows, новые расходы, секреты и права доступа, рабочие данные без дополнительного согласования остаются вне области изменений.

**Факт live-статуса 20.11.2026:** штатный GitOps read-only workflow [44696209219](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44696209219) **PASS**: `ae-controller.service`, `ae-backup.timer`, `ae-host-check.timer` активны; `/opt/ai-agent-engine/current` → `releases/0e04af007375d82daf8f7d3b36c71abb0c784e0b`. Это зафиксированный **исходный серверный SHA для отката**, его необходимо повторно проверить непосредственно перед выкладкой. Источник этого SHA взят из GitHub для проверки; действующий `controller.py` обслуживает heartbeat/systemd и **не** запускает TaskInbox или GPT/Claude. Коммит `2210d0329f78f3ac9b35f1c575fbbeb0d4000f82` добавил исключительно GitOps status-request — без изменения Netcup.

**Подготовленная интеграция, исходники `0.3.13a0`:** `ai_agent_engine/project_diagnostic_adapter.py` — вызываемый только доверенным runtime bridge между durable `TaskInbox` и `ProjectDiagnosticsStore`: требует подтверждённый `FAILED` transition именно назначенного worker, реальный Git baseline назначенного workspace, совпадающий owner/project source-binding и выводит idempotency key из ID события, а не текста модели. Добавлены offline тесты `tests/test_project_diagnostic_adapter.py`. Нет автозапуска, моделей, новых grants, API, правок runtime SQLite и автоматического восстановления действий. **Мост ещё не подключён к выполняемому серверному task runner**: его нет в развёрнутом контроллере; серверный пакет **не заменён**, 7.7 **[ ] PARTIAL / LIVE NOT DEPLOYED**.

**Остаётся/блокер:** определить утверждённый host task runtime и отдельную допустимость его запуска и новой диагностической runtime SQLite (рабочие данные); подготовить host acceptance и rollback. Существующие owner-only GitHub release transfer/activation требуют **ручного `workflow_dispatch` от `vl-ktk`** и подписанного release intent — не обходить. Перед deployment сверить current SHA, взять исходник выложенной версии, проверить candidate и wheel/manifest, затем разрешённый запуск и read-only host приёмку. Windows и Netcup-сервисы не изменялись. GitHub rollback до кода адаптера: `main` `2210d0329f78f3ac9b35f1c575fbbeb0d4000f82`; после публикации использовать адресный revert feature-коммита без force-push.

**Реальные offline-проверки:** новый bridge + core: `22 OK`; полный `unittest discover`: `807 OK (1 skipped)`; `compileall`, `check_public_source.py`, `check_workflow_boundaries.py` — PASS. `pip wheel --no-deps --no-build-isolation` собрал `ai_agent_engine-0.3.13a0-py3-none-any.whl` (`sha256 0011302c173cfc7e8b4ef3ce8d40c35816866c121b3f842dcdfb9fabada11c48`), архив содержит оба 7.7-модуля. В исходной среде отсутствует `python -m build`, поэтому сборка выполнена доступным `pip wheel`. Никаких серверных интеграционных тестов GPT/Claude и live-приёмки не было.

## Текущая задача 7.7

**Новое решение владельца 20.11.2026 — вариант А прямого подключения диагностического ядра к работающему оркестратору Netcup.** Выбор режима одобрен, но **не дано отдельного разрешения изменять Netcup**: владелец одновременно потребовал не менять Netcup без такого разрешения и не менять Windows. Статус **7.7 [ ] PARTIAL / LIVE BLOCKED**; не считать выбор А выполненной выкладкой, не запускать VPS, внешние модели, миграции, диагностические сборщики, платные сервисы или live-тесты. Что осталось: явное разрешение на изменения Netcup; read-only сверка работающей версии и rollback перед работой, доверенный task/grant/source/QA adapter, безопасные интеграционные тесты, повторная проверка версии непосредственно перед разрешённой выкладкой и live-приёмка. Отдельные ресурсоёмкие функции требуют будущих замеров/разрешений. 7.8 не начинать автоматически.

**Публикация решения А:** только изменение архитектурного протокола, roadmap и этого HANDOFF в GitHub. Исходный `main` до записи — `fcfe3ea4757ba6095aa2948fd7dd9eb8699abf91` для точечного `git revert` после проверки HEAD. Код/версия пакета `0.3.12a0` и реальные системы не меняются. Предыдущее решение вопроса №10/В об offline ядре сохраняется.

**7.7 [ ] PARTIAL — архитектура №1–10/В утверждена Владиславом 20.11.2026; проверяемое Linux/offline ядро реализовано.** Реализованы `ai_agent_engine/project_diagnostics.py`, `tests/test_project_diagnostics.py` и пакет `0.3.12a0`: в доверенном `ProjectLeaderStore` только одобренные owner/project; отдельная private SQLite `diagnostics_7_7.sqlite3` (не центральная task DB), жёсткая binding-карта Git repo ↔ project, повторно проверенный Git baseline/result/dirty SHA 6.13, bounded sanitized failure/immutable occurrence/группировка fingerprint, history/audit, idempotency, максимум три зарезервированные до эффекта попытки, QA callbacks с recheck и требованием фактического изменения версии, `HOLD` после неопределённого crash/recovery без replay. Подробные логи/секреты/неавторизованные команды не выполнять и не собирать. **Проверки: 17 новых offline тестов PASS, полный unittest: 802 OK (1 skipped), compileall PASS, source/CI boundary scanners PASS, wheel `0.3.12a0` собран и содержит модуль.** Реальная семантическая оценка гипотез, независимая идентичность QA-исполнителя, живые task/grant/process gates, приём из GPT/Claude, архивные бюджеты/TTL, очистка и серверная приёмка ещё **не реализованы/не разрешены**; не объявлять 7.7 полностью закрытым, 7.8 не начинать без отдельной команды. [Протокол](./infra/orchestrator/PROJECT_DIAGNOSTICS_7_7.md).

**Публикация offline ядра 7.7 №10:** код, тесты, дорожная карта и протокол в GitHub `main`; предыдущий SHA для адресного отката до публикации `a9406880cdb4807ee39ab1016cd264f140077629` (только `git revert` после проверки HEAD; без force-push). Новая версия Python-пакета `0.3.12a0`, исключительно исходники без deployment. **Windows и Netcup не менять:** ни запусков, ни live-проверок, ни расходов, ни правок workflows. Следующий шаг — отдельное согласование живой интеграции и замеров после приёмки offline ядра.

**CI GitHub после публикации [f8f33fe](https://github.com/vl-ktk/ai-agent-engine/commit/f8f33fe0a8b3a166c11ce3c894a22e89cb705bd6):** [run 44693742817](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44693742817) — Ubuntu Python 3.11/3.12/3.13 **PASS**, Build and exercise isolated wheel **PASS**; Windows Python 3.12 **FAIL** (в логе `ModuleNotFoundError: fcntl`, всего 30 import errors и 1 failure), поэтому общий CI **не зелёный**. Windows не исправлять и workflow не менять без отдельного указания владельца. Для отката реализации требуется проверка HEAD и адресный revert feature-коммита `f8f33fe0a8b3a166c11ce3c894a22e89cb705bd6`; предыдущее состояние исходников `a9406880cdb4807ee39ab1016cd264f140077629`.

## Предыдущий выполненный этап 7.6

## Текущая задача

**7.6 [x] в согласованной offline-границе №10 / В.** Все архитектурные вопросы №1–10: выбор **В**; детали — `infra/orchestrator/PROJECT_LEADER_7_6.md`. Реализован доверенный `ProjectMemoryStore` (`ai_agent_engine/project_memory.py`) поверх approved 7.5 project workspace: проектная SQLite v2 с атомарной миграцией v1, значимые append-only события с provenance и проверяемым статусом, idempotency, supersession, краткая сводка с CAS `expected_revision`/историей/безопасным восстановлением, bounded scoped SQLite search и выдача маркированного недоверенного JSON-контекста при обязательном настоящем счётчике токенов. Явные секреты отбрасываются при записи, некоторые явные prompt injections дают audit/HOLD. Не добавлялись полномочия owner, specialist/LLM, grants или доступ к чужой SQLite. **Границы безопасности:** доверенный вызывающий слой отвечает за настоящую проверку scope/внешнего источника; `verified` и SHA сами по себе не являются внешней аттестацией; эвристика injection/secret не исчерпывающая, свободный текст семантически не верифицирован. **OPEN / DEFERRED** без измерений/разрешений: FTS5/embeddings, semantic rerank, фоновые/пакетные индексаторы, owner-wide каталог уроков, живые модели, сетевой UI/auth, производственные квоты/TTL и интеграция backup. Только code/offline, не deploy.

**Windows и Netcup:** владелец прямо запретил их изменения. Workflow Windows не исправляли, прежняя Windows CI failure Linux-only `fcntl` остаётся; новые live-проверки не проводили. Нет изменений Netcup, рабочих данных, секретов, платных сервисов и прав доступа.

**Проверки:** `tests/test_project_memory.py` — **23 PASS**; тесты 7.5 — 19 PASS; весь проект — **785 unittest OK (1 skipped)**; `compileall`, source/workflow checks и wheel — результаты дополнительно ниже/в итоговом отчёте. Исходник для новой реализации: публичный GitHub `main` `05959feab4171b8a5cb6bb1198ced997a0dd057a` (точный id до изменений для отката). Выложенная source revision — новый commit `main` по проверке публикации; Python distribution остаётся `0.3.11a0`. Откат исходников: адресный `git revert` коммита 7.6 после перепроверки HEAD, **без force-push**. Netcup-откат не требуется. Следующий 7.7 запускать только по отдельной команде владельца.

## Предыдущий выполненный этап 7.5

**7.5 [x] в выбранной владельцем границе №11 / А, offline.** Вопросы №1–10: архитектура согласована (изолированные project-scoped папки/SQLite, однократное создание после owner approval, управленческие материалы без копии исходников, scoped права/гранты, безопасность параллельной работы, восстановление, резервные копии, слои хранения, lifecycle). Точные решения и оговорки: `infra/orchestrator/PROJECT_LEADER_7_5.md`. №11 / А разрешает сейчас только фундамент 7.5; согласование архитектуры остальных возможностей **не означает**, что они реализованы.

### Что реализовано в исходнике

- `ai_agent_engine/project_leader.py`: инертная scoped-заявка `draft_project` с SHA-256; точная проверка аутентифицированного вызывающим кодом `OwnerDecision`; реестр одобрений в собственной приватной `project-leaders.sqlite3`. Идемпотентное создание после одобрения, устойчивость к прерыванию состояния `provisioning` и fail-closed на исчезнувшей/чужой активной БД.
- Раздельные `<projects_root>/<owner_id>/<project_id>/leader.sqlite3` и `documents/`, каталоги 0700, файлы 0600, owner/project scope и проверка symlink, владельца/режима/идентичности. Общая task SQLite и очередь не изменены; постоянная копия кода не создаётся.
- Минимальный `management_refs` с типизированными ссылками на план/задачи/решения/QA/ошибки/checkpoints/историю/контекст/исходник. Только доверенный локальный store; `verified` фиксирует заявленную отметку и checksum, но **не удостоверяет** внешний источник самостоятельно.
- `tests/test_project_leader.py`: 19 offline-тестов на одобрения, повтор, параллельность, изоляцию, отказ при повреждениях/симлинках и восстановление provisioning. Совместимость с текущей task SQLite проверяется отдельно.

### Проведённые проверки

- `python -m unittest discover -s tests -p test_project_leader.py -q` — **19 PASS**.
- `python -m unittest discover -s tests -q` — **762 tests OK, 1 skipped** (полный offline-регресс после финальной реализации).
- `python -m compileall -q ai_agent_engine`, `python scripts/check_public_source.py`, `python scripts/check_workflow_boundaries.py` — **PASS**.
- `python -m pip wheel --no-build-isolation --no-deps .` — **PASS**, пакет `ai_agent_engine-0.3.11a0-py3-none-any.whl` (version пакета не изменяли, это только новая исходная ревизия 7.5). Живых E2E/Netcup проверок не было.

### Границы и следующее

**Не включено:** HTTP/UI/достоверная идентификация владельца в сетевом интерфейсе, живые модели, запись специалистов, сквозные runtime grants, полноценная память 7.6, CAS/merge документов, восстановление задач, backup/restore, автоочистка, PAUSED/ARCHIVED. Без новых owner-approved квот и полных интеграционных тестов эти функции не активировать. **Netcup не трогали** — изменения только в GitHub-исходнике; live/deploy проверки не выполнялись.

**Следующий плановый пункт:** 7.6 после отдельной команды владельца, начиная с чтения этого HANDOFF и проверки актуального `main`. Прежний HEAD до 7.5: `53d64eae7cafcd66a76eb13b681822afd8bc5824`; для отката исходников использовать адресный `git revert` нового коммита 7.5 после сверки актуальной ветки, не force-push. Netcup откат не требуется, потому что выкладки не было.

## Предыдущий выполненный этап 7.4

## Задача и решение владельца

Пункт **7.4 [x] в согласованной offline-границе №9 / А**: owner-only переназначение специалистов после полной проверки старого process-group, новой специализации, grants, checkpoint и точного SHA/TTL согласия. Все архитектурные решения №1–8 записаны в `infra/orchestrator/MAIN_ORCHESTRATOR_7_4.md`; дорожная карта обновлена. Кризисный узкоспециализированный вызов из отдельного `CRISIS_SPECIALIST_ESCALATION_POLICY.md` **не является** переназначением 7.4.

## Что сделано в GitHub-исходнике

- `ai_agent_engine/main_reassignment.py`: предложение и owner-карточка, согласование `OwnerDecision`, immutable identity `task_id`+`attempt_id`, чекпоинты SHA с verified/unverified и независимой QA-границей, новый attempt после FAILED/CANCELLED/TIMED_OUT только с `EffectClearance`, запрет на SUCCEEDED, scoped history, отказ при истечении/устаревании approval и повторная проверка перед `ready`.
- `ai_agent_engine/task_storage.py`: приватная схема SQLite v14 и миграции старых v1–v13, таблицы proposal, attempts, current, artifacts и событий. Изменение **только** приватного формата при открытии будущей версии; чужие БД не изменять.
- `ai_agent_engine/task_api.py`, `task_control.py`, `task_permissions.py`: блокировка повторного запуска/смены старого `TaskState` и старых действий после переназначения; per-task barrier предотвращает гонку между owner transfer и чувствительным callback.
- `observe_unavailable` принимает доверенную, утверждаемую отдельно политику приоритетов с конечными сроками/числом опросов и **только советует** `wait` или `suggest_alternative`; сам не меняет агента, не выдаёт права и не запускает модель. Предложение нового агента всегда требует нового owner approval.
- `tests/test_main_reassignment.py` плюс обновлённые ожидания private schema v14 в миграционных тестах. Fake-процессы, негативные grants/hold/scope/TTL/replay/checkpoint/priority и callback-race проверяются offline.

## Граница результата

**Ничего на Netcup не выкладывали и не меняли.** Реальные GPT/Codex/Claude Code, подписочная авторизация, рабочая панель с кнопками и production task-runner этим этапом **не активированы**. Метод `activate_transfer` записывает только `ready` (не процесс). Отсутствуют утверждённые числовые wait/queue-пороги и окончательная owner-approved policy версии 7.11–7.12; тестовые значения не являются рабочими настройками.

## Проверки и версионность

- Проверка модуля 7.4: **21 unittest PASS**; полный прогон после последних правок и wheel/compileall — см. журнал текущей реализации/коммит GitHub.
- Начальный исходник `main` для реализации: `34b856690694b6923e33fa2ab2cda328717d3775`; исходный tree `88d248dd667c1646231821218994ef67a5e85e4d`.
- Netcup **не проверялся и не менялся** в этой задаче. Последний ранее зафиксированный в проекте выпуск: `0.3.11a0`, release `0e04af007375d82daf8f7d3b36c71abb0c784e0b`; это не свежая диагностика сервера. Для source rollback использовать адресный `git revert` коммита 7.4 после проверки актуальной ветки, **без force-push**. Netcup rollback не нужен, потому что выкладки не было.

## Следующее

Перейти к обсуждению следующего архитектурного пункта по очереди дорожной карты. Перед реальным включением переназначения понадобится trusted owner-authenticated UI/adapter, отдельная execution integration нового `attempt_id`, проверка подписок/сессий, approval конкретных приоритетных числовых лимитов, затем отдельные live/E2E и решение владельца о production deployment. Не считать текущий offline PASS доказательством работы моделей на сервере.
