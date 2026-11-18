# Этап 6.3 — личный Claude Pro task adapter: LIVE PASS

**Дата проверки: 18.11.2026.** Источник: GitHub
[`vl-ktk/ai-agent-engine`, `main`](https://github.com/vl-ktk/ai-agent-engine).
Кодовая версия: [`e46b751c3090408d30fa3abce5f1c3958660f8e2`](https://github.com/vl-ktk/ai-agent-engine/commit/e46b751c3090408d30fa3abce5f1c3958660f8e2),
дерево `0e21194fad644a6337f085f4d2f0079023c08dc5`, версия до реализации:
`24285bee11009bd780781d3ceedd88ccf69d5757`.

## Что реализовано

- `ClaudeTaskExecutor` (`ai_agent_engine/task_claude.py`) использует проверенную
  цепочку `TaskInbox → TaskDeliveryManager → TaskPermissionGate →
  TaskProcessController → task worker → durable ACK/TaskState`. Только явный
  запуск для одного `authorized_owner_id`, совпадающего с доверенным
  `OwnerScope`; нет демона, scheduler или общего hosted API.
- `claude_task_worker.py` в отдельном процессе до отправки задачи сверяет
  `claude auth status`: уже имеющийся **личный** `claude.ai` login,
  `loggedIn=True`, `subscriptionType=pro/max`, `apiProvider=firstParty/none`.
  Затем запускает **немодифицированный официальный** `claude -p` с запросом
  через stdin, JSON-ответом, `--no-session-persistence`, `--max-turns 1`,
  пустым списком tools, строгим MCP config, `--permission-mode plan`,
  отключённым Chrome и ограниченными объёмом/временем.
- Адаптер удаляет `ANTHROPIC_*`, `CLAUDE_CODE_*` и связанные переменные из
  дочернего окружения, чтобы не унаследовать API-key и сторонние маршруты.
  **Нет** собственного OAuth-обмена, токенов в Git/БД/логах, API fallback,
  передачи подписки другим пользователям, консольных платных ключей или
  автоматически выдаваемых file/shell/network grants. Провайдерский журнал
  хранит категорию результата, а не текст задачи, ответ или секреты.
- `task_claude_acceptance.py` — отдельная **опциональная**, не CI, проверка
  одного задания в одноразовой папке с SQLite. Её нельзя путать с запуском
  постоянно работающего сервера.

## Реально наблюдавшаяся проверка

В уже существовавшем Codespace **`laughing disco`** взята опубликованная
версия `e46b751` (`detached HEAD`). Безопасный вывод проверки авторизации
не содержал токенов:

```text
exit=0 loggedIn=True method=claude.ai plan=pro provider=firstParty
```

Команда:

```bash
python3 -m ai_agent_engine.task_claude_acceptance
```

Завершилась успешно с сообщением:

```text
CLAUDE_TASK_6_3_LIVE PASS: one personal Claude Pro task, permission, result, provider audit and SQLite restart
```

Внутри acceptance проверены одна задача в owner/project scope, контрольный
маркер `AE-63-OK` в ответе, разрешённый `EXTERNAL_REQUEST` grant, одно
`completed`-событие с `provider=claude`, завершение delivery, `succeeded`
после повторного открытия тестовой SQLite. Текст запроса/ответа и
авторизационные данные не публиковались. После запуска GitHub показал
**`Codespace "laughing disco" stopped.`** — оставлять платный Codespace
работать ради отчёта не требовалось.

## Проверки кода перед публикацией

- `python -m unittest discover -s tests -q` — **513 OK, 1 skipped**.
- `python -m compileall -q ai_agent_engine tests` — PASS.
- `python scripts/check_workflow_boundaries.py` — PASS.
- `python scripts/check_public_source.py` — PASS.
- Проверка staged diff — PASS; опубликованное Git-дерево совпало с локальным.

Не проводились: реальная ошибка квоты `429`, проверка отдельного биллинга,
содержимого личного Extra Usage, долгоживущий worker, Netcup production,
передача файловых артефактов (6.12) или юридическое подтверждение Anthropic
для своего unattended сервиса. Нельзя выводить из одного PASS эти гарантии.

## Режим доступа и оставшийся HOLD

Официальные первичные источники, сверенные 18.11.2026:

- [Claude Code Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance):
  индивидуальный вход в неизменённый Claude Code разрешён; для
  продукта/hosted agent infrastructure отдельные коммерческие условия;
  нельзя посредничать OAuth-токенам других пользователей.
- [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan):
  подписочный вход для `claude -p` сейчас учитывается в лимитах подписки.
- [Use Claude Code with your Pro, Max, or Ultra plan](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-max-or-ultra-plan):
  `ANTHROPIC_API_KEY` может переключить CLI на отдельное API-тарифицирование.

Это ограниченная техническая реализация **личного** сценария, а не
юридическое заключение или универсальная лицензия на собственный серверный
продукт. **HOLD сохраняется** для unattended/24×7 Claude Pro worker на
Netcup и многопользовательского engine, до подтверждения правомерного режима
или отдельного согласования API/условий/бюджета с владельцем. Дополнительные
списания **не проверены**; новый платный API сознательно не включён.

**Следующий шаг по дорожной карте:** `6.4 [ ]` — контракт запроса, контекста,
ролей и прав. Начать только по новой команде владельца.

**Откат:** заново сверить текущий `main`, адресно отменить кодовые изменения
коммита `e46b751` относительно `24285bee` и отдельно поправить документацию,
без force push; на Netcup откатывать нечего.
