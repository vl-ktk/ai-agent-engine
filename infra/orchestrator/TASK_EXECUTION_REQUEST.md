# 6.4 — единый контракт запроса GPT/Claude: схема, валидатор, fixture

**Статус:** реализован **локальный provider-neutral контракт v1** для
доверенного координатора; он **не** означает повторного live-вызова GPT/Claude,
развёртывания Netcup или передачи новых полномочий моделям.

## Артефакты

- [JSON Schema v1](../../schemas/task_execution_request.v1.schema.json)
  (Draft 2020-12, `additionalProperties=false`, явная версия).
- [Корректный JSON fixture](../../tests/fixtures/task_execution_request.valid.json),
  полностью синтетический: не содержит настоящих задач, ключей и токенов.
- `ai_agent_engine/task_request.py` — `parse_task_request` (структурный
  строгий валидатор), `build_task_request` (из реальной доверенной очереди
  и назначения), `verify_bound_task_request` (сверка всех полей с реальными
  owner/project/task/worker/role/policy данными); неизменяемый
  `TaskExecutionRequest` и каноническая JSON-сериализация.
- `tests/test_task_request.py` — локальные тесты валидности fixture,
  отсутствия неизвестных/повторяющихся полей, лимитов, UTF-8, подмены
  чужого owner/project, роли, задания, каталога и разрешений.

## Контракт v1

| Раздел | Обязательные поля | Источник правды |
| --- | --- | --- |
| `schema_version` | `1` | Фиксированная версия формата |
| `task` | `task_id`, `project_id`, `title`, `prompt`, `parent_task_id` (`null` для корневой), `priority` | Уже принятый `TaskRecord` в `TaskInbox`, не заново поданный prompt |
| `context` | `owner_id`, `workspace_name`, `summary` | `OwnerScope`, доверенное неизменяемое назначение и текстовый контекст координатора |
| `role` | `worker_id`, `kind` (`executor`/`supervisor`), `instructions` | Уже назначенный `TaskAssignment.worker_id`, доверенная роль `AgentRole` и инструкции координатора |
| `permissions` | `required_actions` — **отсортированный список нужных действий**, допускаются только 5 имён `ProtectedAction` | Доверенное описание потребности; **не grant** и не разрешение на выполнение |

Нельзя добавлять `api_key`, OAuth, модель, инструменты, произвольный путь
к рабочей папке, роль `admin` или собственные поля. Идентификаторы и
лимиты синхронизированы с уже существующими `TaskSubmission`,
`OwnerScope`, `TaskAssignment`, `AgentRole`, `ProtectedAction`. Корневой
`task_...` требует `parent_task_id=null`, а `subtask_...` — валидного
родительского ID. Поля со строками проверяются на UTF-8, ограничены
по длине; весь JSON не более **65 536 байт**. Неизвестные и повторные
ключи, неизвестные действия, несовпадающие роли, пустой prompt, Boolean
вместо числа и нестандартные JSON-значения отклоняются.

**Важная граница безопасности:** `parse_task_request` проверяет только
формат; сам JSON не является правом исполнения. Для работы координатор
должен использовать `verify_bound_task_request` с настоящими `TaskInbox`,
`OwnerScope` и ожидаемыми доверенными ролью/контекстом/действиями.
Даже успешная проверка **ничего не разрешает**: перед сетевым запросом,
записью, shell и т.д. `TaskPermissionGate` заново проверяет именно
`ActionGrant`, назначенного worker, текущий статус и TTL, фиксируя решение
в SQLite **до** побочного эффекта. Роль `supervisor` не выдаёт гранты.

## Пример для доверенного Python-кода

```python
from ai_agent_engine.core import AgentRole
from ai_agent_engine.task_permissions import ProtectedAction
from ai_agent_engine.task_request import build_task_request, verify_bound_task_request

# `inbox`, `scope`, `task_id` получены из доверенной очереди/контроллера;
# задача уже закреплена за существующим приватным workspace.
request = build_task_request(
    inbox, scope, task_id, role=AgentRole.EXECUTOR,
    context_summary="Контекст владельца", role_instructions="Работай по задаче",
    required_actions=(ProtectedAction.EXTERNAL_REQUEST,),
)
verified = verify_bound_task_request(
    request.to_bytes(), inbox, scope, task_id, role=AgentRole.EXECUTOR,
    context_summary="Контекст владельца", role_instructions="Работай по задаче",
    required_actions=(ProtectedAction.EXTERNAL_REQUEST,),
)
```

Здесь нет вызова модели, получения grant или изменения системы. Действующий
`POST /v1/tasks` этапа 5.1 остаётся прежним: клиент **не может** отправить
через него `owner_id`, `role` или `required_actions`. GPT 6.2 и Claude 6.3
пока используют свои узкие одноразовые payload `model_id/prompt`;
унифицированная схема подготовлена **внутри координатора** и не
подменяет их live-проверки. Этап **6.5** отдельно определит ответ, ошибку,
артефакты и лимиты исполнения; другие пункты 6.x — sandbox и lifecycle.

## Воспроизведение без сетевых запросов

```bash
python3 -m unittest tests.test_task_request -v
python3 -m unittest discover -s tests -q
python3 -m compileall -q ai_agent_engine tests
python3 scripts/check_workflow_boundaries.py
python3 scripts/check_public_source.py
```

**Откат:** если понадобится, от текущего GitHub `main` сделать адресный
обратный коммит 6.4 после повторной сверки головы. Базовая версия до
пункта — `1c06f77307537aea0c120f272903f3895956d2f4`; Netcup/production
не менялись, серверного отката нет.
