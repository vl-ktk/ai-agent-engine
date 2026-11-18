# AI Agent Engine — 6.1. Матрица доступа GPT / Claude

Проверено: **18.11.2026** по существующему исходнику проекта, отчёту
личного пилота от 16–17.11.2026 и актуальным официальным материалам
OpenAI/Anthropic. **Ни одна модель заново не вызывалась.** Ни один OAuth,
секрет, платный API, дополнительный сервер или Netcup не менялись.

## Что уже подтверждено, а что нет

- **GPT:** личный `Sign in with ChatGPT` в существующем Codespace,
  OAuth scope для plan usage `GRANTED`, реальный GPT-ответ, запись/чтение
  временного файла. Код `ai_agent_engine/siwc.py`, `inference.py`,
  `session_refresh.py` уже существует; это не новое подключение.
- **Claude:** официальный немодифицированный Claude Code с личным Pro,
  один реальный `claude -p` текстовый ответ в Codespace.
- **Общая приёмка пилота:** **12/12 PASS по сохранённому отчёту владельца**
  от 17.11.2026, а **не** новая независимая live-проверка в 6.1.
- **Не доказано:** GPT и Claude как исполняемые адаптеры новой
  `TaskInbox → TaskDeliveryManager → TaskProcessController → TaskPermissionGate`
  цепочки; реальный жизненный цикл авторизации/лимитов на уже существующем
  Netcup; непрерывное unattended использование личных подписок.
- `ModelRouter` и `ProviderSpec` — **инъекция доверенных адаптеров**,
  а не сами GPT/Claude провайдеры. Локальные Popen-тесты 5.14 не были
  реальными модельными запросами.

## Решение по режимам

`GO` означает только ранее проверенный ограниченный личный сценарий;
`CONDITIONAL` — официальный технический путь есть, но запуск требует
новой проверки на конкретной среде; `HOLD` — нельзя объявлять режим
подтверждённым или запускать его автоматически; `NO-GO` — запрещено
текущими согласованными ограничениями проекта, пока владелец отдельно
не изменит решение.

| Провайдер / режим | Статус 6.1 | Проверенное основание / условие |
| --- | --- | --- |
| GPT — разовый личный ChatGPT OAuth + Responses в Codespace | **GO, прежний пилот** | `GRANTED`, реальный ответ и безопасная тестовая запись, 12/12 в общем отчёте. Ограничения плана сохраняются. |
| GPT — open-source SIWC на собственной VM | **CONDITIONAL** | Официальная документация поддерживает self-hosted VM с **собственным стабильным host ID**, защищённым переносом selected registration и refresh на VM. На текущем Netcup именно этот credential/model маршрут ещё не проверяли. |
| GPT — непрерывные задания из нашей очереди на Netcup | **HOLD для эксплуатации** | Надо интегрировать реальный адаптер (6.2), проверить разрешения/scope, limits/stream errors, безопасный restart и отсутствие произвольного API fallback. Наличие VPS не равно работающему GPT runner. |
| GPT — обычный платный OpenAI API key / автоматический fallback на него | **NO-GO без отдельного решения владельца** | Подписка Plus и SIWC plan usage — не универсальный оплаченный API. Новые расходы и смена credential route не разрешены. |
| Claude — официальный личный `claude -p` в Codespace | **GO, прежний пилот** | Один настоящий ответ под личной Pro-подпиской. CLI использует лимиты личного плана; это не тест работы на Netcup. |
| Claude — интегрированный unattended runner на VPS с личным Pro OAuth | **HOLD** | Пилот не доказывает разрешение на фоновый серверный сервис. Официальный Claude Code для обычного личного использования отличается от продукта/хостинговой инфраструктуры. Для последнего могут потребоваться Commercial Terms/разъяснение Anthropic (6.3). |
| Claude — собственное приложение, проксирующее чужие подписки/токены | **NO-GO** | Anthropic запрещает сторонним разработчикам предлагать собственный Claude.ai login, собирать/проксировать чужие сессионные токены или маршрутизировать запросы за других через личную подписку. |
| Claude — Console API key / иной платный официальный провайдер | **Технически доступный режим, проектный NO-GO до согласования** | Официальный путь для сторонних продуктов, но требует отдельного разрешения владельца на оплату и соответствующие условия. Нельзя незаметно включать API key/Extra Usage. |

## Точные ограничения адаптеров, которые уже можно зафиксировать

**GPT (плановый SIWC):**

1. Только документированный `https://api.openai.com/v1/responses` с
   OAuth доступом конкретного пользователя/аккаунта, не ChatGPT
   `backend-api` и не неявная подмена API-ключом. Вызов `store=false`,
   `stream=true`, выбираем модель из видимого этому аккаунту каталога.
2. `response.completed` = завершено. `response.failed`, `incomplete`,
   обрыв потока, `401/403/429/503` = **не подтверждать результат**;
   на ошибках usage limit ставить новые задания на паузу, не списывать
   деньги через другой маршрут и не переисполнять неизвестный side effect.
3. При self-hosted VM — отдельный стабильный host ID и защищённый
   credential import; токены нельзя помещать в Git/логи/публичные ответы.
   Официальная документация предупреждает, что **host-specific usage
   attribution и отзыв plan access для перенесённых сессий пока не
   доступны** — не заявлять обратное.
4. Не переносить неподтверждённые возможности произвольного Responses
   API в SIWC route: у plan usage есть ограничения полей/инструментов,
   в частности `background` как HTTP-параметр не поддерживается.
   Наша очередь может быть асинхронной **сама**, не полагаясь на этот
   неподдерживаемый режим.

**Claude (официальный CLI):**

1. Использовать только немодифицированный официальный Claude Code
   и собственный вход владельца через Claude. Никакого своего
   Claude.ai OAuth посредника, копирования или хранения чужих токенов.
2. На 18.11.2026 Anthropic сообщает, что `claude -p` / Agent SDK при
   входе через план расходуют **лимиты подписки**; это правило биллинга,
   **не** разрешение на любое unattended использование в своём сервисе.
3. Для продуктов/сервисов и hosted agent infrastructure Anthropic
   отдельно описывает Commercial Terms; перед интеграцией фонового
   Claude Pro runner выяснить применимость режима к личному AI Agent
   Engine и отдельно проверить, что **Extra Usage** и платный API-key
   routing не включены. До этого — **HOLD**, не имитировать GO.

## Что проверять далее — без повторения старой работы

- **6.2:** в разрешённой среде связать уже работающий GPT вход с
  настоящей задачей из приватной очереди, её assignment/grants,
  результатом и журнальной фиксацией. Проверить account scopes,
  модельный маршрут, quota errors, stream completion и реальный restart;
  не расходовать новые средства. Не выдумывать, что пилотный OAuth
  уже работает на Netcup.
- **6.3:** решение о Claude unattended Pro должно опираться на
  подтверждённую допустимость режима. При её отсутствии оставить
  **HOLD/NO-GO**, не обходить через API-ключ или чужой вход. Это
  не тормозит реализацию изолированного GPT-адаптера.

## Источники

**Внутренние свидетельства:**

- [`PHASE1_GO_NOGO.md`](../../PHASE1_GO_NOGO.md) — действительные
  результаты и границы личного 12/12 пилота.
- [`CLAUDE_INTEGRATION_REVIEW.md`](../../CLAUDE_INTEGRATION_REVIEW.md) —
  проверенный один Claude CLI вызов и риски фоновой подписки.
- [`STAGE3_ENGINE_AUDIT.md`](../../STAGE3_ENGINE_AUDIT.md) — отличия
  `ModelRouter` и тестовых callback от реального AI-адаптера.
- [`STAGE5_INTEGRATION.md`](./STAGE5_INTEGRATION.md) — что именно
  проверено в новой очереди/контроллере без LLM.

**Официальные документы, просмотрены 18.11.2026:**

- OpenAI — [Sign in with ChatGPT Quickstart](https://developers.openai.com/siwc/quickstart),
  [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference),
  [Self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms),
  [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations),
  [Errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery).
- Anthropic — [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance),
  [Agent SDK / `claude -p` with subscription](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

Результат 6.1 — **матрица политики, не новый доступ и не live integration**.
Никаких новых серверов/моделей/платных API, Netcup и других проектов.
