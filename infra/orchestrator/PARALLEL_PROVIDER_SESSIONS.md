# 6.10 — параллельные сессии GPT/Claude task executors

## Критерий и реализованная граница

Две разные owner-bound задачи должны **одновременно** удерживать разные рабочие каталоги и оставаться в `RUNNING`, пока реально работают два отдельных Linux-процесса. После независимого завершения обе задачи имеют отдельные устойчивые `SUCCEEDED`, provider audit, permission audit, delivery ACK и записи о `stopped` в task SQLite v8.

Реализация 5.9/5.10 и 6.2/6.3/6.9 уже поддерживает эту параллельность: `TaskInbox._lock` охватывает только короткие транзакции метаданных, `WorkspaceRoot.acquire` ограничивает ровно один каталог, а `TaskProcessController` контролирует отдельные process groups. Новая правка 6.10 не меняет API, схему БД или production runtime: добавлен **реальный** интеграционный тест этих инвариантов.

## Проверка

`python3 -m unittest tests.test_task_parallel_sessions -v`

Тест запускает `GPTTaskExecutor` и `ClaudeTaskExecutor` как разные потоки над одной доверенной SQLite, но каждый реально запускает свой private gated `subprocess.Popen` (`python -u -c` с полностью фиксированным кодом без сети и paid API). Один worker работает в `project-a/gpt-slot`, другой в `project-b/claude-slot`. Каждый записывает `started` и ждёт независимого файла `release`. Пока оба ждут, тест проверяет:

- оба task states одновременно `RUNNING`, оба Future ещё выполняются;
- два разных живых PID и две `active` process-ledger rows SQLite v8;
- обе приватные workspace заблокированы независимо, нет глобальной блокировки;
- у каждого отдельный grant на `EXTERNAL_REQUEST`, приватная выдача запроса, один provider event/ACK;
- оба завершаются в `SUCCEEDED` с разными ответами, ledger `stopped`, второй запуск не происходит;
- новый `TaskInbox` после открытия той же SQLite видит оба финальных состояния.

## CI и известные ограничения

PR #54 `stage-6.10-parallel-sessions`. GitHub Actions `CI (offline)`: Ubuntu Python 3.11 PASS, Python 3.13 PASS, Python 3.12 PASS на повторном прогоне (**588 unittest OK**), wheel build PASS. Начальный 3.12 прогон упал в старом тесте 5.14: короткий subprocess уже завершился до `TaskProcessController.attach`; повтор без изменения этого теста PASS. Windows/Python 3.12 FAIL по причине Linux-only `fcntl` imports в существующем `task_assignment.py`, тот же сбой присутствовал до 6.10.

**Не утверждается:** два успешных ответа реальных GPT/Claude аккаунтов, параллельность подписочных сетевых запросов, лимиты и биллинг, unattended Claude на VPS, cross-host распределение, production deployment. В тесте реальные OS-процессы, но **синтетические детерминированные LLM-ответы**. Секреты, существующий Netcup, производство и платные API не затронуты.

Откат исходника 6.10 при необходимости — адресный обратный commit от проверенного текущего main к состоянию до 6.10 `724f9e2971e8271b610efb1fbd8c39d21bb1fe1d`, без force/reset.