# WEB-08 — проектные инструкции и проверенный bootstrap (offline mock)

**21.11.2026. Исходный пакет `0.3.33a0`.** Восьмой из 20 внутренних пунктов 7.11. Код: `ai_agent_engine/chatgpt_web_instructions.py`, безопасная загрузка в `chatgpt_web_models.py` и `chatgpt_web_sessions.py`, тесты `tests/test_chatgpt_web_web08.py`.

## Проверяемый контракт

1. `TrustedAgentInstructionPolicy` задаётся доверенным владельцем: точные owner/project/agent/ChatGPT Project, роль, **положительный номер активной ревизии**, SHA-256 текста инструкций и требуемые action names. Последние описывают ожидаемые разрешения, **не создают их**. Регистрация требует уже существующую WEB-06 модель, совпадение идентичности/роли и деклараций и запрещает смешение со старым inline `WebAgentIdentity.instructions`.
2. `WebInstructionBootstrap` получает текст **только через внедрённый доверенный scoped mock** `ScopedInstructionSource.read_current`. Snapshot обязан подтвердить все шесть полей scope/profile/mailbox, роль, `verified is True`, `current is True`, точную ревизию и SHA-256 UTF-8 содержимого. Пустой/слишком длинный/NUL текст, stale/newer revision, неподтверждённая или чужая запись — отказ. Никакого fallback к системной/чужой инструкции или к тексту запроса пользователя.
3. Независимый `InstructionGrantVerifier.verify` на **каждом** turn возвращает `InstructionGrantEvidence`: точные scope/profile/mailbox, роль, ревизия, tuple требуемых action names и `verified is True`. Текст инструкции не может разрешить собственный grant. Это лишь mock-подтверждение: живой `TaskPermissionGate` подключается в WEB-12, а `declared_actions` по-прежнему не права.
4. В конфигурации `WebAgentModelRegistry(..., instruction_bootstrap=...)` отсутствующая инструкция или grant **всегда блокирует `run`**. В той же per-agent FIFO порядок строго: свежая WEB-04 membership → WEB-06 model proof → WEB-07 reasoning proof → WEB-08 source+grant proof → новая `AgentSession` с этим проверенным текстом → `BrowserPort.complete`. При ошибке — sanitized отказ до отправки, очередь освобождается, нет скрытого повтора.
5. Без включённого WEB-08 прежние API остаются для исторических офлайн-тестов и **не считаются готовым live-каналом**. Обновление ревизии в этом immutable in-memory реестре требует создания нового доверенного registry по будущему owner-approved lifecycle; live persistence/restart и фактическое чтение `ProjectMemoryStore` здесь не реализованы. Интеграция долговечной памяти, TaskInbox и TaskPermissionGate — WEB-11/12/15.

## Тесты и ограничения

23 новых unit проверяют pinned policy, корректный system bootstrap, каждый scope field, текущую/старую/новую ревизию, digest mismatch, отзыв membership, разные проекты, роль, требуемые действия, malformed/False/не bool grant и snapshot, обезличенные ошибки, FIFO при одновременных задачах, восстановление очереди после отказа, повторную проверку на каждом turn и сохранение старых тестовых контрактов.

```bash
python -m unittest discover -s tests -p 'test_chatgpt_web_web08.py' -v
python -m unittest discover -s tests -q
python -m compileall -q ai_agent_engine
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/ai-agent-engine-web08-wheel .
git diff --check
```

**Подтверждённый результат:** 23/23 WEB-08 unit PASS; полный локальный regression **1196 OK (3 skipped)**, `compileall`, diff check и wheel `ai_agent_engine-0.3.33a0-py3-none-any.whl` PASS (instruction module в wheel). GitHub source [commit `54d4333`](https://github.com/vl-ktk/ai-agent-engine/commit/54d4333df853b4291ab4f8121e8f20e98b982530): 10/10 точных blob SHA и Git tree локального исходника совпали, `main` подтверждён. [CI #44895009301](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44895009301): Ubuntu 3.11/3.12/3.13 + wheel **PASS**, общий Windows **FAIL** по прежнему CI-WIN-001 (4 failures, 44 errors), но **23/23 новых WEB-08 Windows unit PASS**. Общая матрица не зелёная. **База source для адресного rollback:** `84501d99996039f6fab6015280c9e0290e6e04c7`. Netcup/рабочую SQLite, реальные ChatGPT Projects, подписку, секреты, платные API и production не меняли. `POLICY_HOLD` до WEB-17 остаётся.

**Следующий только WEB-09** — эвристики заполнения контекста, по новой команде Владислава.
