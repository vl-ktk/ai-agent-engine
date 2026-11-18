# HANDOFF — 19.11.2026: Netcup full admin setup LIVE PASS (all 5 points)

**STOP:** the owner's five-point administrative task is complete. Do not start
roadmap **6.14** without a new instruction. Its GPT+Claude LIVE E2E remains
unperformed; no provider authorization, paid API or new service was introduced.

- **LIVE PASS:** [Actions #44416162976](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44416162976).
  Connected GitHub → hosted Actions → pinned SSH → ae-deploy sudo root.
  Package smoke, service smoke, deploy, actual rollback, final reactivation,
  health and unchanged SQLite/file inventories all PASS.
- **Installed:** package `0.3.11a0`, VPS release
  `0e04af007375d82daf8f7d3b36c71abb0c784e0b`.
  **Previous/rollback:** `85375103800845eeff696e53f510c66c428408ed`.
  Request commit `141f9362617621324066af6ceca7500eda16b110`.
- **Rights LIVE:** ae-deploy full NOPASSWD sudo, visudo PASS, root/password SSH
  remain disabled. Original configuration: `/root/ae-admin-20261119/`.
  Private online data backup/evidence:
  `/root/ae-admin-jobs/20261119005500000000000000000005/evidence/`.
- **Tests:** 657 hosted unittests OK; Linux 3.11/3.12/3.13 and isolated wheel PASS
  in [CI #44416162937](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44416162937).
  Whole CI remains red for pre-existing Windows fcntl/symlink issues.
- **Cloud autonomy:** the live request/retries, execution and result retrieval
  used only connected GitHub tools after bootstrap; no Windows files/processes,
  local SSH or provider console was part of execution. On-demand administration
  works independently of the PC. A continuously running GPT was not created.
- First startup failure exposed private-umask release permissions; automatic
  recovery to the old healthy version succeeded LIVE. Fixed and regression-tested.
  Seven local backup snapshots verified; working inventory unchanged. Off-host
  recovery remains unconfirmed.

**Full evidence:** [NETCUP_FULL_ADMIN_ACCESS_RESULT.md](./infra/deploy/NETCUP_FULL_ADMIN_ACCESS_RESULT.md).
**How to administer:** [NETCUP_OWNER_ADMIN.md](./infra/deploy/NETCUP_OWNER_ADMIN.md).
Original GitHub source before this task: `0c1c244f6bb2656c8d7c3bcafec981a2fb4a4294`.
Revert sources by addressed commits only; re-read GitHub and VPS before future
operations. Do not mistake later report/status commits for deployed versions.

---

# HANDOFF — 19.11.2026: Netcup baseline status verified, admin console BLOCKED

**Источник:** `vl-ktk/ai-agent-engine/main`. Владелец поручил начать пункт 1 [плана полной административной настройки](./infra/deploy/NETCUP_FULL_ADMIN_ACCESS_PLAN.md).

- **LIVE:** GitHub Actions [#44409546477](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44409546477) PASSED, actor `vl-ktk`, pinned SSH host PASS, `ae-controller.service` + `ae-backup.timer` + `ae-host-check.timer` active, Netcup current `releases/85375103800845eeff696e53f510c66c428408ed`. Исходник тестового status commit `fb4c57579f9c076b5b63c50cf3db948a0137557f`.
- **Блокер пункта 1:** доступна лишь непривилегированная SSH-проверка, нет действующей административной консоли/root в текущей инструментальной сессии. Состояние рабочих данных/backup-интегритет не проверено. Пункт 1 **PARTIAL**, права sudo не выдавались, Netcup не менялся.
- После административного входа проверить права, текущую версию, данные/бэкапы read-only; только потом пункт 2 (согласованная настройка админ-прав). После завершения пунктов 1–5 вернуться ровно к `6.14 [ ] LIVE BLOCKED` для GPT+Claude E2E.
- **Откат GitHub:** исходный main до документальной записи `fb4c57579f9c076b5b63c50cf3db948a0137557f`; не сбрасывать `main` силой, использовать адресный revert после повторной сверки HEAD. **Реальной VPS-публикации не было.**

---

# HANDOFF — 19.11.2026: отдельная задача полного админ-доступа Netcup

**Текущее решение владельца:** прежде чем продолжать разработку AI Agent
Engine, настроить **максимальный постоянный административный доступ** GPT к
существующему Netcup по уже работающей связке GitHub Actions → SSH.
Приоритет — быстро и просто, без нового сервера, отдельного MCP и
ограничений sudo на каждую команду. **Полный план и риски:** 
[NETCUP_FULL_ADMIN_ACCESS_PLAN.md](./infra/deploy/NETCUP_FULL_ADMIN_ACCESS_PLAN.md).

**Состояние:** план записан, root/sudo не настроены, production не менялся.
Живой read-only доступ GitHub → Netcup PASS [#44406398637](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44406398637);
три юнита active, наблюдавшаяся текущая VPS-версия
`85375103800845eeff696e53f510c66c428408ed`.
Следующее: один авторизованный вход в Netcup и настройка
sudo/канала команд, затем реальный административный smoke, проверенная
публикация и откат. **Перед правками** ещё раз сверить живую VPS-версию,
**перед публикацией** GitHub HEAD и VPS; не тереть чужую работу.
Изменения привилегий, секретов и рабочих данных требуют отдельного
согласия владельца. Не отправлять ключи в чат. Ключ для отката исходников
до этой записи: `7661a401f276319817901d2f34af1f2b29f3da23` (только адресный revert, не force).

**Где остановились:** `IMPLEMENTATION_ROADMAP.md` пункт **6.14 [ ] PARTIAL /
LIVE BLOCKED**. После подтверждения полного админ-доступа вернуться именно
к 6.14, провести живой GPT + Claude E2E и только после успешного теста
переходить к дальнейшим этапам.

**Фактические проверки:** live read-only PASS ранее; после этого
документального изменения тесты/деплой Netcup не проводились.

Исторические handoff и технический протокол до решения сохранены
в истории Git по коммиту `7661a401f276319817901d2f34af1f2b29f3da23` и
[старом release-протоколе](./infra/deploy/GITOPS_NETCUP_CONTROL.md).
