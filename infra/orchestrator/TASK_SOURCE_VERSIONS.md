# 6.13 — точная версия Git-исходника после исполнения задачи

## Реализованный контракт

`TaskSourceVersionRecorder` — **доверенный локальный Python-мост** после успешного завершения задачи; ни текст модели, ни HTTP-запрос не могут самостоятельно выбирать `task_id`, `worker` или произвольную папку.

```python
from ai_agent_engine.task_versions import TaskSourceVersionRecorder

# baseline_sha получен доверенным координатором из Git HEAD ДО работы агента.
recorder = TaskSourceVersionRecorder(inbox, owner_scope)
receipt = recorder.record(runner, outcome, baseline_commit=baseline_sha)
# После рестарта/перед выкладкой или передачей результата:
pinned = recorder.read_version(outcome.task_id)    # запись из SQLite
verified = recorder.verify_version(outcome.task_id)  # НОВАЯ проверка файлов Git
```

Условия `record`: `outcome.executed is True`, состояние `SUCCEEDED`, подтверждённые `provider audit` и `delivery.completed/success`, тот же доверенный owner/project/task/worker, исходное неизменное назначение workspace. Функция повторно захватывает `flock` исходной физической рабочей папки после освобождения исполнителем. Если папку уже заняла новая задача, отказ `workspace_busy` до чтения. Нет автоматического назначения прав.

## Что именно означает версия

- `baseline_commit`: валидный Git SHA-1 (40 hex) либо SHA-256 (64 hex), известный координатору **до исполнения**. Он должен существовать в рабочем Git и быть предком итогового коммита.
- `result_commit`: фактический локальный `HEAD^{commit}` после исполнения. Если исполнитель создал новый Git-коммит в своей разрешённой копии, фиксируется именно новый commit ID, а не старое состояние. Несвязанная история, неизвестный baseline и переписанный baseline отвергаются (`source_diverged`).
- `patch_sha256`: `null` для чистого дерева или проектный SHA-256 **канонической комбинации** `git diff --binary --no-ext-diff --no-textconv HEAD --` и прочитанных байтов каждого неигнорируемого untracked файла с именами/длинами. Это **не** стандартный `git patch-id`, не Git-коммит и **не** сохранённые для восстановления байты diff. Без полного исходника по одному digest восстановить незакоммиченные изменения нельзя.
- `recorded_at`: дата записи, выданная SQLite.

Хранение: новая таблица `task_source_versions` в **отдельной приватной task SQLite v10**, по одной неизменяемой записи на `task_id`. Миграция v9→v10 затрагивает только эту БД, не рабочие БД/Netcup. Дубль отвергается (`already_recorded`), нет `UPDATE`/подмены версии. Owner-scoped `read_version` восстанавливает метаданные после рестарта.

### Повторная сверка и безопасность

`record` считывает HEAD+patch дважды до фиксации и третий раз после фиксации; при несовпадении возвращает `source_changed`. `verify_version` снова захватывает исходную папку и вычисляет HEAD/patch; если задача/другой процесс после записи изменили исходники, возвращается `source_changed`. **Исторический receipt при этом не переписывается**. Для окончательной выкладки доверенный координатор всё равно должен непосредственно перед `push/deploy` ещё раз проверить HEAD и актуальную версию удалённой ветки и не затирать чужие коммиты. Сам 6.13 не делает `commit`, `push` или deploy.

Git инспектируется только фиксированными read-only командами, без shell, без Git network, с очищенным окружением, отключёнными глобальными конфигурациями, hooks, fsmonitor, external diff и replace objects. Вывод и время ограничены. Требуется настоящий корневой `.git` каталог в выданной workspace (не `.git` symlink/file), Linux и отсутствие submodule/unmerged index. Untracked файлы читаются из закреплённого directory FD через `openat/O_NOFOLLOW`; symlink, смена inode, небезопасные права и выход из каталога отвергаются. Не более 8 МиБ вывода Git, 8 МиБ незакоммиченных untracked bytes и 4096 untracked файлов. Ignored файлы не включаются; полученный digest относится к **Git-учитываемым** исходникам, не ко всей файловой системе.

При изменении файла непосредственно **после последней проверки** версия уже может устареть. Поэтому `verify_version` и повторная сверка удалённого `main` перед публикацией обязательны. Здесь нет атомарной межсерверной блокировки, гарантии целостности удалённого origin, подписи Git-коммитов или автоматического восстановительного patch-архива. Это не переход к unattended Claude на VPS.

## Отказ и проверки

Только обезличенные коды: `invalid_source`, `trusted_context_required`, `task_not_complete`, `workspace_busy`, `unsafe_repository`, `secure_io_unavailable`, `source_too_large`, `unsafe_source`, `source_changed`, `source_diverged`, `unsupported_repository`, `git_unavailable`, `already_recorded`, `storage_unavailable`, `not_found`, `version_corrupt`. Путь проекта, конфигурация Git, diff и секреты не включаются в ответ/аудит.

`python3 -m unittest tests.test_task_versions -v`: 14 offline-тестов на временных настоящих Git-репозиториях и реальных gated subprocess GPT/Claude, без сетевых обращений к моделям. Проверяются clean/dirty/fast-forward commit, untracked bytes, изменение файлов после записи, wrong base и unrelated history, symlink, oversized untracked, повтор, чужой owner, занятая workspace, сбой БД, гонка между двух snapshots, подмена receipt и миграция v9→v10.

Должны быть повторно пройдены GitHub Actions Ubuntu Python 3.11/3.12/3.13 и wheel; общая Windows CI из-за Linux-only `fcntl` оставалась красной до 6.13. Нет изменений рабочего Netcup, production/секретов/платных API. Исходный `main` до 6.13: `3e07028ad66cafb57c5f8ab2e9c692a8de62ff3b`. Откат GitHub-изменений — адресный обратный коммит после проверки текущей вершины, без force/reset.
