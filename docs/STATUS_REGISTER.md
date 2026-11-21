# AI Agent Engine — реестр фактических статусов

**Снимок на 21.11.2026. Базовый GitHub `main` перед WEB-01: `06e30efb347f344b084788a3850bfddaecaf0681`.** Этот файл — короткий индекс, а не замена [единому плану](../IMPLEMENTATION_ROADMAP.md), [HANDOFF](../HANDOFF.md), [Web-first gate](../infra/orchestrator/CHATGPT_WEB_FIRST_GATE.md), исходникам и протоколам испытаний. Если описание расходится с кодом или свежими журналами, проверять первичные доказательства, не повышать статус задним числом.

## 1. Проверенные флажки дорожной карты

| Этап | Отмечено `[x]` / всего | Честный статус на текущий момент |
|---|---:|---|
| 1. Авторизация моделей | 13/17 | **ЧАСТИЧНО:** открыты 1.12 (длительный фоновый режим), 1.14 (real refresh/revoke), 1.15 (перенос авторизации), 1.17 (финальное решение GO/NO-GO) |
| 2. Архитектура, роли, права | 0/14 | **ОТКРЫТО:** архитектурные решения обсуждены и частично документированы, но чекбоксы 2.1–2.14 не закрыты испытаниями |
| 3. Open-source движок | 14/14 | **ЗАВЕРШЕНО В РАНЕЕ ПРИНЯТОМ OFFLINE-ОБЪЁМЕ:** это каркас, а не живой автономный GPT-оркестратор |
| 4. Netcup, безопасность, backup | 24/24 | **ПРИНЯТО В СОГЛАСОВАННОМ ОБЪЁМЕ:** Netcup и семь локальных снимков; копия вне VPS при полной утрате не доказана |
| 5. Системное ядро задач | 14/14 | **ПОДПУНКТЫ ЗАКРЫТЫ:** API/SQLite/DAG/permission/retry/process/offline интеграция проверены; это не доказательство самостоятельной работы всех агентов в production |
| 6. GPT/Claude-исполнители | 14/14 | **ПОДПУНКТЫ ЗАКРЫТЫ В УЗКОЙ ОБЛАСТИ:** source/offline контракты и ограниченные owner-approved одноразовые live GPT SIWC + Claude; это **не** обычный Web ChatGPT |
| 7. Главный ИИ-оркестратор | 7/14 | **ЧАСТИЧНО:** 7.1–7.6 и 7.10 отмечены `[x]` в описанном source/offline объёме; 7.7–7.9, 7.11 частично открыты; 7.12–7.14 не начинались |
| 8. Общение агентов и QA | 0/14 | **НЕ НАЧАТО:** схемы/частичные offline контракты этапа 7 ещё не заменяют живой MessageBus и QA |
| 9. MCP и рабочая панель | 0/12 | **НЕ НАЧАТО** |
| 10. Память и обучение процессов | 0/14 | **НЕ НАЧАТО КАК ОТДЕЛЬНЫЙ ЭТАП:** отдельная проектная память 7.6 существует offline; общая база, AI School/AI University не реализованы |
| 11. Пилот, подключение проектов, город | 0/18 | **НЕ НАЧАТО** |

**Итого по прямым подпунктам:** **86 `[x]` / 169**, **83 `[ ]`**. Метка `[x]` означает только доказанный **конкретный объём** соответствующего подпункта, **не** общий production PASS всей системы. Отдельная **7.7A — Secure Vault** есть в roadmap как открытый infrastructure BACKLOG **вне счётчика 169**, не забывать перед масштабированием новых проектов.

**Контроль выполнения подключения:** внутри [7.11 плана](../IMPLEMENTATION_ROADMAP.md) **20 внутренних WEB-01…WEB-20: 8/20 `[x]`, 12/20 `[ ]`**. WEB-01 — [source review](../infra/orchestrator/CHATGPT_WEB_WEB01_REFERENCE_REVIEW.md); WEB-02 — [adapter](../infra/orchestrator/CHATGPT_WEB_WEB02_ADAPTER.md), 12 unit PASS; WEB-03 — [agent sessions](../infra/orchestrator/CHATGPT_WEB_WEB03_SESSIONS.md), 14 unit PASS; WEB-04 — [Project gate](../infra/orchestrator/CHATGPT_WEB_WEB04_PROJECT_MEMBERSHIP.md), 14 unit PASS; WEB-05 — [mock ChatManager](../infra/orchestrator/CHATGPT_WEB_WEB05_CHAT_LIFECYCLE.md), 17 unit PASS, [CI #44889580956](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44889580956) Linux + wheel PASS, 17 WEB-05 Windows unit PASS, общий Windows FAIL по [CI-WIN-001](./KNOWN_ISSUES.md). **WEB-06 `[x] OFFLINE MOCK PASS`** — [model catalog/policy/selection](../infra/orchestrator/CHATGPT_WEB_WEB06_MODEL_SELECTION.md), 18 unit PASS, source `0.3.31a0`, без fallback при чужом/неподтверждённом model ID. [CI #44891594867](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44891594867): Linux 3.11/3.12/3.13 и wheel PASS, общий Windows FAIL по CI-WIN-001, все 18 WEB-06 Windows unit PASS. **WEB-07 `[x] OFFLINE MOCK PASS`** — [model-specific reasoning choice, policy и proof](../infra/orchestrator/CHATGPT_WEB_WEB07_REASONING_SELECTION.md), 22 unit PASS, source `0.3.32a0`, no fallback. **WEB-08 `[x] OFFLINE MOCK PASS`** — [scoped versioned project instructions](../infra/orchestrator/CHATGPT_WEB_WEB08_INSTRUCTIONS.md), 23 unit PASS, source `0.3.33a0`; чужие/устаревшие инструкции и непроверенный grant блокируются. **WEB-09…20 `[ ]`**, следующий WEB-09. 169 прямых подпунктов/86 ранее выполненных не меняются. Полный `WEB-16 OFFLINE_PASS`, `LIVE_PASS`, лимиты обычного ChatGPT, `POLICY_GO` и итоговый `INTEGRATION_GO` **не достигнуты**.

**WEB-06 локальные проверки:** 18/18 новых unit PASS; общий регресс **1151 OK (3 skipped)**, wheel `0.3.31a0` и `compileall` PASS. `WEB-06` принят только в offline/mock-объёме, реальный UI и пределы подписки остаются неподтверждёнными.

**WEB-07 проверки:** 22/22 новых unit и 1173 локальный regression OK (3 skipped), source `0.3.32a0`, compileall/wheel PASS, reasoning proof до BrowserPort внутри FIFO. [GitHub CI #44894136371](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44894136371): Linux 3.11/3.12/3.13 и wheel PASS, Windows global FAIL на CI-WIN-001, все 22 новых Windows unit PASS. [Протокол](../infra/orchestrator/CHATGPT_WEB_WEB07_REASONING_SELECTION.md). Mock не означает реального выбора effort в UI.

**WEB-08 локальные проверки:** 23/23 новых unit PASS, полный регресс **1196 OK (3 skipped)**, wheel `0.3.33a0` с instruction bootstrap, compileall и diff check PASS. Итог CI фиксируется по фактическому завершению; [протокол WEB-08](../infra/orchestrator/CHATGPT_WEB_WEB08_INSTRUCTIONS.md).

## 2. Точка остановки — текущий 7.11

| Компонент | Что реально сделано | Чего ещё нет / что держим HOLD |
|---|---|---|
| `chatgpt_web` | WEB-01/02 source/adapter, [WEB-03 scoped sessions](../infra/orchestrator/CHATGPT_WEB_WEB03_SESSIONS.md), [WEB-04 mock Project gate](../infra/orchestrator/CHATGPT_WEB_WEB04_PROJECT_MEMBERSHIP.md), [WEB-05 mock ChatManager](../infra/orchestrator/CHATGPT_WEB_WEB05_CHAT_LIFECYCLE.md), [WEB-06 mock model selection](../infra/orchestrator/CHATGPT_WEB_WEB06_MODEL_SELECTION.md), [WEB-07 mock reasoning](../infra/orchestrator/CHATGPT_WEB_WEB07_REASONING_SELECTION.md), [WEB-08 scoped instruction bootstrap](../infra/orchestrator/CHATGPT_WEB_WEB08_INSTRUCTIONS.md), source `0.3.33a0` | **НЕТ** реальных ChatGPT Project/chats, подтверждённой UI membership, browser profiles, durable mailbox/restart, переноса контекста, model/effort UI verification, TaskInbox integration, WEB-16 full offline acceptance, live подключения и обычного Chat allowance; `POLICY_HOLD` |
| `SharedCapacity` | Исходники + SQLite v17, offline тесты, disposable Netcup **GPT SIWC + Claude** owner-approved E2E PASS | Нет включения во все вызовы постоянного диспетчера; production лимиты не утверждены; рабочая SQLite остаётся v14 |
| 7.7 QA incidents | Часть source-only диагностики, протоколов и разовых gate/тестовых заготовок | Полный живой поток дефектов и production-приёмка не завершены |
| 7.8 Эскалация | Source-only/частично согласованные правила кризисного специалиста | Нет end-to-end живой эскалации/доставки владельцу |
| 7.9 Руководители направлений | Source-only owner-approved contract + SQLite v16, offline tests | Нет live owner-intake и назначения в работающем оркестраторе; `DEFERRED/PARTIAL` |
| 7.10 Обмен подзадачами | `[x]` контракт и offline QA узких scoped peer subtasks | Живой обмен сообщениями между самостоятельными чатами не работает |
| 7.12–7.14 | Требования определены | Не выполнялись; после 7.11/gate |
| Project ChatManager и chat rotation | Архитектура, пороги и поведение согласованы | Нет подтверждённого web-провайдера и работающей автоматической ротации |
| Project instructions/model-effort | Архитектурные правила и server registry предложены | Нет проверенного управления проектами/выбором веб-моделей/усилия |
| Общая библиотека знаний | Требования изоляции и передачи утверждены | Нет доказанной общей публикации/retrieval по grants |
| AI School/AI University | Instant/высокое рассуждение и правило **трёх подтверждённых неудач суммарно по школе/проектам** описаны | Нет запущенных учеников/исследователей, глобальной дедупликации и проверенной автоматической передачи |

**Строгая следующая работа:** **WEB-09** — контроль заполнения контекста по настраиваемым эвристикам около 60%/75%/90% с безопасной деградацией при неизвестном числе токенов; только по новой команде владельца. WEB-10…16 — ротация, память, task integration и общий offline gate после WEB-09. Затем отдельно проверить поддерживаемый и допустимый real ChatGPT access, с отдельным решением владельца до live-автоматизации. Только `TECH_GO + POLICY_GO` разблокирует зависимые будущие возможности. Не переключаться незаметно на Codex/Work/SIWC/paid API, не обещать безлимит Instant. Непроверенная попытка не равна рабочему подключению.

## 3. Что зафиксировано о реальном Netcup

- Из последнего подтверждённого [HANDOFF](../HANDOFF.md): production controller **`v0.3.11a0`**, release/rollback **`0e04af007375d82daf8f7d3b36c71abb0c784e0b`**, рабочая task SQLite **`v14`**.
- Изолированные owner E2E [#44807319708](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44807319708) OFFLINE PASS и [#44808394097](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44808394097) **SIWC GPT + Claude LIVE PASS** — *не* deployment новой SQLite и *не* проверка обычного Web ChatGPT.
- Запись текущей даты — **документальная**, состояние работающего Netcup напрямую в этом действии **не перепроверялось**. Никогда не интерпретировать этот снимок как новый live health-check.

## 4. Обязательные правила обновления после каждого подпункта

1. Сначала сверить исходник `main`, текущую живую версию (если имеет отношение), `HANDOFF` и область поручения владельца.
2. По каждому действию отделить **решение/архитектуру**, **код без тестов**, **OFFLINE PASS**, **isolated LIVE PASS**, **production deployment** и **принятый подпункт `[x]`**. Не повышать статус без соответствующего доказательства.
3. Записать в roadmap: точный ID подпункта, что сделано и что осталось, фактические тесты/артефакты/CI, known issues и риск, `GO/HOLD/NO-GO`. При подтверждённой повторяемой ошибке — также [KNOWN_ISSUES](./KNOWN_ISSUES.md) и регрессионную защиту.
4. В начале `HANDOFF.md` оставить **одну актуальную короткую записку** с текущим пунктом, последним commit/source, live version, release rollback ID, проверками и следующим действием. Исторические записи сохранять там или в Git history, но не считать более свежими текущей записки.
5. Перед выкладкой проверить HEAD и live release ещё раз; не затирать чужой source/deploy, не менять production без поручения. После изменения перепроверить опубликованный Git SHA и настоящие результаты.
6. Этот реестр — **снимок**: после следующих выполненных работ обновить и его, чтобы сумма статусов, roadmap и HANDOFF не расходились. Если есть противоречие — `HOLD` до проверки первичных доказательств.

## 5. Ссылки для передачи задачи

- [Единый roadmap](../IMPLEMENTATION_ROADMAP.md)
- [Текущий HANDOFF](../HANDOFF.md)
- [ChatGPT Web First Gate](../infra/orchestrator/CHATGPT_WEB_FIRST_GATE.md)
- [Реестр повторяющихся дефектов](./KNOWN_ISSUES.md)

**С этого статуса не следует право запускать live ChatGPT web-автоматизацию, переносить секреты или обновлять production.**
