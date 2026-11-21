# WEB-01 — аудит готового кода для `chatgpt_web`

**Дата:** 21.11.2026. **Статус:** WEB-01 **SOURCE REVIEW COMPLETE**; это не `OFFLINE_PASS`, `LIVE_PASS` или разрешение веб-автоматизации. **Основание:** [пункт 7.11 единого roadmap](../../IMPLEMENTATION_ROADMAP.md) и [Web-first gate](./CHATGPT_WEB_FIRST_GATE.md).

## Проверенные ревизии, лицензии и зависимости

| Референс | Пин исходника (проверенный HEAD) | Реализация | Лицензия и зависимости |
|---|---|---|---|
| [Octo-Lex/ChatGPT-Web2API](https://github.com/Octo-Lex/ChatGPT-Web2API) | [`master@497527dceabfa3f95961e23c291e618c5570f1ac`](https://github.com/Octo-Lex/ChatGPT-Web2API/tree/497527dceabfa3f95961e23c291e618c5570f1ac) | Python >=3.11, пакет `0.2.0`, Chrome DevTools Protocol (CDP), REST + MCP | [MIT, © 2025 Elephant Rock Lab](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/LICENSE); [`pyproject.toml`](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/pyproject.toml): `websockets>=12`, `aiohttp>=3.9`, `mcp>=1.26,<2`, `portalocker>=2.7`; для тестов `pytest`, `pytest-asyncio`, `ruff` |
| [cromat/chatgpt-web-session-proxy](https://github.com/cromat/chatgpt-web-session-proxy) | [`main@753d7d8c5f0fa3fe5f342839b016dfb18b781b0b`](https://github.com/cromat/chatgpt-web-session-proxy/tree/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b) | TypeScript/Node >=22, версия `0.12.2`, Fastify + Playwright через Chrome/CDP | [MIT, © 2026](https://github.com/cromat/chatgpt-web-session-proxy/blob/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b/LICENSE); [`package.json`](https://github.com/cromat/chatgpt-web-session-proxy/blob/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b/package.json): `fastify ^5.6.1`, `playwright ^1.55.0`; dev `tsx`, `typescript`, `@types/node` |

При буквальном копировании MIT-кода сохранять copyright и текст разрешения. **В этой задаче сторонний код не копировали** и новых runtime-зависимостей не добавляли. Версии выше — состояния **проверенных commit**, не обещания совместимости с будущим ChatGPT UI.

## Сравнение архитектуры и фактические ограничения

| Требование нашего движка | ChatGPT-Web2API | chatgpt-web-session-proxy | Вывод |
|---|---|---|---|
| Разговор через обычный веб-интерфейс | CDP driver, DOM и неофициальный backend клиент; REST/MCP фасады | Playwright/CDP, REST-совместимый фасад | Оба демонстрируют технические прототипы, но не официальный стабильный API |
| Многопользовательская независимость | [README, MCP SSE](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/README.md): несколько SSE-клиентов **разделяют один driver/tab** до заявленного B1 session-affine pool; есть owned tabs/процессы и locks | [`sessions.ts`](https://github.com/cromat/chatgpt-web-session-proxy/blob/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b/src/sessions.ts): session ID, отдельная страница, последовательная очередь и persisted URL/transcript | Требуются свои `agent_id`, эксклюзивное владение браузерной сессией и изоляция проекта; общий SSE из первой реализации **не принимать** |
| ChatGPT Project / чат внутри нужного проекта | [`backend_client.py`](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/src/chatgpt_web2api/backend_client.py): операции с проектами через внутренние endpoints; доказанной связки «создать новый чат строго в нужном ChatGPT Project + подтвердить членство» для нашего контракта нет | [`project-bootstrap.ts`](https://github.com/cromat/chatgpt-web-session-proxy/blob/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b/src/project-bootstrap.ts) собирает **локальный snapshot проекта Pi**, это **не ChatGPT Project** | **REPLACE**: разрабатывать scoped реестр, проверку членства и `HOLD` при неопределённости; не считать `project-bootstrap` нужной функцией |
| Выбор модели и усилия | [`cdp_driver.py::select_model`](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/src/chatgpt_web2api/cdp_driver.py) прямо допускает нефатальный провал выбора и отправку в активной модели | [пример конфигурации](https://github.com/cromat/chatgpt-web-session-proxy/blob/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b/examples/pi-models.json) не даёт доказанного runtime выбора `reasoning_effort` в ChatGPT UI | **REPLACE/fail-closed**: запрос без подтверждения `model_id` / `reasoning_effort` не отправлять |
| Продолжение, ротация, crash | CDP tab registry/heartbeat, retry/rate-limit обработчики, conversation guard и тесты; сохранение нужного нам server checkpoint не доказано | Persisted conversation URL и append-only shadow transcript; `SessionDivergedError` при несовпадении, serial queue и atomic-file write | **IDEAS ONLY**: durable `task_id`/checkpoint/links между чатами в нашей SQLite, reconcile после неопределённой отправки; shadow transcript **не** источник прав |
| Отказы / безопасность | Отдельные [тесты conversation guard](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/tests/test_conversation_guard.py) и [tab isolation](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/tests/test_tab_isolation.py), но есть автоматические повторы/действия в UI | [`browser.ts`](https://github.com/cromat/chatgpt-web-session-proxy/blob/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b/src/browser.ts) различает login/challenge/UI drift и требует ручного прохождения security check | **IDEAS ONLY**: строго классифицировать `auth`/`rate-limit`/`challenge`/`ui-drift`/`unknown-send` → `HOLD`; без скрытых ретраев |
| Tool calls и параллельный оркестратор | MCP tooling, но интерфейс MCP не означает авторизацию агентных действий и изоляцию | [`tool-protocol.ts`](https://github.com/cromat/chatgpt-web-session-proxy/blob/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b/src/tool-protocol.ts): tool calls **эмулируются промптом**, а не подтверждённым вызовом инструментов | **REPLACE**: только доверенный `TaskPermissionGate` исполняет эффекты, не текст модели |
| Поток и вложения | API/SSE фасад со своей семантикой | [README](https://github.com/cromat/chatgpt-web-session-proxy/blob/753d7d8c5f0fa3fe5f342839b016dfb18b781b0b/README.md): SSE буферизован, файлы/изображения не пересылаются | Нельзя обещать native streaming, файлы или полную совместимость OpenAI API |

### Критичный нюанс первого референса

[README](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/README.md) может создавать впечатление полностью браузерного сценария без извлечения токена. Но в проверенном [`backend_client.py::_refresh_token`](https://github.com/Octo-Lex/ChatGPT-Web2API/blob/497527dceabfa3f95961e23c291e618c5570f1ac/src/chatgpt_web2api/backend_client.py) код явно обращается к `/api/auth/session` и считывает `accessToken`, используя его для недокументированных backend endpoints. **Этот механизм не переносить** в движок, не обрабатывать/логировать/копировать credentials и не пытаться превращать сессию в неофициальный API.

## Матрица решения reuse / replace / reject

| Элемент | Решение | Применение в WEB-02…16 |
|---|---|---|
| Изолированные browser pages/tabs, owner/session key, serial queue | **REUSE DESIGN** из обоих | В своём Python `BrowserPort` с отдельной очередью и владением для каждого `agent_id`; на WEB-02 только mock |
| Сохранение ID диалога, проверка append-only, отказ при расхождении | **REUSE DESIGN** из `sessions.ts` | Своя private SQLite registry / checkpoint / predecessor→successor; транзакционность и scope, без полного копирования истории |
| Якоря запрос/ответ, обнаружение UI drift и вызовов без результата, fault fixtures | **REUSE DESIGN** из `cdp_driver.py`, `browser.ts` и тестов | Offline mock с `request_id`, `turn_id`, `delivery_ack`, `UNKNOWN` после неподтверждённой отправки |
| REST OpenAI-compatible façade, MCP SSE multi-client общий tab | **REJECT AS-IS** | Веб-адаптер — внутренний provider, без лишнего публичного HTTP endpoint; MCP отдельно и только после grants |
| Внутренний `backend-api`, extraction `accessToken`, cookies/session export | **REJECT** | Никакого доступа к скрытым токенам, неофициального backend и хранения секретов в логах |
| Prompt-based tool execution и автоматический локальный bash project snapshot | **REJECT** | Работает только существующее trusted ядро: TaskInbox/TaskPermissionGate/ScopedMemory |
| Автоматическое закрытие security challenges, retry после неясного send, fallback модели/effort | **REJECT** | Fail closed, явный `HOLD`; retries только после сверки durable state/дедупликации |
| Node/Fastify/Playwright сервис как постоянная зависимость | **REPLACE** | Основной движок Python, дополнительные процессы и зависимости сейчас не нужны |

**Выбор реализации:** свой небольшой **Python provider adapter** `chatgpt_web`, disabled-by-default, оборачивающий абстрактный `BrowserPort`. Первый драйвер — детерминированный **mock**. Использовать существующие внутренние `TaskExecutionRequest`/`TaskExecutionResponse`, `GPTTaskExecutor`/provider-neutral routing, `TaskDeliveryManager`, `TaskPermissionGate`, `SharedCapacity`. Из референсов заимствовать **идеи**, а не развернуть один из proxy как root-service или автоматически добавить его зависимости. `gpt_siwc` не менять.

## Отказные сценарии, обязательные для следующих задач

1. **Перепутанные агент/проект/чат:** несовпадающие trusted IDs, пустая или неопределённая ChatGPT Project membership → `HOLD` до отправки; не создавать чат в корне.
2. **Модель/усилие не подтверждены:** picker отсутствует, нет заявленной модели/режима, UI изменился → `HOLD`, без fallback на текущий вариант.
3. **Отправлено, но ACK не наблюдаем:** timeout, падение browser/process после submit, внезапный reload → `UNKNOWN/HOLD`; слепой автоматический повтор может выполнить задачу дважды.
4. **Rate limit/login/CAPTCHA/anti-bot:** явный тип ошибки и `HOLD`, не обходить защиту и не зацикливать повтор.
5. **Межагентная конкуренция:** запрещено shared-tab исполнение без эксклюзивного session lock, соответствующего project ownership, сохранённых conversation IDs и очереди.
6. **Crash/restart:** atomic reconcile долгоживущего task/checkpoint/conversation, очистка неполных записей без повторного внешнего эффекта и без перезаписи чужих данных.
7. **Prompt-injection и tool hallucination:** текст веб-ответа не заменяет формальный grant или проверенное выполнение инструмента.

Это **не результаты успешно выполненных fault-injection тестов**; это зафиксированные требования к WEB-13…16.

## Условия доступа: отдельный policy gate, не закрытый WEB-01

- [Условия OpenAI для пользователей, действующие на дату проверки](https://openai.com/policies/terms-of-use/) ограничивают программное извлечение ответов и обход защит. **Личная подписка или личное использование не исключают эти условия.**
- [ChatGPT Plus](https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus): API оплачивается отдельно; отдельные участвующие приложения могут по разрешению использовать определённый allowance подписки, но это **не подтверждает** автоматизацию обычного web UI или тождество его message limits.
- Ни в одном референсе не найдено доказательства, что наш **многозадачный автономный движок** может официально потреблять именно лимит **обычного ChatGPT Chat** с гарантированной маршрутизацией проектов и усилия. Конкретный разрешённый live канал, ограничения и модель оплаты — **`UNKNOWN / POLICY_HOLD` до WEB-17**.
- `WEB-01 SOURCE REVIEW COMPLETE` **не даёт** права запускать подключение к личному ChatGPT, копировать cookies/access tokens, обращаться к скрытым endpoints, обходить CAPTCHA или запускать браузерную автоматику.

## Проверки и следующий шаг

**Проведено:** read-only получение pinned веток, manifests/licences/README, ключевых файлов управления браузером/сессиями/проектами и относящихся к ним тестов в двух GitHub-репозиториях; сопоставление с `vl-ktk/ai-agent-engine/main` и существующим [GPT Task adapter](./GPT_TASK_ADAPTER.md); сверка официальных условий и Help Center. **Не проводилось:** clone, установка зависимостей, запуск чужих unit/E2E, mock-интеграция, обращение к личному ChatGPT, измерение лимитов, Netcup health, production deploy. Поэтому это только **аналитическое закрытие WEB-01**.

**Следующий ровно один подпункт по новой команде владельца:** **WEB-02**, контракт и disabled-by-default Python adapter на mock `BrowserPort`, с тестом выключенного режима. WEB-03…WEB-20 остаются `[ ]`; общий 7.11 — `[ ] PARTIAL`.
