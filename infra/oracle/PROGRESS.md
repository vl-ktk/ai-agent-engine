# Oracle — фактический прогресс

Дата: 2026-11-20. Ветка `infra/oracle-setup`. Это **отдельный пилот**, не статус основного roadmap AIE.

| Шаг | Статус | Подтверждение |
| --- | --- | --- |
| OR-0 Документы и ветка | DONE после проверки коммита | GitHub branch и четыре документа |
| OR-1 Инвентаризация tenancy 1 | TODO | Требуется актуальная проверка Console |
| OR-2 Создание VM1 | TODO | **Нет подтверждения, что VM уже создана** |
| OR-3 Безопасность VM1 | TODO | Нет ни адреса, ни проверенного доступа |
| OR-4 Управление/MCP | TODO | MCP в этом чате к Oracle не подключён |
| OR-5 Второй tenancy / VM2 | TODO | Нужна отдельная проверка другого аккаунта |
| OR-6 Единый управляющий endpoint | TODO | Архитектура не развёрнута |
| OR-7 Бэкап и restore | TODO | Нет независимого теста |
| OR-8 Read-only аудит AIE | TODO | Нельзя заменять аудит прошлой перепиской |
| OR-9 Изолированный staging | TODO | Не запускался |
| OR-10 GO/NO-GO cutover | TODO | Решение владельца отсутствует |

## Уже подтверждено (без live доступа к OCI)

- По присланным владельцем скриншотам: Oracle Console открывалась в Germany Central (Frankfurt), tenancy успешно provisioned; список Compute Instances в выбранном root compartment был пуст; открывался мастер создания VM.
- Важно: пустой список в одном compartment/region — **не полный аудит всех ресурсов tenancy**. Последующий запуск сервера не подтверждён.
- По GitHub: `main` использует отдельный, активный `IMPLEMENTATION_ROADMAP.md`; Netcup этап 7.7 отмечен PARTIAL/HOLD. Ветка Oracle не должна его менять.
- Перед созданием ветки проверены все 13 workflow в `.github/workflows/`: `push` на `main` либо только ручные триггеры; push в `infra/oracle-setup` не должен запускать их.

## Следующий единственный шаг

**OR-1: read-only проверка первого Oracle аккаунта.** От владельца — актуальные скриншоты по home region, Free Tier/Cost Analysis, Compute → Instances и выбору Always Free A1. Секреты, passkey и пароли не пересылать. Не нажимать Create до проверки shape, storage и стоимости.

## Проверки и журнал

- 2026-11-20: read-only GitHub repository/root/HANDOFF, workflow triggers и отсутствие целевой ветки — PASS.
- 2026-11-20: официальные документы Oracle Free Tier, Run Command и `oracle/mcp` просмотрены; отдельное действующее OCI подключение — **NOT TESTED**.
- Выполнение команд на Netcup, VM и AIE тестов в этом шаге: **NOT RUN**.
