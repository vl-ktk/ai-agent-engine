# HANDOFF — Oracle pilot

**Задача:** по просьбе Владислава изолировать подготовку двух Oracle Always Free VM и управления MCP от параллельной разработки AI Agent Engine на Netcup.

**Дата:** 2026-11-20.
**Репозиторий:** `vl-ktk/ai-agent-engine`.
**Ветка пилота:** `infra/oracle-setup`, создана от `main` commit `d79f40d472ce8e2e0848b1b130123f8315786fb4`.
**Основной roadmap:** `IMPLEMENTATION_ROADMAP.md` на `main`; эту отдельную ветку не считать разрешением двигать этап 7.7.
**Netcup:** прошлый read-only audit, записанный в корневом `HANDOFF.md`, показывал package `0.3.11a0`, release SHA `0e04af007375d82daf8f7d3b36c71abb0c784e0b`. Это **историческая точка**, не новая live-проверка. Netcup сейчас не трогали.
**Выполнено:** branch/doc план OR-0; GitHub repo, основной HANDOFF, AGENTS и все 13 workflows проверены; `main` не меняли; OCI docs просмотрены.
**Не выполнено:** OCI read-only inventory, VM creation, SSH/OS setup, MCP wiring, второй сервер, совместимость AIE, staging, миграция.
**Следующий шаг:** OR-1 — актуально проверить Oracle Free Tier/home region/ресурсы на первом аккаунте, без изменения cloud. Попросить владельца прислать несекретные скриншоты Console либо настроить авторизованный read-only доступ.
**Проверки:** GitHub branch/doc SHA после записи, CI trigger inspection. Никаких VM tests или deployments не было.
**Откат:** новых production-изменений нет. Ветку можно оставить как черновик или позже убрать по отдельному запросу владельца; `main` не откатывать.
**Блокер:** нет прямого OCI read-only API/MCP подключения. Предыдущие скриншоты не доказывают текущее состояние облака.
