# WEB-14 — fail-closed BrowserPort fault guard (21.11.2026)

**Статус: OFFLINE MOCK PASS, нет реального ChatGPT UI.** Исходник `ai_agent_engine/chatgpt_web_failures.py`, интеграция opt-in `WebIdempotentDispatcher(..., fault_guard=...)`, тесты `tests/test_chatgpt_web_web14.py`. Не используются реальные аккаунты, cookies, hidden endpoints, платные API, CAPTCHA solver или автоматический login.

## Контракт доверенной ошибки

Инжектируемый доверенный **mock** `BrowserPort` может вернуть `BrowserTurn(model_id, text)` или выбросить `WebPortFault(WebFaultCode, retry_after_seconds=None)`. Принимаются **только** экземпляр точного класса `WebPortFault` и значение закрытого `WebFaultCode`, не текст с веб-страницы, не `HTTP status`, не посторонний subclass/переписанный объект. Неизвестные исключения и испорченный fault объект → `UNKNOWN_SEND`. Причины не извлекаются эвристикой из сообщений модели или HTML:

| Код | Пример условия в доверенном mock порте | Действие |
| --- | --- | --- |
| `AUTH_REQUIRED`, `SESSION_EXPIRED` | Нет подтверждённой авторизации / сессия истекла | `HOLD`, без тихого login/refresh |
| `RATE_LIMITED` | Порт сообщает rate limit | `HOLD`, `retry_after_seconds` только для оператора, **не** таймер запуска |
| `TIMEOUT` | Тайм-аут mock операции | `HOLD`, возможный внешний эффект неизвестен |
| `UI_DRIFT` | Потеря доказанного UI контракта | `HOLD`, без слепого нажатия |
| `CAPTCHA`, `ANTI_BOT` | Проверка безопасности | `HOLD`, без обхода/решателя |
| `WRONG_PROJECT`, `WRONG_MODEL` | Подтверждённое несовпадение области / модели | `HOLD`, без переключения/замены модели |
| `INVALID_RESPONSE` | Пустой/повреждённый/чрезмерный результат или не-`BrowserTurn` | `HOLD`, без второго send |
| `UNKNOWN_SEND` | Любое неподтверждённое исключение и невалидный код | `HOLD`, без предположений о side effect |

`WebFaultGuard` закреплён за точным `WebAgentProfile(owner/project/agent/profile_key/mailbox_key)` и только **одним** `GuardedBrowserPort`. После первого отказа он сохраняет в памяти безопасную сводку `state=HOLD`, `code`, timestamp и допустимый `retry_after_seconds`; **нет** payload/prompts/answers/credentials/raw exceptions. `GuardedBrowserPort` предоставляет только `complete`: никакого proxy-доступа к `solve_captcha`, `login_again`, `switch_model`, retry или fallback. Его отдельная блокировка сериализует даже одновременные прямые mock вызовы.

Когда этот же порт установлен в `WebAgentSessionRegistry` и передан **тот же** guard в WEB-13 dispatcher, `require_bound_port` и `require_ready` работают **после** read-only проверки WEB-12 binding, но **до** `TaskDeliveryManager.reserve`. Следовательно, уже известный `HOLD` не расходует новое delivery token/capacity и не запускает браузер. Если отказ возник **внутри** `complete`, WEB-13 уже сохранил `ACTIVE` в долговечном журнале, а task и slot остаются `RUNNING`/reserved на ручной reconciliation; повторная отправка запрещена. Никакого дополнительного вызова к GPT-подписке нет.

## Доказательства и ограничения

`tests/test_chatgpt_web_web14.py`: закрытая таблица всех кодов, invalid codes, hidden exception text, forged/subclassed faults, по одному browser call при 20 конкурентных вызовах, неправильный profile/port, wrong/empty/oversize result, проверка того, что challenge/login/model-switch methods не вызываются, pre-reserve HOLD, error inside WEB-13 task с сохранением lease/capacity, позитивный mock ACK. Все проверки через строго локальные фиктивные порты. WEB-14 код сам по себе к реальному ChatGPT не подключается.

**Честные границы:** Состояние guard **только в памяти процесса**. Активный task/delivery `ACTIVE` уже сохраняется WEB-13, но durable перезапуск с состоянием всего agent/session и ручной recovery после crash — WEB-15. Нет реального измерения provider quota/`Retry-After`, исполняемого watchdog для зависшего настоящего браузера, настоящего CAPTCHA/login или подтверждения UI/Project/model в подписке. Типизированные ошибки исходят только из mock порта, не являются live-доказательством. Это fail-stop механизм, не автоматический самоисцеляющийся UI-адаптер. `POLICY_GO`, `TECH_GO`, `LIVE_PASS` отсутствуют.
