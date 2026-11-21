# WEB-05 — жизненный цикл чатов в своём ChatGPT Project (offline mock)

**21.11.2026.** Пятый из двадцати подпунктов 7.11. Python source `0.3.30a0`: `ai_agent_engine/chatgpt_web_chats.py`, дополнение к `chatgpt_web_sessions.py`, `tests/test_chatgpt_web_web05.py`.

## Что проверяет offline ChatManager

- `ProjectChatManager.create_initial(...)`: единственный первый чат агента. `rotate(...)`: следующий чат, привязанный к предыдущему. `active(...)`: актуальный чат, `history(...)`: неизменяемые снимки всей цепочки.
- Создание через `ScopedProjectChatPort.create_chat(chatgpt_project_id=..., predecessor_id=...)` разрешается только **внутри** `WebAgentSessionRegistry.scoped_project_operation(...)`: проверяется точный owner/project/agent, свежий WEB-04 `ProjectMembershipEvidence`, сохраняется per-agent FIFO на время всего mock создания. Необязательного `project_id`, создания «чата в корне» или fallback нет.
- `CreatedProjectChat` — отчёт *внедрённого mock порта*. Подтверждается совпадение `chatgpt_project_id` с доверенным binding, `profile_key`/`mailbox_key`, непустой допустимый `conversation_id`; каждый ID уникален во всём менеджере. При неопределённом исходе, ошибке драйвера, подмене полей или повторе chat ID — **нет изменения active/history и автоматического повтора**.
- Для цепочки истории хранится `(owner_id, project_id, agent_id, chatgpt_project_id, conversation_id, predecessor_id, next_id)`. Только актуальный чат имеет `next_id=None`, при успешной ротации указатели обоих звеньев обновляются в одной in-memory критической секции.
- Реестр ограничивает просмотр scope: другой владелец/проект/агент не может прочитать `active/history` или создать/ротировать чат. Параллельная работа двух агентов не блокируется одним глобальным браузерным замком; операции одного агента сериализуются с его WEB-03 model turns.

**Доверительная граница:** proof и `CreatedProjectChat` в WEB-05 целиком синтетические. Это **не** настоящая UI проверка, что чат появился в ChatGPT Project, не API создания реальных чатов и не доказательство подписочных лимитов. ChatManager не соединён с реальным браузером, не хранит историю после рестарта, не переносит содержимое предыдущего чата и не управляет расходом контекста. Реальная проверка UI/принадлежности и policy gate остаются `HOLD`; перенос/память/ротация по лимиту — в WEB-09…11, долговечная task очередь — WEB-12…16.

## Тестирование и откат

17 новых offline unit: начальный чат в своём проекте; цепочка нескольких ротаций; два агента/проекта; запрет чужого scope; отзыв proof; отсутствие gate; отказ root/foreign-project/profile/mailbox; malformed/duplicate ID; двойной initial и rotate без active; чужой/reused port; sanitized failure без retry; per-agent FIFO с модельным запросом и параллельность двух агентов; неверная конфигурация.

```bash
python -m unittest discover -s tests -p 'test_chatgpt_web_web05.py' -v
python -m unittest discover -s tests -q
python -m compileall -q ai_agent_engine
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/ai-agent-engine-web05-wheel .
```

**Факты:** 17/17 специальных WEB-05 unit PASS; полный локальный регресс **1133 OK (3 skipped)**, wheel `ai_agent_engine-0.3.30a0-py3-none-any.whl`, `compileall`, diff check PASS. Исходный commit [`c2047e9b59246e47e05178c67bae3636fc8371c6`](https://github.com/vl-ktk/ai-agent-engine/commit/c2047e9b59246e47e05178c67bae3636fc8371c6), 10/10 blob SHA и точный tree SHA проверены. [CI WEB-05 #44889580956](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44889580956): Ubuntu 3.11/3.12/3.13 и wheel **PASS**, общий Windows Python 3.12 **FAIL** на прежнем [CI-WIN-001](../../docs/KNOWN_ISSUES.md) (4 failures, 44 errors), **17/17 новых WEB-05 Windows тестов PASS** по job log. Общую матрицу зелёной не считать. **Git base/source rollback до WEB-05:** `99069ed33b9894987dfad202a5e80353cccb22c2`. Production Netcup по последней записи controller `v0.3.11a0`, release/rollback ID `0e04af007375d82daf8f7d3b36c71abb0c784e0b`, SQLite v14; здесь live не проверяли и не меняли.

**Следующий только WEB-06:** явная доступная модель для агента/задачи с отказом неизвестной модели и без silent fallback. WEB-05 не подтверждает доступность моделей ChatGPT.
