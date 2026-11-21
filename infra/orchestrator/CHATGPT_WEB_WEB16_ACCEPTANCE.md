# WEB-16 — multi-project offline acceptance (21.11.2026)

**Режим: SOURCE/OFFLINE/MOCK.** Это не проверка настоящего сайта ChatGPT,
права автоматизации обычной подписки, реальных квот, session cookies или
применимости модели/effort в настоящем UI. Ни один сетевой ChatGPT-запрос
не выполняется. `TECH_GO`, `POLICY_GO`, `INTEGRATION_GO`, `LIVE_PASS` не выданы.

## Воспроизводимая интеграция

`tests/test_chatgpt_web_web16.py` строит **один физический TaskInbox SQLite v17**
с двумя независимо одобренными проектными командами и планами:

| Контур | Проект ядра / mock ChatGPT Project | Агент | Модель / effort | Workspace |
|---|---|---|---|---|
| A | `engine` / `gpt-engine` | `agent_a` | `thinking` / `High` | `slot-one` |
| B | `other` / `gpt-other` | `agent_b` | `instant` / `Instant` | `slot-two` |

Обе задачи создаются в одном TaskInbox с разными owner-approved
`SpecialistPin(provider=chatgpt_web)`, `TaskPermissionGate(EXTERNAL_REQUEST)`.
Общий GPT `SharedCapacity`, строго разделённые WEB-03 профили/почтовые
контексты и WEB-04 Project membership; отдельно mock `BrowserPort` на агента.
Используются реальные классы WEB-02…15 и реальные SQLite-файлы:
WEB-10 rotation journal, WEB-13 delivery journal, WEB-15 recovery queue.
Все *свидетельства* браузера/инструкций/модели/effort/context/checkpoint/
memory — **синтетические, доверенные тестовые двойники**, не чтение UI.

### Матрица проверок

1. **Два успешных пути:** WEB-12 task→WEB-08 instructions→WEB-06 model→
   WEB-07 effort→WEB-14 guarded mock Browser→WEB-13 scoped lease/response SHA/ACK
   →WEB-15 `COMPLETED`. Оба проекта имеют собственные записи и 1 mock turn.
2. **Параллельность:** агент A удерживается внутри mock BrowserPort;
   агент B завершает независимую задачу до освобождения A. Межагентного
   FIFO/BrowserPort/разрешений и побочных эффектов не разделяют.
3. **WEB-09/10/11 + доставка:** pressure 77% вызывает `ROTATE`, до ротации
   turn не отправляется; новый mock conversation регистрируется в журнале.
   WEB-11 pinned checkpoint + memory и инструкция передаются как ограниченный
   JSON в единственный WEB-12/13/15 send. Перед отправкой WEB-15 требует
   точный `WebRotationReceipt` для этого же вращаемого task; без receipt
   отказ **до** выдачи ticket/capacity/BrowserPort. Без подмены режима/модели.
4. **Cold restart rotated task:** SQLite TaskInbox/WEB13/WEB15 закрываются,
   runtime/модель/права пересоздаются, WEB-10 history восстанавливается из
   проверяемой scoped journal chain, WEB-11 policy регистрируется снова;
   только явный вызов с точным receipt разрешает первичную mock отправку/ACK.
   Это **не** восстановление реального открытого чата/авторизации пользователя.
5. **Негативные границы:** чужой project/agent/task/receipt и недопустимые
   model/effort отклоняются без сетевого эффекта и без чужого delivery ticket.
6. **Fault isolation:** CAPTCHA/HOLD A записывается до отказа, B независимо
   отправляет и ACK. После cold restart `IN_FLIGHT` A→`UNKNOWN_SEND/HOLD`,
   но ещё не отправленная очередь B остаётся `READY` и выполняется один раз.
7. **Наследуемые WEB-13/15 crash tests:** реальный `os._exit(37)` внутри
   mock BrowserPort из `test_chatgpt_web_web13.py`/`web15.py` проверяет
   отдельный случай неожиданной смерти процесса; инвентаризация после
   рестарта не даёт второй отправки с неизвестным эффектом.

`WebRecoveryCoordinator.dispatch_next(..., handoff_receipt=...)` — небольшое
устранение обнаруженного при интеграции пробела. Внутри WEB-15, если
активный проверенный mock chat был ротирован **для той же задачи**,
без точного `WebRotationReceipt` нельзя попасть в WEB-13 dispatcher.
WEB-11 всё равно повторно проверяет receipt, инструкции, checkpoint,
scope, current/verified memory перед mock BrowserPort. Ранее существовавший
путь для задач без ротации сохраняется. Нет автоматического retry после
unknown effect, нет `HOLD→PENDING`, автологина или fallback модели.

## Команды проверки и ограничения

```bash
python -m unittest discover -s tests -p 'test_chatgpt_web_web16.py' -v
python -m unittest discover -s tests -p 'test_*.py' -q
python -m compileall -q ai_agent_engine tests
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/aae-web16-wheel .
```

**Реальный результат:** 8/8 новых Linux integration tests PASS; полный
локальный regression **1316 OK (3 skipped)**, повторён на финальной
версии; `compileall`, `git diff --check` и wheel
`ai_agent_engine-0.3.41a0-py3-none-any.whl` PASS, SHA-256
`ec6cb19927fbc5242832d2686457184ce586c12c1fce2d1a0722be0b2d5c8b8c`.
[Исходный commit `6d3fcec313bb339200f2dbdf29b918eeb2b25811`](https://github.com/vl-ktk/ai-agent-engine/commit/6d3fcec313bb339200f2dbdf29b918eeb2b25811)
(tree `7c033e5eea832b9c7000f36dd86c559e3d23accc`), исходный rollback
baseline `2aac70eda003a0cee3c22e031ef40373dd40a05d`.
[CI #44910201321](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44910201321):
Ubuntu Python **3.11/3.12/3.13 PASS**, isolated wheel **PASS**;
Windows Python 3.12 общий **FAIL** на ранее известной `CI-WIN-001`
(`fcntl`/`pwd`/`grp`/POSIX-тесты: 4 failures, 44 errors, 238 skipped,
в том числе все 8 новых Linux-only WEB-16). **Общая матрица не зелёная.**

**Остаточные риски:** настоящий browser session, quota, доступ к платному
или подписочному ChatGPT, устойчивость живого UI, серверный deploy, непрерывная
синхронизация runtime с настоящими чатами, и исход внешнего эффекта между
независимыми SQLite commit не доказаны. WEB-17 требует отдельного
нормативного решения, WEB-18/19 не разрешены без будущих gates.
