# Stage 4.2 / 6.2 — Netcup SCP Firewall и SSH: журнал внедрения

**Фактический статус на 2026-11-17: LIVE APPLIED; новый SSH IPv4 PASS; DNS и прямой UDP/NTP тест PASS; IPv6/negative/SSH Match tests пока не проверены.** Первоначальный preflight-план ниже сохранён как исторический документ. Его старые утверждения об отсутствии политики не описывают текущее состояние.

## Применённые изменения

1. По разрешению владельца создана конфигурация /etc/ssh/sshd_config.d/00-hardening.conf: PermitRootLogin no; PasswordAuthentication no; KbdInteractiveAuthentication no; PubkeyAuthentication yes. sshd -t и основные sshd -T значения PASS. systemctl reload ssh SUCCESS. Пароль root не изменялся; перезагрузки VPS не было.
2. До Netcup firewall: новый SSH key-only вход для ae-deploy через GitHub-hosted runner и закреплённый host key, read-only Ansible ping [#44130562809](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44130562809): SUCCESS, 23 секунды.
3. Создана и назначена **только VPS v2202611460959561466** политика Netcup с названием **AI Agent Engine VPS - SSH and NTP ingress**. Правило Allow SSH TCP22 = INCOMING TCP ACCEPT, source any, source port any, destination any, destination port 22. Правило Allow NTP UDP reply source123 = INCOMING UDP ACCEPT, source any, source port 123, destination any, destination port any. Пользовательских EGRESS правил нет; стандартные Mail block/Ping allow сохранены.
4. Через Edit Policies → Apply → Save → OK политика применена. После повторной загрузки Netcup SCP показывает её на сервере, implicit **DROP ALL INCOMING** и **ACCEPT ALL OUTGOING**, без сообщения о несохранённых изменениях. Применение без перезагрузки.
5. После firewall: новое соединение SSH IPv4 и read-only Ansible ping [#44134072220](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44134072220): **SUCCESS, 31 секунда**. Это новая TCP-сессия после изменения, а не существовавшее соединение.
6. Сетевой интерфейс eth0 имеет статические IPv4 62.83.131.128/22 и IPv6 в сети 2a03:4000:19:1ea0::/64 по /etc/network/interfaces.d/50-cloud-init.cfg; DHCP не используется в текущем конфиге. До firewall DNS Netcup и NTP были синхронизированы.

## Проверка DNS/NTP после назначения Netcup firewall (2026-11-17)

- Root Out-of-Band console Netcup SCP Screen, Debian 13 VPS; никаких изменений конфигурации/сервисов.
- DNS: `getent hosts debian.org` вернул адреса, включая `2a04:4e42::644`; PASS.
- Время: `timedatectl show -p NTP -p NTPSynchronized` → `NTP=yes`, `NTPSynchronized=yes`; `timedatectl timesync-status` → сервер `2a01:b2e0:2::61`, stratum 2, packet count 9, offset +16.528ms, состояние leap normal.
- Свежий UDP NTP ответ: через стандартный `python3` без установки пакетов отправлен пакет на `0.debian.pool.ntp.org`/UDP 123; новый ответ: длина 48, mode 4 (server), stratum 2. **PASS реального UDP return трафика при действующем firewall.** Сама systemd-timesyncd не перезапускалась, момент нового автоматического цикла не отслеживался.
- Остались отдельными пунктами: SSH по IPv6 (если используем), отрицательная проверка закрытого порта, effective SSH Match/source-specific анализ. **Этап 6.2 не закрыт**.

## Контрольный список

- [x] Конкретное согласие владельца на firewall.
- [x] Проверенные root Console Screen и базовый sshd hardening; пароль root неизменен.
- [x] Новый SSH key-only IPv4 до и после применения.
- [x] Сохранённая policy видна после обновления страницы; implicit IN DROP / OUT ACCEPT.
- [x] Штатные Netcup Mail/Ping сохранены; нет дополнительного EGRESS правила.
- [x] DNS **после** изменения: `getent hosts debian.org` вернул IPv6-адреса; запрос к `0.debian.pool.ntp.org` дополнительно подтвердил разрешение IPv4 A-записи для NTP.
- [x] **Свежий NTP UDP-обмен после firewall:** read-only Python socket отправил 48-байтный UDP NTP-запрос на `0.debian.pool.ntp.org:123`; получен новый ответ 48 байт, NTP mode=4, stratum=2. `timedatectl show -p NTP -p NTPSynchronized`: NTP=yes / NTPSynchronized=yes; `timedatectl timesync-status`: stratum 2, 9 пакетов, сервер Debian pool. Не проводились restart/forced resync сервиса; подтверждён сетевой NTP-обмен, а не отдельный запуск новой синхронизации системной службы.
- [ ] Вход SSH по IPv6 при необходимости, поскольку GitHub Actions тестировал IPv4.
- [ ] Отрицательный внешний тест заведомо закрытого TCP-порта, без сканирования чужих серверов.
- [ ] Проверка SSH Match/source-specific правил.

## Восстановление при проблеме

Открыть Netcup SCP → VPS → Firewall → Edit Policies. Переместить только политику **AI Agent Engine VPS - SSH and NTP ingress** из Selected в Available, нажать Apply → Save и подтвердить. Проверить, что implicit INCOMING вернулся к ACCEPT ALL, а OUTGOING остался ACCEPT ALL, затем повторить новый SSH ping. **Restore Default Policies не открепляет кастомную политику.** Сохраняется резервный доступ через Netcup SCP Screen. Откат SSH hardening — отдельная привилегированная операция с проверкой sshd -t, сейчас не делалась.

**Не заявлять пункт 6.2 завершённым** до оставшихся live проверок. Не открывать 80/443 или другие порты до отдельного согласования сетевой архитектуры и сервисов.

---

## Первоначальный preflight-план (историческое состояние до внедрения)

**Статус на 2026-11-17: OFFLINE PREPARED, NOT APPLIED.** Это документ с планом и критериями безопасности, НЕ выгруженная политика и НЕ подтверждение, что на VPS уже включена фильтрация. Изменения провайдерского firewall, OpenSSH, root, sudo, секретов, сервисов или production требуют отдельного явного разрешения владельца.

## 1. Исходная точка до внедрения (историческая)

- VPS Netcup, Debian 13.7, учетная запись автоматизации `ae-deploy`. Рабочая связка GitHub Actions → pinned SSH host key → SSH key → Ansible подтверждена; последнее чтение системы: [Actions 44121113323](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44121113323), `ok=18 changed=0 unreachable=0 failed=0`.
- Слушатели: TCP только 22 на IPv4/IPv6; UDP LISTEN нет. Это состояние **слушающих сокетов**, а не список разрешенных firewall направлений.
- Live Netcup SCP firewall всё ещё **ACCEPT ANY INCOMING / ACCEPT ANY OUTGOING**, кроме штатного SMTP block и ping правил. Живая команда root `nft list ruleset` вернула пустой ruleset: фильтрации через nftables сейчас нет.
- Live SCP Screen `root` login **PASS** на повторной попытке: первичный сбой был вводом lowercase `l` вместо uppercase `I`; пароль владельца не изменялся. Корневая сессия закрыта командой `exit` (возврат к Debian `login:`). При чтении от root `sshd -T` вывел `port 22`, `permitrootlogin yes`, `passwordauthentication yes`, `pubkeyauthentication yes`, `kbdinteractiveauthentication no`, `authenticationmethods any`, `usepam yes`. Это **публично доступный риск root/password SSH**, а не доказательство успешного SSH-логина по сети и не проверка всех Match rules. Полные sudoers отдельно не изучались, GitHub secret не использовался.

- DNS/NTP preflight: `/etc/resolv.conf` содержит DNS Netcup на IPv4 и IPv6, `NTP=yes`, `NTPSynchronized=yes`; `timedatectl timesync-status` показал динамический IPv6-пир Debian pool. Это не доказывает, что NTP UDP-ответы пройдут после назначения пользовательской Netcup INGRESS policy; необходима отдельная live проверка с подготовленным откатом. DHCP не проверялся.

## 2. Особенности Netcup — проверено по документации

1. Netcup автоматически изменяет неявное действие для **INGRESS** с `ACCEPT` на `DROP`, как только сохранено первое собственное входящее правило. Аналогично первый собственный EGRESS переведёт исходящее направление в `DROP`.
2. TCP-ответы на соединения отслеживаются состоянием; для UDP Netcup требует отдельно предусмотреть оба направления / ответные пакеты. Штатная политика уже разрешает обязательный трафик, в том числе DNS к DNS Netcup; нельзя автоматически предполагать, что этого достаточно для всех используемых DNS/NTP/DHCP адресов.
3. Новые правила после `Save` действуют немедленно без перезагрузки. Старое открытое SSH-соединение не доказывает доступность **нового** соединения.
4. `Restore Default Policies` не снимает назначенные пользователем политики. Плановый откат — **открепить кастомную политику от конкретного сервера** в `Firewall → Edit Policies → Save` и затем убедиться в возврате неявного `ACCEPT ANY` (только если это фактически наблюдается).
5. Провайдерский firewall даёт базовую сетевую защиту, а не заменяет конфигурацию SSH и гостевого firewall.

Источники: [Netcup Firewall](https://www.netcup.com/en/helpcenter/documentation/server/firewall), [GitHub-hosted runners IP ranges](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

## 3. Первоначальный черновик политики (исторический)

| Направление | Протокол | Источник | Destination | Действие | Условие |
| --- | --- | --- | --- | --- | --- |
| INGRESS | TCP | IPv4 any (`0.0.0.0/0`) | сервер, port 22 | ACCEPT | Оставить доступ стандартных GitHub-hosted runners к `ae-deploy`; доступ определяется только SSH-ключом, host key должен проверяться |
| INGRESS | TCP | IPv6 any (`::/0`) | сервер, port 22 | ACCEPT | Только если SCP действительно применяет отдельное правило IPv6; проверить отображаемый охват |
| INGRESS | ICMP / ICMPv6 | default built-in | сервер | ACCEPT | Сохранить штатную политику, убедиться, что не нарушается PMTU |
| INGRESS | UDP replies | Только согласованные внешние серверы DNS/NTP и исходные UDP source ports, если нужны | Эфемерные порты клиента VPS | ACCEPT | **Не создавать наугад**. Определить точные NTP/DNS адреса и поведение штатных Netcup правил по результатам preflight |
| EGRESS | all existing defaults | без нового кастомного правила | — | Без изменений | Не трогать SMTP block; не переключать implicit EGRESS в DROP |

Эффект только после создания и **назначения** собственной INGRESS policy серверу: прочие новые входящие соединения будут DROP; исходящий трафик должен остаться в прежнем режиме. Никакие HTTP/HTTPS входящие порты не добавлять до отдельного решения по архитектуре и доменам.

**Ограничение доступа по IP GitHub Actions:** стандартные GitHub-hosted runners имеют изменяемые общие адресные диапазоны; GitHub не рекомендует включать весь их список в allowlist. Поэтому для текущей автоматизации невозможно безопасно сделать статический узкий allowlist без смены способа запуска. Открытый интернет-порт TCP22 — осознанный компромисс **только при подтверждённой безопасной SSH-аутентификации**. Если проверка эффективного SSH показывает доступ root/password и владелец запрещает изменение — **STOP: не прикреплять эту политику и не заявлять 6.2 завершённым.** Этот план не санкционирует изменение пароля.

## 4. Первоначальный preflight checklist (исторический)

- [ ] Владелец отдельно разрешил **конкретную** публикацию политики. Нет молчаливого разрешения на password rotation, SSHD/системный firewall или root.
- [ ] Сохранены ID текущих назначенных SCP firewall policies и скриншоты текущего `ACCEPT ANY`; подтверждён рабочий вход в SCP.
- [x] Netcup SCP Screen доступна без SSH: проверен реальный root login, а после диагностики выполнен logout к `login:`. Порядок отката документацией определён (открепление policy через SCP), но **сам rollback пока не запускался**.
- [ ] Подтверждена новая SSH-сессия `ae-deploy` с доверенным host key, без пароля; подтверждена работа GitHub workflow на стандартном runner, без временных IP allowlists.
- [ ] Уполномоченно прочитаны фактические `sshd -T`/Match-условия, root/password authentication, и правила guest firewall. Без этой проверки при публичном SSH22 — HOLD.
- [ ] Сверены используемые DNS/NTP/DHCP источники, маршруты IPv4/IPv6, собственные SSH-подключения и API-клиенты; выяснено, какие UDP ответы необходимо явно пропустить. Сначала подготовить правила UDP, только потом назначать политику.
- [ ] Задокументированы проверка новой SSH-сессии, новые GitHub Actions run IDs, интернет/DNS/NTP и обратный путь в SCP. Ни один preflight не требует отправлять root-пароль в чат.
- [ ] Указано окно изменения и простой безусловный критерий немедленного отката; при разногласиях — не применять.

**Новый критический STOP:** пока sshd допускает `PermitRootLogin yes` и `PasswordAuthentication yes`, не объявлять SSH безопасным. Предлагаемое отключение root/password SSH не требует менять пароль root для SCP Screen, но изменение SSHD требует отдельного разрешения владельца.

## 5. Первоначальная процедура применения (историческая)

1. Прямо перед изменением снова прочесть текущие SCP назначения и GitHub main/политику; если они изменились, пересогласовать.
2. Подготовить отдельную policy с **всеми заранее проверенными INGRESS rules**, но не назначать серверу до конца preflight. Убедиться, что ни одно EGRESS правило не создано.
3. Назначить policy **только целевому VPS** и нажать `Save` в SCP; отметить время и точное изменение. Не перезагружать сервер.
4. **Немедленно проверить новое** SSH соединение по IPv4, GitHub Actions read-only ping, DNS, NTP, нужный UDP, IPv6 SSH если используется, а также проверку отказа на заведомо неразрешённом **тестовом** входящем порту. Не менять другие проекты/сервисы.
5. Если что-то недоступно либо новые подключения нестабильны: в SCP Firewall → Edit Policies **открепить новую политику**, `Save`, проверить возвращение исходных implicit правил и нового SSH. Если SCP доступ пропал, использовать проверенную out-of-band консоль и поддержку Netcup; не полагаться на старый SSH TCP сеанс.
6. Только после проверки и отдельного согласия зафиксировать live evidence в `STAGE4_2_AUTOMATION_PLAN.md` и `HANDOFF.md`, со всеми run IDs, видом изменённой политики и способом отката. НЕ помечать 6.2 выполненным раньше.

## 6. Стоп-условия и границы

**STOP**, если не проверены root/password effective SSH controls, нет живого рабочего OOB fallback, источники UDP не определены, SSH новая сессия падает или владелец не дал отдельное разрешение. Никакие платные операции, root password edits, тайные sudo изменения, deploy приложения/модели, внешние сообщения не входят в этот документ. Приготовленный план — не свидетельство живой настройки.
