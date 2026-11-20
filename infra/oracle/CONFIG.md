# Oracle pilot — несекретная конфигурация и неизвестные

Этот файл хранит только безопасную для **публичного репозитория** информацию; не является рабочим `.env`.

## Идентификаторы

| Параметр | Значение / проверка |
| --- | --- |
| Project | AI Agent Engine, Oracle pilot |
| Branch | `infra/oracle-setup` |
| Account 1 | `oci-primary` — псевдоним; реальный tenancy/account не публиковать |
| Account 1 Console region | Germany Central (Frankfurt), **по скриншоту**, но home region не подтверждён |
| Account 1 home region | UNKNOWN — проверить OR-1 |
| Account 1 Free Trial / Always Free | UNKNOWN сейчас — проверить OR-1 |
| Account 1 VM | NOT VERIFIED / по прежнему скриншоту отсутствовал в выбранном compartment |
| Account 2 | `oci-secondary` — псевдоним, tenancy и home region ещё не проверены |
| OS / ARM image | TBD после совместимости и Free Eligible проверки |
| VM1 shape / OCPU / RAM / boot GB | TBD, строго Always Free |
| VM2 shape / OCPU / RAM / boot GB | TBD, строго Always Free |
| SSH key storage / OCI credentials | Только защищённое хранилище с отдельным разрешением, **никогда GitHub** |
| One-IP management | TBD: защищённый HTTPS/MCP gateway, не NAT-объединение VM |
| Netcup / AIE source | GitHub `main`; **ничего не менять** |

## Секреты и платные функции

Не добавлять сюда:
- почты и имена владельцев tenancy, Tenant OCID, API key, fingerprint, password, passkey, FIDO recovery;
- private SSH keys, JWT, токены, файлы `~/.oci/config`, cloud-init с учётными данными;
- публичные IP, домены управления и credential-bearing URLs до отдельного согласования раскрытия.

Любой платный ресурс = STOP и запрос нового решения. Документ не предоставляет IAM, root или финансовых полномочий.
