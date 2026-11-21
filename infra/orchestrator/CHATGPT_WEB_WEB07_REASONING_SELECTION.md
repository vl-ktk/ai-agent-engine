# WEB-07 — выбор reasoning effort (только offline mock)

**21.11.2026; исходный пакет `0.3.32a0`.** Седьмой из 20 внутренних пунктов 7.11. Реализация: `ai_agent_engine/chatgpt_web_reasoning.py`, расширение `chatgpt_web_models.py`, `tests/test_chatgpt_web_web07.py`.

## Контракт

1. `TrustedProjectReasoningCatalog` задаёт точный набор разрешённых `reasoning_effort` **для каждой модели** в конкретном доверенном ChatGPT Project. `Instant`, `Medium`, `High` в тестах — только имена mock-режимов, не доказанные функции реального ChatGPT или обещание соответствующих лимитов.
2. `TrustedAgentReasoningPolicy` закрепляет разрешённые уровни и default **одного агента**. Политика регистрируется только после WEB-06 модели; default обязан поддерживаться моделью по умолчанию, а каждое разрешённое значение — хотя бы одной разрешённой моделью. Каталоги и политики не перезаписываются тихо.
3. `WebAgentModelRegistry.run(..., requested_model_id=..., requested_reasoning_effort=...)` выбирает значения задачи либо закреплённые defaults. Пара модель/уровень должна входить и в agent policy, и в конфигурацию конкретной модели/проекта. При несовместимой паре запрос отвергается **до** selector, без подмены на ближайший уровень.
4. При включённой конфигурации reasoning отсутствие reasoning policy **всегда** запрещает `run`. `None` означает только закреплённый default. При отключённом WEB-07 явный `requested_reasoning_effort` запрещён; прежний WEB-06 mock не становится live-путём.
5. На каждом turn после свежей WEB-04 membership внутри **одной per-agent FIFO** идут WEB-06 model proof, затем WEB-07 reasoning proof и только затем `BrowserPort.complete`. Mock selector подтверждает точные owner/project/agent, ChatGPT Project, profile/mailbox, model, effort и `verified is True`. Несовпадение, исключение или malformed proof → sanitized `WebReasoningSelectionError`, **нет отправки, fallback или retry**.

`ProjectReasoningSelector` — внедряемая mock-граница. Нет реального браузера, UI-проверки, live ChatGPT Project/model/effort, расхода подписки или гарантии Thinking. По-прежнему `POLICY_HOLD` до WEB-17 и отдельного решения владельца. Официальные [примечания ChatGPT](https://help.openai.com/en/articles/6825453-chatgpt-release-notes) показывают, что названия и доступность уровней могут меняться; настройки этого кода — не автоматическое обнаружение.

## Проверки

22 новых offline unit: схема/политики и каталоги; default/task override; запрет несовместимых model+effort; два проекта и чужой scope; дублирование/перепривязка; отсутствие policy; подмена каждого поля proof, false/non-bool/malformed, sanitized exceptions; отзыв membership; ошибка model proof до effort; свежий proof на каждом turn; одна FIFO и запрет отправки до подтверждения.

```bash
python -m unittest discover -s tests -p 'test_chatgpt_web_web07.py' -v
python -m unittest discover -s tests -q
python -m compileall -q ai_agent_engine
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/ai-agent-engine-web07-wheel .
git diff --check
```

**Первичный результат:** 22/22 WEB-07 unit PASS, `compileall` и wheel `ai_agent_engine-0.3.32a0-py3-none-any.whl` PASS, reasoning-модуль внутри wheel. Полный регресс и CI фиксируются отдельно после завершения; прежняя общая Windows ошибка `CI-WIN-001` не исправлялась. **Исходная база перед WEB-07:** `246bfe80191c891632cb0af0b21807b597ec7f68`; откат только адресным revert собственного коммита при более поздних чужих изменениях. Netcup/рабочая SQLite/секреты/paid API не затронуты.

**Следующий только WEB-08** — проектные инструкции и revision bootstrap, по новой команде владельца.
