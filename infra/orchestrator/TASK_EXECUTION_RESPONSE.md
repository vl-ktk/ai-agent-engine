# 6.5 — единый контракт ответа GPT/Claude: результат, артефакты, ошибки, лимиты

**Статус:** структурный контракт v1 и адаптеры обоих ранее подключённых
исполнителей проверены **офлайн**. Здесь нет новых обращений к провайдерам,
развёртывания на Netcup или передачи файлов из workspace в ядро.

## Источники контракта

- [JSON Schema v1](../../schemas/task_execution_response.v1.schema.json) —
  строгая Draft 2020-12 схема терминального результата.
- [Success fixture](../../tests/fixtures/task_execution_response.success.json),
  [failure fixture](../../tests/fixtures/task_execution_response.failure.json),
  [synthetic artifact fixture](../../tests/fixtures/task_execution_response.artifact.json).
- `ai_agent_engine/task_response.py` — неизменяемый `TaskExecutionResponse`,
  `ArtifactDescriptor`, `parse_task_response` для проверки JSON и
  `adapt_task_outcome` для **доверенного** `GPTTaskExecutor` или его
  owner-bound наследника `ClaudeTaskExecutor`.
- `tests/test_task_response.py` — структурные и отрицательные тесты, в том
  числе **настоящие изолированные Python-подпроцессы** обоих адаптеров.

## Поля ответа v1

| Раздел | Поля | Фактическое значение |
| --- | --- | --- |
| Идентичность | `schema_version=1`, `task_id`, `provider=gpt/claude`, `model_id` | Только зарегистрированный исполнитель и подтверждённая scoped-задача |
| `state` | `succeeded`, `failed`, `cancelled`, `timed_out` | Только терминальное состояние из доверенной SQLite; `running` — ещё не ответ |
| `result` | `text`, `artifacts` | При успехе объект с текстом и/или проверяемым по форме списком описателей; при ошибке `null` |
| `error` | `null` или `{ "code": "..." }` | Только безопасный allowlist из приватного provider audit, без сырых API-ответов |
| `limits` | `timeout_ms`, `max_text_chars=20000`, `text_chars`, `token_usage=null` | Настроенный таймаут и фактическая длина текста; токены/квота/счёт **не измерены** |

Допустимые коды ошибки: `quota`, `auth`, `unsupported`, `unavailable`,
`worker_failure`, `invalid_result` для `failed`; `timed_out` для таймаута;
`cancelled` для отмены. Структура и связность полей строго проверяются:
успех не может содержать ошибку, неуспех — фальшивый результат; не может
быть `text_chars`, не совпадающего с реальной длиной текста, или
неподтверждённого `token_usage`. Нет произвольного `message`/stack trace.

### Артефакты — только манифест, без выдачи файла

Описание артефакта: `path` (относительный POSIX путь из допустимых
сегментов, без traversal/абсолютных путей), `sha256` (64 lowercase hex),
`size_bytes` (0–100 млн), `media_type` (строгое `type/subtype`). Не более
16 описателей с уникальными путями. Нельзя подменить список именами
файлов, сырыми байтами, ссылками на секреты или shell-командами.

Это **только синтаксис манифеста**. `parse_task_response` **не** проверяет
существование файла, отсутствие symlink, реальный checksum или содержимое.
Задача **6.12** отдельно реализует разрешённое чтение, SHA и доставку
артефактов. Сейчас `adapt_task_outcome` для обоих живых провайдерских
адаптеров **всегда выставляет `artifacts: []`**; произвольный путь из
текста модели артефактом не становится.

### Доверенная конвертация из уже подключённых GPT и Claude

```python
from ai_agent_engine.task_response import adapt_task_outcome

# `runner`: доверенный GPTTaskExecutor либо owner-bound ClaudeTaskExecutor;
# `outcome`: результат одного реального runner.execute(...).
response = adapt_task_outcome(runner, outcome)
private_json_bytes = response.to_bytes()
```

Конвертер сверяет `task_id`, модель, провайдера, терминальный статус в
SQLite, `outcome.executed is True` и **ровно одно** соответствующее
событие provider audit. Повторное `execute` без новой работы не
превращается в ложный второй успех. Доверенные ошибки переводятся в
безопасные коды, но ни текст задания/ответа, ни секреты в аудит не
копируются. Текст ответа остаётся только в памяти доверенного Python
вызывающего; сохранение артефактов/ответов для внешнего клиента — позже.

**Таймаут** в миллисекундах — конфигурационная верхняя граница, не
измеренная длительность. `token_usage=null` означает **нет достоверных
цифр**, а не нулевой расход токенов, не бесплатный запрос или
отсутствие Extra Usage. Состояние `quota` проверено синтетически,
а не вызванным реальным исчерпанием лимита.

## Проверки без сетевых вызовов

```bash
python3 -m unittest tests.test_task_response -v
python3 -m unittest discover -s tests -q
python3 -m compileall -q ai_agent_engine tests
python3 scripts/check_workflow_boundaries.py
python3 scripts/check_public_source.py
```

Новый критерий 6.5 — **структурные тесты адаптера**; никаких новых
запросов к Claude Pro/GPT и новых затрат он не требует. Публикация
ограничена GitHub исходниками и документацией. Рабочие Netcup/SQLite,
секреты, systemd и API-сервисы не меняются.

**Версия до пункта:** `32390ef583ef2c54dac187215b83bb59d8ca5890`.
Откат — адресным обратным коммитом от сверенного текущего `main`, без
force/reset; серверный откат не нужен. Следующий этап — **6.6 [ ]**,
защита файловых операций от выхода из разрешённой рабочей области,
только после следующего поручения владельца.
