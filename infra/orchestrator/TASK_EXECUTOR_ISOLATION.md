# 6.8 — Изоляция локального исполнителя и маскирование секретов

**Статус:** 18.11.2026, локальный Linux PASS. Код: `ai_agent_engine/task_isolation.py`, интеграция с `task_commands.py` и ограничение окружения `task_gpt.py`. Это не развёртывание на Netcup и не повторный live-вызов GPT/Claude.

## Реальная граница для offline task commands

Каждый разрешённый в 6.7 subprocess теперь запускается **только через Bubblewrap**, с реальными user/PID/IPC/UTS/mount namespaces и отключённым вложенным созданием user namespace. Корень, видимый программе, собран из ограниченного набора read-only runtime mounts (`/usr/bin`, `/usr/lib`, `/usr/share`, при наличии `/usr/libexec`, `/lib`, `/lib64`), нового `/proc`, минимального `/dev`, временного `/tmp`, пустого `/etc` и назначенной SQLite task workspace, закреплённой по проверенному inode **как открытый FD** в `/workspace`. `/home`, `/usr/local`, корневая файловая система, секреты и проекты хоста не монтируются. По умолчанию workspace read-only для Git/syntax; для `python_unit_tests` — writable, только после обоих грантов и trusted opt-in из 6.7. `stdin` неинтерактивен, stderr не отправляется в результат, stdout до 4096 байт, сроки прежние.

В этом исполнении Linux `bwrap --unshare-net` не смог настроить loopback из-за `NETLINK_ROUTE: Operation not permitted`. Поэтому **отдельный network namespace не заявлен**. Вместо него Bubblewrap устанавливает экспортированный **libseccomp BPF** на уровне ядра, который блокирует создание и использование сетевых сокетов (`socket`, `connect`, `sendto` и др.), а также системные вызовы `ptrace`, mount/namespace escape и другие опасные интерфейсы. Реальный `socket(AF_INET, SOCK_STREAM)` в sandbox получает `EPERM`. Фильтр и sandbox обязательны: если Bubblewrap, libseccomp или нужные kernel permissions недоступны, `TaskCommandRunner` **отказывает в запуске** — небезопасного fallback нет. Это профиль для offline-кода; не для провайдеров с необходимым сетевым доступом.

## Граница секретов и ответов

- Offline subprocess получает фиксированную среду `HOME=/workspace`, без наследования OAuth/API-токенов, `LD_PRELOAD`, произвольного `PYTHONPATH`, private Git config и прочих переменных хоста. Внешние credentials нельзя открыть по пути, поскольку домашний каталог хоста отсутствует в mount namespace.
- GPT/Claude **сетевые** subprocess остаются отдельным доверенным owner-side путём. Их окружение заменено белым списком служебных переменных (`HOME`, PATH, язык, ограниченные proxy/CA/XDG настройки), `PYTHONPATH` принудительно указывает на доверенный установленный пакет. Личные OAuth/CLI файлы по-прежнему доступны самому доверенному provider subprocess в его `HOME`: иначе уже проверенные subscription входы сломаются. **Это не процессная OS-изоляция сетевых GPT/Claude**, запуск этих рабочих процессов нельзя считать sandboxed.
- Вывод offline tools и успешные текстовые ответы GPT/Claude перед возвратом проходят `redact_text`: прямые значения чувствительных host environment переменных (длина не менее 8), характерные API/GitHub/Slack token shapes, `Bearer`/`Authorization` и значения `password`/`token`/`secret` полей. Ошибки и журналы доступа по-прежнему содержат только безопасные категории. Это защита от случайного вывода; она **не гарантирует** поиск обфусцированных, частичных или неизвестных секретов внутри произвольного текста.

## Проверено

`python3 -m unittest tests.test_task_isolation tests.test_task_commands -v` и `python3 -m unittest tests.test_task_gpt -q`:

- Реальные попытки чтения host-only файла и `/etc/passwd` из sandbox — недоступны; `/usr/local` не виден.
- Git/syntax в read-only workspace не могут писать; тестовый writable runner меняет только назначенную workspace, внешняя копия файла неизменна.
- Создание сетевого socket блокируется ядром (`EPERM`); наследованные fake API/OAuth токены не проходят; утечка заранее известного host secret в stdout маскируется.
- При недоступном Bubblewrap или libseccomp subprocess не запускается; положительные тесты старого 6.7 проходят с sandbox.
- GPT fake subprocess выдаёт тестовую строку, равную host `SESSION_TOKEN`: наружу возвращается `[REDACTED]`, не токен; provider audit содержит только `completed`. Парсер валидации ответа и реальные одноразовые offline process fixtures старых этапов проходят без изменения.

**Ограничения:** один и тот же UNIX UID у хостового доверенного оркестратора и текущего GPT/Claude worker; нет отдельного service UID, полноценного network namespace, блокировки любого возможного side-channel, определения/уборки процессов после гибели контроллера (**6.9**) или sandbox для live provider CLI. Нет нового платного API и live Netcup/production/секретных изменений. Использованный тестовый Linux поддерживает Bubblewrap 0.12.0; доступность и настройки на Netcup **не проверены**.

Источник: GitHub `vl-ktk/ai-agent-engine/main`, база перед 6.8 — `c7b085dc0a567d1fd0cbb8bed53995c5b9a51f9b`. Откат изменений 6.8 — точечный новый revert-коммит от актуального `main`, без force/reset; серверного релиза не было.

Официальные спецификации: [Bubblewrap](https://github.com/containers/bubblewrap/blob/main/README.md), [libseccomp](https://github.com/seccomp/libseccomp/blob/main/include/seccomp.h.in).
