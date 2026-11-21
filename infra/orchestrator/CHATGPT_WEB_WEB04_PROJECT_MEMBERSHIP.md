# WEB-04 — доверенная привязка агента к ChatGPT Project (offline mock)

**21.11.2026.** Это четвёртый из двадцати внутренних пунктов 7.11. Source Python `0.3.29a0`; исходник `ai_agent_engine/chatgpt_web_projects.py` и добавленная проверка в `chatgpt_web_sessions.py`. **Нет реального ChatGPT-доступа, проверки аккаунта или UI.**

## Доверенная привязка

1. Владелец/ядро, не текст задания и не LLM, формирует `TrustedChatGPTProjectBinding(owner_id, project_id, chatgpt_project_id)`; `TrustedChatGPTProjectRegistry` принимает неизменяемый набор. Неизвестный owner/project, дубликат, переприсвоение одного внешнего Project двум внутренним проектам или неверный ID приводят к отказу.
2. `WebAgentSessionRegistry` в режиме WEB-04 создаётся с **обоими** `project_registry` и `project_verifier`. Неполная конфигурация отвергается. При регистрации агента внутренний owner/project должен уже существовать в доверенном реестре — иначе mock-порт даже не создаётся.
3. **Перед каждым** синтетическим `run` (в его per-agent FIFO очереди) доверенный `ProjectMembershipVerifier.verify(binding=..., profile=...)` должен вернуть `ProjectMembershipEvidence` с точными owner, внутренним project, agent, внешним ChatGPT Project, `profile_key`, `mailbox_key` и именно булевым `verified=True`. Любой mismatch, `False`, неверный тип, исключение или отсутствие подтверждения — `WebAgentScopeError` **до** `BrowserPort.complete`. Не сохраняем старый успешный ответ и не делаем скрытый retry.
4. `project_binding(...)` позволяет доверенному коду прочитать привязку только после проверки конкретных owner/project/agent. Устаревший режим WEB-03 без project gate по-прежнему существует **лишь для legacy offline mock**; его отсутствие gate не выдаётся за подтверждённую принадлежность (метод `project_binding` явно отказывает). Для будущего реального маршрута наличие project gate должно быть обязательным.

Это **контракт между доверенными компонентами**, а не криптографическая верификация. В WEB-04 verifier тестовый и возвращает искусственные доказательства. Текст `verified=True` сам по себе не доказывает, что пользователь состоит в настоящем ChatGPT Project: реальная проверка через допустимый канал, авторизация и защита от изменения вкладки между check/use остаются `HOLD` до последующих WEB/policy-gates. Обычный ChatGPT allowance не подтверждён.

## Отрицательные сценарии

`tests/test_chatgpt_web_web04.py` проверяет два внутренних проекта с разными `chatgpt_project_id`, неверный owner/project/agent, unknown binding до создания порта, чужой внешний ChatGPT Project, подмену каждого поля proof (включая profile/mailbox), `False`, `1`, `None`/словарь, исключение verifier с sanitization, отсутствие неполного gate, повторную проверку на каждом turn, немедленный отказ после mock-отзыва и освобождение очереди.

```bash
python -m unittest discover -s tests -p 'test_chatgpt_web_web04.py' -v
python -m unittest discover -s tests -q
python -m compileall -q ai_agent_engine
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/ai-agent-engine-web04-wheel .
```

**Фактические результаты:** 14 WEB-04 unit PASS, **1116 общих unit OK (3 skipped)**, `compileall` PASS, wheel `ai_agent_engine-0.3.29a0-py3-none-any.whl` PASS (проверено присутствие обоих web-projects/sessions модулей), `git diff --check` PASS. Код опубликован в source commit [`bcad9ace81a0f97e0aa2c7e5198692bb8b0cf31e`](https://github.com/vl-ktk/ai-agent-engine/commit/bcad9ace81a0f97e0aa2c7e5198692bb8b0cf31e): 10/10 blob SHA и точный Git tree SHA сравнены с локальными, `main` проверен до и после. [CI #44885546675](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44885546675): Linux Python 3.11/3.12/3.13 и isolated wheel **PASS**, **общий Windows FAIL** (4 failures, 44 errors) на прежнем [CI-WIN-001](../../docs/KNOWN_ISSUES.md); по Windows job log **все 14 новых WEB-04 тестов PASS**. Общая CI матрица поэтому **не зелёная**. Состояние до WEB-04: Git `ae6f027411c83c90af8569e57776abd02bc211a0` (rollback этой source-правки); Netcup production controller по последнему подтверждению `v0.3.11a0`, release/rollback `0e04af007375d82daf8f7d3b36c71abb0c784e0b`, SQLite v14 — **не перепроверялись и не изменялись**.

**Следующий только WEB-05:** отдельный mock ChatManager, agent→active conversation, создание чата строго в подтверждённом проекте, predecessor/next. WEB-04 не создаёт чаты, вкладки, долговечные mailbox и не внедряет live-автоматизацию.
