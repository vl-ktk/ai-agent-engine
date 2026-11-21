# WEB-02 — сменяемый ChatGPT Web provider (только offline mock)

**Дата:** 21.11.2026. **Область:** один подпункт `WEB-02` внутри 7.11. Исходник `ai_agent_engine/chatgpt_web.py`, тесты `tests/test_chatgpt_web_web02.py`. Версия Python-пакета — `0.3.27a0` (**не** production Netcup).

## Реализовано

- `ChatGPTWebConfig(enabled=False, models=())`: конфигурация по умолчанию **отключена**; model IDs вводятся явно; нет автоматического выбора тарифа или модели.
- `chatgpt_web_provider_spec(config, browser_factory=...)`: подключение через существующие `ProviderSpec` / `ModelRouter` / `AgentSession`, `provider=chatgpt_web`, `supports_tools=False`.
- `BrowserPort.complete(model_id, messages) -> BrowserTurn`: доверенная точка инъекции. **Реального браузерного драйвера, HTTP/CDP, API, login и чтения секретов в пакете нет.** Только in-process `FakeBrowser` в тестах.
- При `enabled=False` — отказ **до вызова фабрики браузера**; при отсутствии/неправильном driver — тоже отказ. Нет автоматического повторного запроса, silent fallback модели, исполнения model-supplied tools или скрытых side effects.
- Неизвестный ID, неподтверждённый driver-reported ID, пустой/слишком длинный ответ, неверная история, ошибки драйвера — fail-closed с очищенным сообщением. `gpt_siwc`, действующий `GPTTaskExecutor`, `TaskInbox` и `TaskExecutionResponse` **не изменены**.

## Пример запуска только с фиктивным браузером

```python
from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, ModelRouter,
    SessionContext, chatgpt_web_provider_spec,
)

class FakeBrowser:
    def complete(self, *, model_id, messages):
        assert model_id == "mock-model"
        assert messages[-1].role == "user"
        return BrowserTurn(model_id="mock-model", text="SYNTHETIC ANSWER")

config = ChatGPTWebConfig(enabled=True, models=("mock-model",))
router = ModelRouter((chatgpt_web_provider_spec(
    config, browser_factory=lambda model_id: FakeBrowser()),))
session = router.new_session(
    provider="chatgpt_web", model_id="mock-model",
    context=SessionContext("test-project", "test-agent", AgentRole.EXECUTOR),
)
assert session.run("synthetic request").answer == "SYNTHETIC ANSWER"
```

В примере `enabled=True` используется **исключительно с локальным mock**. Код не содержит рабочего web-драйвера и не подключается к аккаунту. Реальная конфигурация по умолчанию — `False`.

## Проверки

Тест `tests/test_chatgpt_web_web02.py` покрывает 12 случаев: выключенный адаптер без запуска браузера; неправильные конфигурации; синтетический turn через `ModelRouter` и сохранённый соседний provider; отказ незарегистрированной модели и tool grants до создания порта; отсутствие/ошибки фабрики; некорректная история; неправильный model ID или malformed response; исключение после условной отправки без retry и без утечки текста ошибки.

```bash
python -m unittest discover -s tests -p 'test_chatgpt_web_web02.py' -v
python -m unittest discover -s tests -q
```

Результаты тестов, Git commit и rollback фиксируются в актуальном `HANDOFF.md`. Никаких live ChatGPT, API или Netcup вызовов эта проверка не требует.

## Границы и следующий пункт

- **WEB-03–05:** нет постоянного agent identity, project/chats registry, ChatGPT Project membership и ротации.
- **WEB-06–08:** нет проверки фактического UI выбора модели/effort и инструкций; `BrowserTurn.model_id` — только утверждение **тестового порта**, а не доказательство реальной модели.
- **WEB-09–11:** нет контекстных порогов, checkpoint/handoff, смены чата.
- **WEB-12–16:** нет привязки к TaskInbox/SharedCapacity/TaskPermissionGate, реального durable ACK/queue/crash recovery, полной offline E2E.
- **WEB-17–20:** реальный разрешённый канал, юридический/policy gate, подтверждение именно обычного ChatGPT subscription allowance и live E2E **не проверены**; `POLICY_HOLD`.

**Следующий подпункт — WEB-03, только по новой команде владельца.** До policy gate реальная автоматизация ChatGPT, копирование cookies/access tokens, обращения к недокументированным endpoints не разрешены. Офлайн-код не выдавать за работающую систему агентов.
