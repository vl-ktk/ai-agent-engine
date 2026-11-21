# Known issues — AI Agent Engine

Реестр повторяемых ошибок и защит от их возвращения. Перед изменением
затронутого компонента проверять этот файл и соответствующие тесты.
Новые факты записывать только по реальным логам/тестам; неподтверждённые
исправления помечать как ожидающие live-проверки. Не включать токены,
приватные пути пользователя, тексты ответов моделей или рабочие данные.

## AE-711-001 — root-owned временный Python-пакет недоступен ae-runtime

- **Симптом:** `ModuleNotFoundError: ai_agent_engine.task_stage7_11_acceptance`
  при запуске изолированной stage 7.11 acceptance от имени `ae-runtime`.
- **Доказательство:** [Netcup owner run #44805647740](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44805647740):
  `runtime_file_readable=false`, `runtime_package_traversable=false`,
  `private_package_visible=false`. Модели не вызывались, production SQLite
  и сервисы не изменялись.
- **Причина:** root распаковывает проверенный source в собственный закрытый
  каталог и `shutil.copytree` переносит владельца файлов как root; владелец
  временной директории `ae-runtime` сам по себе не даёт доступ вложенному
  пакету. Менять права исходного архива/production нельзя.
- **Исправление:** после копирования сменить владельца **только нового
  временного пакета и всех его файлов/подкаталогов** на точный uid/gid
  `ae-runtime`, сохранив приватные modes и запретив symlink/нестандартные
  файлы. Ни source, ни live release, ни секреты, ни рабочая БД не трогаются.
- **Защита:**
  `tests/test_stage7_11_netcup_live_acceptance.py::test_root_owned_disposable_copy_is_recursively_owned_by_runtime`,
  плюс синтетическая CPU/provider acceptance в disposable SQLite v17.
- **Статус: FIXED для изолированной Netcup acceptance.** Исходник
  [PR #106](https://github.com/vl-ktk/ai-agent-engine/pull/106) прошёл
  Linux 3.11/3.12/3.13 и wheel CI, после чего реальный
  [Netcup OFFLINE #44807319708](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44807319708)
  вернул `OFFLINE_PASS`, `runtime_file_readable=true`,
  `runtime_package_traversable=true`, `private_package_visible=true`.
  Ограниченный [LIVE #44808394097](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44808394097)
  затем подтвердил `LIVE_PASS` одного GPT и одного Claude без изменения
  production core/SQLite. Это **не** подтверждение постоянного live-dispatch
  и не разрешение автоматически менять рабочую базу.

## CI-313-001 — финализаторы SQLite мешают тесту stderr в Python 3.13

- **Симптом:** `test_invalid_timestamp_never_raises_or_prints_stack_trace`
  дважды падал в Linux Python 3.13 из-за чужого `ResourceWarning` во время
  перехвата `sys.stderr`.
- **Доказательство:** [CI #44801949644](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44801949644)
  (включая повтор); предупреждения об незакрытых SQLite-соединениях других
  тестов подтверждены в job log.
- **Исправление:** узко подавлять только `ResourceWarning` внутри этого
  теста; реальные ошибки и traceback `auth.main(["status"])` по-прежнему
  обязаны оставлять `stderr` пустым.
- **Защита:** `tests/test_cli_sanitization.py` и Linux 3.11/3.12/3.13 CI.
- **Статус:** Linux 3.13 прошёл в [CI #44802762312](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44802762312).

## CI-WIN-001 — POSIX-only зависимости на Windows

- **Симптом:** Windows CI продолжает падать на `fcntl`, `pwd`, `grp` и
  POSIX-зависимых тестах даже при зелёных Linux и wheel.
- **Причина:** код и часть тестов рассчитаны на Linux/Netcup; полного
  Windows-порта пока нет.
- **Новая проверка 21.11.2026:** в [CI #44882782277](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44882782277) для WEB-02 общий Windows Python 3.12 job вновь FAILED (4 test failures, 44 errors; логи показывают `fcntl`, `pwd`, `grp`, отсутствие `socketserver.UnixStreamServer` и `os.geteuid`). Linux 3.11/3.12/3.13 и wheel jobs PASS, **все 12 тестов нового `test_chatgpt_web_web02.py` на Windows также PASS**. Это подтверждение старой ошибки, **не новая поломка `chatgpt_web`**.
- **Повторная проверка WEB-03:** [CI #44884522475](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44884522475) — Windows 3.12 снова 4 failures / 44 errors на POSIX-only коде, Linux 3.11/3.12/3.13 и wheel PASS. **Все 14 новых `test_chatgpt_web_web03.py` тестов на Windows PASS**. Исходная проблема остаётся открытой; это не регресс новой изоляции агентов и не повод скрывать красный CI.
- **Повторная проверка WEB-04:** [CI #44885546675](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44885546675) — тот же Windows 3.12 FAIL (4 failures, 44 errors); Linux 3.11/3.12/3.13 и wheel PASS. **Все 14 новых `test_chatgpt_web_web04.py` Windows тестов PASS** по фактическому job log. Это прежний CI-WIN-001, не новый дефект ChatGPT Project mock gate.
- **Повторная проверка WEB-05:** [CI #44889580956](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44889580956) — тот же Windows 3.12 FAIL (4 failures, 44 errors); Linux 3.11/3.12/3.13 и wheel PASS. **Все 17 новых `test_chatgpt_web_web05.py` Windows тестов PASS**. Старую POSIX-проблему не закрывали и не выдаём за регресс ChatManager.
- **Повторная проверка WEB-06:** [CI #44891594867](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44891594867) — Windows 3.12 снова FAIL (4 failures, 44 errors), Linux 3.11/3.12/3.13 + wheel PASS, **все 18 новых `test_chatgpt_web_web06.py` Windows unit PASS**. Старую проблему не закрывали и не выдаём за регресс model selection.
- **Статус:** открыто. Не выдавать `CI all green`; это известное ограничение,
  а не доказательство корректности Windows. Перед Windows-релизом требуется
  отдельная совместимость и отдельная приёмка.

## Порядок пополнения

1. Зафиксировать номер инцидента, симптомы, подтверждённую причину и ссылку
   на обезличенное доказательство.
2. Исправить первопричину в источнике (не только симптом), добавить
   регрессионный тест либо документировать невозможность автоматической
   проверки.
3. Сверить live-версию и проверить CI; для Netcup отдельно подтвердить
   поведение на реальной одноразовой изолированной проверке.
4. Обновить статус здесь и краткий `HANDOFF.md`, указать source/live SHA
   для отката и не повторять старую ошибку в следующем изменении.
