# WEB-03 — изоляция идентичности и тестовых сессий агентов

**Дата:** 21.11.2026. **Граница:** только WEB-03 внутри 7.11, исходный Python-пакет `0.3.28a0` (это не Netcup production). Код: `ai_agent_engine/chatgpt_web_sessions.py`; тесты: `tests/test_chatgpt_web_web03.py`. База перед изменением: Git `62fc0f060268cd033baf2371505d74b27946bc32` (source rollback).

## Реализовано в рамках offline mock

- `WebAgentIdentity`: доверенно заданный **стабильный** `owner_id`, `project_id`, `agent_id`, роль, инструкции и отдельные *декларативные* action labels. Один `agent_id` нельзя незаметно перепривязать к другому owner/project/role. Label не выдаёт разрешение на инструменты.
- `WebAgentProfile`: две **логические** неизменные namespace-метки на основе SHA-256 и domain separation: `profile_key` и `mailbox_key` от точной связки owner/project/agent. Они не совпадают у разных агентов и воспроизводимы при повторном создании реестра с той же доверенной идентичностью. **Это не директории Chrome, не ChatGPT Projects и не долговечная очередь сообщений.**
- `WebAgentSessionRegistry`: pin identity→exclusive `ScopedBrowserPort`→private serial queue. Mock-port обязан вернуть точные `profile_key` и `mailbox_key`, полученные при регистрации; чужую метку и повторное использование порта у другого агента отвергаем **до первого turn**. Браузерная фабрика вызывается один раз при регистрации агента. Без фабрики или при disabled web-provider — отказ до создания профиля.
- `run(owner_id, project_id, agent_id, model_id, prompt)`: сверка owner/project/agent до доступа к порту, затем отдельный `AgentSession(SessionContext(...))` на один synthetic turn с закреплённой ролью и инструкциями. Никаких caller-supplied замен инструкций или чужих данных.
- FIFO очередь (`Condition`+ticket) существует **отдельно для каждого** агента: второй запрос того же агента ждёт, другой агент может работать параллельно. Даже после ошибки mock-browser очередь освобождается. `pending()` и `profile()` проверяют scope и не позволяют читать чужие записи.
- Только неинвазивные `FakeBrowser` в тестах; ни CDP/HTTP, ни авторизации, ни cookies/tokens, ни UI, ни фактических инструментов в рамках WEB-03.

**Доверительная граница:** здесь *владелец/ядро* передаёт истинные ID; проверка совпадения ID защищает от случайного смешения, **не является самостоятельной аутентификацией**. Метаданные `declared_actions` не заменяют `TaskPermissionGate`; реальные task grants появятся в WEB-12. Разные mock-объекты порта не доказывают физическую изоляцию настоящих вкладок/профилей браузера; это отдельная live/policy-проверка.

## Проверяемые сценарии

| Проверка | Ожидаемый результат |
|---|---|
| Два агента в разных проектах | разные `profile_key`, `mailbox_key`, экземпляры `BrowserPort`, закреплённые инструкции/роли и audit `project_id/agent_id` |
| Неверный owner/project/agent | `WebAgentScopeError` до вызова порта; нельзя получить чужую identity/profile/queue |
| Повторная регистрация/port reuse/чужой profile claim | отказ без перепривязки агента или общего mock port |
| Одна очередь / два concurrent turn | второй запрос ждёт, порядок FIFO, `pending` уменьшается после выполнения/ошибки |
| Два разных concurrent агента | один заблокированный mock не блокирует ответ второго |
| Драйвер/модель/инструкции | sanitization ошибок, fail-closed при неизвестной модели, закреплённые инструкции, без доступа к tools |
| Пересоздание реестра | logical namespace метки воспроизводятся по той же стабильной идентичности, но **состояние очереди не восстанавливается** |

Команды:

```bash
python -m unittest discover -s tests -p 'test_chatgpt_web_web03.py' -v
python -m unittest discover -s tests -q
```

Фактические численные результаты, GitHub commit/CI и откат записаны в актуальном `HANDOFF.md`; сценарии выше описывают строго offline-контракт.

## Что остаётся за пределами WEB-03

- **WEB-04:** проверка принадлежности к настоящему ChatGPT Project; `project_id` пока только owner-scoped ID нашего ядра.
- **WEB-05:** создание и ротация чатов, predecessor/next и доказательство, что новый чат остаётся внутри проекта.
- **WEB-06…11:** реальный выбор модели/effort, инструкции, контекст/ротация и перенос памяти.
- **WEB-12…16:** TaskInbox/SharedCapacity/TaskPermissionGate, durable MessageBus/ACK/retry/restart/тест E2E; здешняя FIFO-очередь ограничена памятью процесса, без persistent messages, без crash recovery и backpressure policy.
- **WEB-17…20:** проверенный допустимый реальный канал к обычному ChatGPT, subscription allowance и отдельный owner policy/live gate. `POLICY_HOLD` сохраняется; ничего не запускать на личном аккаунте или Netcup.

**Следующий по плану только WEB-04**, по отдельному указанию владельца. WEB-03 не объявлять live multi-agent capability.
