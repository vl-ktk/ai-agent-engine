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

**Контроль выполнения подключения:** внутри [7.11 плана](../IMPLEMENTATION_ROADMAP.md) **20 внутренних WEB-01…WEB-20: 4/20 `[x]`, 16/20 `[ ]`**. **WEB-01 `[x] SOURCE REVIEW`** — [аудит двух исходников](../infra/orchestrator/CHATGPT_WEB_WEB01_REFERENCE_REVIEW.md). **WEB-02 `[x] SOURCE/OFFLINE PASS`** — [disabled-by-default Python adapter](../infra/orchestrator/CHATGPT_WEB_WEB02_ADAPTER.md), 12/12 unit PASS. **WEB-03 `[x] OFFLINE MOCK PASS`** — [scoped agent/profile/queue registry](../infra/orchestrator/CHATGPT_WEB_WEB03_SESSIONS.md), 14 unit PASS, cross-scope/FIFO/port reuse, source `0.3.28a0`; [CI WEB-03 #44884522475](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44884522475) Linux + wheel PASS, общий Windows FAIL из-за [CI-WIN-001](./KNOWN_ISSUES.md), WEB-03 Windows 14/14 PASS. **WEB-04 `[x] OFFLINE MOCK PASS`** — [доверенные project bindings и проверка принадлежности](../infra/orchestrator/CHATGPT_WEB_WEB04_PROJECT_MEMBERSHIP.md), 14 отрицательных/положительных unit PASS, полный локальный регресс **1116 OK (3 skipped)**, source `0.3.29a0`, wheel/compileall PASS. [CI WEB-04 #44885546675](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44885546675): Ubuntu 3.11/3.12/3.13 и wheel PASS, общий Windows FAIL на CI-WIN-001, все **14 новых Windows WEB-04 unit PASS**. **WEB-05…WEB-20 `[ ]`**, следующий строго **WEB-05**. 169 прямых подпунктов/86 ранее выполненных не меняются. Полный `WEB-16 OFFLINE_PASS`, `LIVE_PASS`, лимиты обычного ChatGPT, `POLICY_GO` и итоговый `INTEGRATION_GO` **не достигнуты**.

## 2. Точка остановки — текущий 7.11

| Компонент | Что реально сделано | Чего ещё нет / что держим HOLD |
|---|---|---|
| `chatgpt_web` | WEB-01 source review, WEB-02 сменяемый adapter, [WEB-03 scoped identity/mock sessions](../infra/orchestrator/CHATGPT_WEB_WEB03_SESSIONS.md) и [WEB-04 trusted project bindings + mock membership gate](../infra/orchestrator/CHATGPT_WEB_WEB04_PROJECT_MEMBERSHIP.md), source `0.3.29a0` | **НЕТ** настоящей UI-проверки ChatGPT Project membership, реальных browser profiles, durable mailbox/restart, chat rotation, model/effort UI verification, TaskInbox integration, WEB-16 full offline acceptance, live подключения и подтверждения обычного Chat allowance; `POLICY_HOLD` |
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

**Строгая следующая работа:** **WEB-05** — mock ChatManager: доверенная agent→active chat связь, создание нового чата **только внутри подтверждённого ChatGPT Project**, predecessor/next и запрет создания чата «в корне»; только по новой команде владельца. WEB-06…16 — модель/effort, durable task integration и общая offline приёмка, строго после WEB-05. Затем отдельно проверить поддерживаемый и допустимый real ChatGPT access, с отдельным решением владельца до live-автоматизации. Только `TECH_GO + POLICY_GO` разблокирует зависимые будущие возможности. Не переключаться незаметно на Codex/Work/SIWC/paid API, не обещать безлимит Instant. Непроверенная попытка не равна рабочему подключению.

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
