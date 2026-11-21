# WEB-13 — durable at-most-once mock Web delivery (21.11.2026)

**Статус: OFFLINE MOCK PASS для Linux/SQLite, не live ChatGPT.** Код: `ai_agent_engine/chatgpt_web_delivery.py`, `WebCoreTaskAdapter.verify_binding` и подтверждающий `response_verified` callback в `chatgpt_web_core.py`. Новая точка входа — `WebIdempotentDispatcher.dispatch(...)` поверх **существующего** `TaskDeliveryManager`, `TaskInbox`, WEB-12 permission/pin/capacity и WEB-08 instructions. Старый standalone `WebCoreTaskAdapter.execute` остаётся one-shot и **сам** не выдаёт durable delivery ticket; WEB-13 гарантии действуют только при вызове через dispatcher.

## Границы и порядок

1. До резервирования `TaskDeliveryManager`: проверка owner/project/agent, неизменённого `TaskExecutionRequest v1`, одобренного specialist pin (`chatgpt_web`), project membership и модели. Ошибка → отказ без браузера/токена.
2. Долговечная выдача attempt token в **имеющейся SQLite TaskInbox v17**, максимум 1–5 попыток (`max_attempts=3` по умолчанию). До BrowserPort запись в отдельном приватном `SQLiteWebDeliveryJournal` schema 1: стабильный scoped `correlation_id`, `task_id`, номер попытки, SHA-256 запроса и токена, deadline lease. Два файла не образуют общей SQLite-транзакции: любой разрыв между записями → **HOLD**, не дополнительный send.
3. WEB-12 `SharedCapacity` (тот же GPT pool, не отдельный безлимитный ChatGPT) отклоняет запрос без браузера, когда нет слота/provider budget. Только такой *доказанный pre-send отказ* допускает `TaskDeliveryManager.report(success=False)`, journal `DEFERRED` и ограниченную следующую попытку; после лимита — `EXHAUSTED`. Работает существующий bounded `TaskInbox`, отдельная неограниченная очередь/ретрай-демон не создаётся. Точные лимиты обычной ChatGPT подписки не заявляются.
4. Если слот получен, уже `ACTIVE` receipt запрещает второй send даже при дубле, конкуренции, истекшем lease или рестарте. `WebCoreTaskAdapter` проверяет `TaskExecutionResponse v1`, пишет в журнал `VERIFIED` с SHA-256 ответа **до** перевода задачи в `SUCCEEDED`. Затем `TaskDeliveryManager.report(success=True)` записывает durable delivery ACK, journal → `ACKED`. Ответ хранится только в результате вызова, не в WEB-13 журнале.
5. Неизвестный исход браузера, ошибка permissions после admission, сбой диска/ACK, авария между успешным mock Web и завершением/ACK — **HOLD на reconciliation**. Автоматически ни ACK, ни повторная отправка не подделываются. Если `SUCCEEDED`, но delivery `active`, следующий `reserve` честно отклонён. Возможны зависшие `RUNNING`/reservation; recovery/политика разрешения неопределённого исхода — WEB-14/15, пока не реализованы.

## Журнал

Файл sidecar только в директории владельца `0700`; файл `0600`, regular/owner/non-symlink, единственный `flock` writer, `PRAGMA synchronous=FULL`, identity `AWDL`, schema version 1, integrity check. Поля: `owner`, `project`, `agent`, `task`, `correlation`, `request_sha`, `token_sha`, `attempt`, `state`, `lease_until`, `retry_at`, `response_sha`, timestamps. **Нет промптов, ответов, инструкций, grant content, паролей, raw attempt token и транскриптов.** Использование отдельного файла не мигрирует TaskInbox v17 и не касается рабочей Netcup SQLite v14.

Состояния:

| Начало | Событие | Следующее | Можно отправлять BrowserPort? |
| --- | --- | --- | --- |
| нет / `DEFERRED` | durable `reserve` + journal `begin` | `ACTIVE` | один раз только текущему ticket |
| `ACTIVE` | capacity denied до send, retry budget есть | `DEFERRED` | только новая ограниченная попытка |
| `ACTIVE` | capacity denied, budget исчерпан | `EXHAUSTED` | нет |
| `ACTIVE` | strict response verified | `VERIFIED` | нет |
| `VERIFIED` | TaskDeliveryManager ACK success | `ACKED` | нет |
| `ACTIVE` или `VERIFIED` | timeout/crash/duplicate/lease expired | остаётся HOLD | **нет** |

## Проверки и честные пределы

`tests/test_chatgpt_web_web13.py`: **15 Linux unit** на at-most-once, owner/request/model fence, отсутствие исходных строк в журнале, тройной pre-send capacity retry, конкурирующие вызовы, настоящая авария дочернего процесса `os._exit(37)` после входа в mock BrowserPort, повторное открытие SQLite и блокировку второго эффекта, lease expiry, сбой между `SUCCEEDED` и ACK, journal permissions/schema/single-writer. Тесты Linux-only, на Windows пропускаются из-за прежнего Linux `TaskInbox`/`fcntl`.

Это **не** реальный браузерный драйвер и не exactly-once end-to-end: внешняя система не подтверждает наш idempotency key. Нет переключения аккаунта/модели, обхода CAPTCHA, оплаты, открытия реального Web UI, фонового runner, live evidence, `POLICY_GO`, `TECH_GO` или production выкладки. WEB-14 — классификация ошибок/безопасный HOLD, WEB-15 — согласованное восстановление/очередь после рестарта, WEB-16 — интегральная офлайн-приёмка; далее отдельная проверка допустимости реального доступа.
