# WEB-06 — выбор модели агента и задачи (только offline mock)

**21.11.2026.** Шестой из двадцати внутренних пунктов 7.11. Исходная версия `0.3.31a0`: `ai_agent_engine/chatgpt_web_models.py`, дополнение к `chatgpt_web_sessions.py`, тесты `tests/test_chatgpt_web_web06.py`.

## Контракт выбора без скрытой подмены

1. Владелец/ядро явно задаёт `TrustedProjectModelCatalog(chatgpt_project_id, model_ids)` для каждого доверенно связанного ChatGPT Project. Это **настроенный перечень**, не фактическое прочтение UI или доказательство, что модель сегодня есть в подписке.
2. `TrustedAgentModelPolicy(owner_id, project_id, agent_id, default_model_id, allowed_model_ids)` закрепляется один раз для зарегистрированного агента; доступные модели обязаны присутствовать **и** в каталоге его проекта, **и** в `ChatGPTWebConfig.models` адаптера. Неизвестный agent/project/catalog/model, дубликат, silent rebind — отказ.
3. `WebAgentModelRegistry.run(..., requested_model_id=None)` использует default-model при отсутствии явного выбора. Если задача запросила другой model ID, он обязан входить в `allowed_model_ids`. **Неизвестное/запрещённое значение отвергается до UI selector и до BrowserPort; никакого выбора ближайшей модели.** `None` означает сознательный выбор зарегистрированной модели по умолчанию, не автоматическую деградацию.
4. При выбранной модели WEB-06 вызывает `WebAgentSessionRegistry.run_with_model_verifier(...)`: после очередной WEB-04 membership verification, **внутри той же per-agent FIFO**, но до `BrowserPort.complete`, доверенный `ProjectModelSelector.select(binding, profile, model_id)` возвращает `ModelSelectionEvidence` с точными owner/project/agent, ChatGPT Project, profile/mailbox, model_id и `verified is True`. Любой mismatch/False/неправильный ответ/исключение — sanitized `WebModelSelectionError` без отправки и retry.
5. `WebAgentSessionRegistry.run()` остался для старого WEB-02/03 offline mock. Путь будущих реальных запросов должен вызывать **только** контролируемый `WebAgentModelRegistry.run`, вместе с WEB-04 gate и ещё не реализованными WEB-07/08; direct legacy API не является live route.

**Ограничение:** `ProjectModelSelector` в WEB-06 — только внедряемая mock-заглушка. Здесь нет подтверждения реально выбранной модели в UI, доступности именно Instant/Thinking, оценки лимитов обычного ChatGPT или уровня рассуждений. Настройка `reasoning_effort` и доказанный отказ при другом effort — отдельный **WEB-07**. Реальная автоматизация подписки всё ещё `POLICY_HOLD`.

## Проверки

18 offline-тестов: валидность/дубликаты настроек и неизменность catalog; default/explicit task choice; две разные Project model policies; запрет неразрешённой и неизвестной модели до mock browser; mismatch любого поля evidence и `model_id`, falsy/non-bool/malformed proof, sanitized selector error; отзыв membership, повторный proof для каждого turn, общий FIFO с запросами модели, защита от перепривязки и legacy-пути без project gate.

```bash
python -m unittest discover -s tests -p 'test_chatgpt_web_web06.py' -v
python -m unittest discover -s tests -q
python -m compileall -q ai_agent_engine
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/ai-agent-engine-web06-wheel .
```

**Результаты:** 18/18 специальных unit PASS; общий локальный регресс **1151 OK (3 skipped)**, `compileall`, diff check, wheel `ai_agent_engine-0.3.31a0-py3-none-any.whl` PASS (модуль `chatgpt_web_models.py` внутри архива проверен). Source commit [`cf34882087d95a314f27b99fff0e7f1e4826ef72`](https://github.com/vl-ktk/ai-agent-engine/commit/cf34882087d95a314f27b99fff0e7f1e4826ef72): проверены 10/10 точных blob SHA и tree SHA в `main`. [CI #44891594867](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44891594867): Ubuntu 3.11/3.12/3.13 и wheel **PASS**, общий Windows 3.12 **FAIL** (4 failures, 44 errors) по старому [CI-WIN-001](../../docs/KNOWN_ISSUES.md); **18/18 новых WEB-06 Windows unit PASS** по job log. Общая матрица **не зелёная**. **Git source baseline/rollback до WEB-06:** `44e2904dc9f5f40784298a09936063d57a1daaf9`. Последняя зафиксированная live Netcup controller version `v0.3.11a0`, release/rollback ID `0e04af007375d82daf8f7d3b36c71abb0c784e0b`, SQLite v14; в WEB-06 не проверялись и не менялись.

**Следующий строго WEB-07** (configurable reasoning effort) по новой команде. WEB-06 не реализует WEB-07…20.
