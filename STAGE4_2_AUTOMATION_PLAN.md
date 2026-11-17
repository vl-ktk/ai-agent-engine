# AI Agent Engine — этап 4.2: автоматизация управления VPS

**Утверждение:** 2026-11-17. Владелец принял подготовленный Netcup VPS (закрыть 4.1), разрешил начать 4.2 и выбрал схему **GitHub Actions + Ansible + SSH** как начальную автоматику.
**Источник истины:** `vl-ktk/ai-agent-engine`, ветка `main`; этот список отдельный детальный реестр работ по 4.2, а не ещё один глобальный roadmap.
**Фактическое состояние на старте:** Netcup SCP показывает RUNNING, Debian 13 minimal, 4 vCPU/8 ГБ/160 ГБ. SSH-подключения, Ansible и deploy ещё НЕ проверялись.
**Граница:** только управляющий VPS проекта AI Agent Engine. Не внедрять в KTK Social, ATHL, GPU/125B или iPad.

**Проверки для 1.1–1.5:** [PR #26](https://github.com/vl-ktk/ai-agent-engine/pull/26) смержен squash в `main` `a6a8a1c3d9442bb364ef55317a029c2fa30db22f`. [Ansible offline CI #44093814944](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44093814944) — PASS 1/1 (inventory, playbook syntax, без SSH); [основной CI #44093814678](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44093814678) — PASS 4/4. Предыдущий `main` для rollback: `60bdb12b7732a61f0920b5cd54acb93bd424379a`. Работа на Netcup VPS не проводилась. **Пункт 1 принят владельцем 2026-11-17** («Да»).


**Живые проверки 2026-11-17:** [GitHub Actions #44110311382](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44110311382) — создание `ae-deploy` и реальный SSH SUCCESS; [read-only Ansible #44112659558](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44112659558) — SUCCESS (`ok=1 changed=0 unreachable=0 failed=0`). Ни пакеты, ни firewall, ни backup, ни production этим не настроены. [Debian 13 non-root baseline #44114355675](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44114355675) — SUCCESS: check `changed=2`, apply `changed=2`, повторный check `ok=4 changed=0 unreachable=0 failed=0`. Это подтверждает только пользовательские каталоги (0700), не системную настройку.

## Подробный план — 10 пунктов

- [x] **1. Подготовить конфигурацию автоматизации в GitHub.** **Технически PASS и принят владельцем 2026-11-17.**
  - [x] 1.1. Сверить актуальный `main`, `HANDOFF.md` и текущую CI-конфигурацию.
  - [x] 1.2. Завести этот отдельный план, зафиксировать принятие 4.1 в глобальном реестре.
  - [x] 1.3. Создать безопасный Ansible-каркас: конфигурацию, пример inventory, read-only SSH/ping playbook.
  - [x] 1.4. Добавить GitHub Actions для **офлайн-проверки** Ansible; без реального доступа к VPS и без секретов.
  - [x] 1.5. Проверить CI, принять PR в `main`, зафиксировать SHA и rollback.
- [ ] **2. Подготовить изолированный SSH-доступ развёртывания.** **Частично выполнено на реальном Netcup VPS 2026-11-17:** публичный ключ установлен, non-root `ae-deploy` создан, реальный вход через SSH проверен. Права, удаление временного root-secret и резервный доступ ещё требуют завершения.
  - [x] 2.1. Подготовить отдельный Ed25519-ключ deploy-доступа; публичную часть — для VPS, приватную — только в защищённое хранилище. **PASS:** зашифрованный GitHub Actions secret `AE_DEPLOY_PRIVATE_KEY`; никакого приватного ключа в git.
  - [x] 2.2. Однократно создать отдельного системного пользователя и авторизовать публичный SSH-ключ без изменения существующего root-логина. **PASS:** вместо неудобного SCP Screen выполнен согласованный guarded `workflow_dispatch` через GitHub Actions; [run #44110311382](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44110311382) — Success, `create-deploy-user` и `Verify non-root SSH access` успешны.
  - [ ] 2.3. Проверить реальные права, минимально необходимые sudo-действия, резервный консольный путь и SSH host fingerprint. **Частичный live PASS:** [audit #44115931152](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44115931152) подтвердил non-root `ae-deploy`, единственную группу `ae-deploy`, pinned SSH host key; глубокий sudoers/SCP fallback без отдельного административного доступа не подтверждён.
  - [ ] 2.4. Не открывать дополнительных административных портов и не давать CI постоянный root-доступ.
- [ ] **3. Создать защищённые настройки GitHub Actions.**
  - [x] 3.1. Записать приватный deploy-ключ в GitHub Actions repository secret (не в файлы репозитория, workflow, логи или чат). **PASS:** `AE_DEPLOY_PRIVATE_KEY` создан и использован для успешного реального SSH.
  - [ ] 3.2. Указать адрес VPS, пользователя и заранее подтверждённый SSH host key; установить только необходимые права workflow.
  - [ ] 3.3. Добавить условия ручного допуска к потенциально опасным операциям с учётом реальных ограничений тарифа GitHub.
- [ ] **4. Доказать реальное подключение GitHub → VPS.** Реальный Ansible `ping` завершён; остаётся live OS/права-аудит и проверка всего состава сведений.
  - [x] 4.1. Запустить отдельный ручной workflow, выполнить безвредный Ansible `ping`. **PASS:** [run #44112659558](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44112659558), `ok=1 changed=0 unreachable=0 failed=0`; SSH key-only, no root.
  - [x] 4.2. Проверить имя/OS/доступ и отсутствие опубликованных ключей в логах; зафиксировать успешный run ID. **PASS:** [read-only audit #44115931152](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44115931152) проверил hostname `v2202611460959561466`, Debian 13.7 и пользователя `ae-deploy`; job `ok=7 changed=0 unreachable=0 failed=0`, маркеров приватного SSH-ключа в логах нет; значения root-пароля не использовались.
  - [x] 4.3. Ошибки доступа решать без изменения production-сервисов. **PASS:** workflow syntax fix [PR #31](https://github.com/vl-ktk/ai-agent-engine/pull/31) (first bad YAML rejected with 0 jobs), then clean run; no prod/OS modification.
- [x] **5. Создать воспроизводимую Ansible-конфигурацию Debian 13 в non-root scope.** **PASS 2026-11-17:** [PR #32](https://github.com/vl-ktk/ai-agent-engine/pull/32) merged `00f8ca5f7b6207e0721d45125844f0524d602843` (rollback code `1839cd8430922149102b4cb603e1268f55e2ad9e`), offline CI both PASS, [live run #44114355675](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44114355675) PASS. Системные apt-пакеты/привилегии/сервисы отложены, **не установлены**.
  - [x] 5.1. Зафиксировать базовые пакеты и Python runtime с совместимыми версиями. **Документировано:** [DEBIAN13_BASE.md](./infra/ansible/DEBIAN13_BASE.md), Debian 13 Python 3.13; базовые apt-пакеты **не установлены**, административный доступ не расширялся.
  - [x] 5.2. Добавить idempotent playbooks для подготовительного состояния. **PASS:** `infra/ansible/debian13-user-baseline.yml`, user `ae-deploy`, Debian 13/Python assertions, два приватных каталога. На VPS повторный check показал `changed=0`.
  - [x] 5.3. Прогон `--check --diff`, затем управляемое применение и повторный `--check --diff`. **PASS:** [GitHub Actions #44114355675](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44114355675), check `changed=2`, apply `changed=2`, idempotency check `changed=0`; `unreachable=0 failed=0` на всех трёх прогонах.
- [ ] **6. Подготовить безопасность и системные процессы.**
  - [x] 6.1. Инвентаризировать порты, SSH, сетевую политику, сервисы и системные журналы. **Live PASS в non-root scope:** [GitHub Actions #44121113323](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44121113323), `ok=18 changed=0 unreachable=0 failed=0`. Debian 13.7, `ae-deploy`; TCP LISTEN только IPv4/IPv6 SSH `:22`, UDP LISTEN пусто; `ssh.service` и `systemd-journald.service` активны; 10 системных служб работают, failed нет; метаданные journald доступны, постоянный и runtime каталоги есть. Netcup SCP Firewall включён, исходящий SMTP 25/465/587 блокируется, но финальные implicit rules принимают **весь входящий и исходящий** трафик. `sshd -T`, `sudo -n -l` и `nft list ruleset` недоступны в правах `ae-deploy`, поэтому **НЕ подтверждены** effective SSH root/password policies, полные sudoers и guest nftables. Это границы инвентаризации, а не доказательство защищённости; ограничение входящего трафика и проверка привилегированных политик — отдельный 6.2 после явного согласия. Пароль и секреты не изменялись.
  - [ ] 6.2. **OOB PASS / SSH HARDENING HOLD.** 2026-11-17: повторный root login в SCP Screen PASS; первая ошибка была путаницей I/l. Корневой пароль не менялся, выполнен выход до login. Привилегированная read-only проверка: sshd -T показал port 22, permitrootlogin yes, passwordauthentication yes, pubkeyauthentication yes; nft list ruleset пустой. DNS Netcup настроен, NTP=yes и NTPSynchronized=yes, peer Debian pool IPv6. SCP firewall по-прежнему implicit ACCEPT ANY IN/OUT с default SMTP/Ping; ничего не применяли. [Безопасный план 6.2](infra/firewall/NETCUP_SCP_ROLLOUT.md). До закрытия 6.2 — отдельное согласие на SSH hardening / firewall, новые SSH-проверки, UDP/rollback; пароль root менять не требуется.
  - [ ] 6.3. Настроить отдельных сервисных пользователей, изоляцию каталогов, systemd, ограничение журналов и мониторинг.
  - [ ] 6.4. Изменение секретов, расширение прав или продакшен-выкладка — только по отдельному согласию владельца.
- [ ] **7. Реализовать контролируемый CI/CD проекта.**
  - [ ] 7.1. CI: сборка, offline unit/integration tests, секрет-сканирование и Ansible syntax/check.
  - [ ] 7.2. CD: строго выбранный commit/tag, предварительная проверка HEAD, подготовка релиза, контролируемый запуск без произвольного shell от root.
  - [ ] 7.3. Отделить проверочные задачи от задач с правом выкладки; не запускать CD на произвольных PR.
- [ ] **8. Добавить наблюдение и откат.**
  - [ ] 8.1. Health checks и журналы статусов с понятной причиной сбоя.
  - [ ] 8.2. Хранить идентификатор предыдущего стабильного релиза и сценарий отката; проверить его на тестовой сборке.
  - [ ] 8.3. Резервное копирование данных и проверку восстановления выполнять в отдельном глобальном подпункте **4.3**; не считать сделанными здесь.
- [ ] **9. Провести безопасный end-to-end тест автоматики.**
  - [ ] 9.1. Протестировать workflow: GitHub → SSH → Ansible → состояние VPS → отчёт.
  - [ ] 9.2. Проверить повторный запуск, контролируемую ошибку, отмену, блокировки и откат.
  - [ ] 9.3. Приложить run IDs, версии, результаты и список невыполненных проверок.
- [ ] **10. Подготовить развёртывание AI Agent Engine.**
  - [ ] 10.1. Описать service units, рабочие каталоги и параметризацию под runtime.
  - [ ] 10.2. Подготовить конфиг для перехода на этапы 4.3/5/6; не заявлять модельные OAuth/live tests как пройденные.
  - [ ] 10.3. Подтвердить, что без домашнего ПК доступны управляемые операции, а ненужные порты закрыты.

## Контроль качества и полномочия

- Ключи, пароли, OAuth-токены, SSH private key и другие секреты **не коммитить**. Для GitHub Actions использовать secrets; для SSH — проверку host key (не `StrictHostKeyChecking=no`).
- Проверочные CI-workflows не имеют SSH-ключа, не выходят на VPS и не получают административных прав.
- Все изменения серверных прав, секретов, firewall, оплачиваемых ресурсов или production разрешать отдельно. Исключить самовольные расходы.
- Каждый пункт закрывать только по фактическим проверкам и приёмке владельца, с указанием ID workflow/release и пути отката.
- Перед публикацией повторно проверить текущий `main`, синхронизировать с ним изменения, проверить тесты и обновить `HANDOFF.md`.
- Stage 1.6–1.7 (живые Claude/GPT тесты) и Stage 4.3 (реальные резервные копии) остаются самостоятельными открытыми задачами.
