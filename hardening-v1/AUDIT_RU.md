# Аудит browser-automation-mcp + hermes-memory-wiki

## Критические находки

1. **Связь с Memory Wiki фактически не работает.** `_persist_to_wiki()` и `_recall_from_wiki()` являются заглушками, возвращающими `None`. Из-за этого `persist_to_wiki=true` и `browser_recall` не обеспечивают заявленный жизненный цикл.
2. **Cookie-защита не является границей безопасности.** Cookie API редактирует значения по умолчанию, однако универсальный `browser_exec` способен обращаться к `document.cookie`, `localStorage`, `sessionStorage` и DOM-полям. Без единой проверки на уровне диспетчера typed-tool ограничения обходятся.
3. **`browser_login` противоречит собственному описанию.** Схема всё ещё принимает `username` и `password` как MCP-аргументы, хотя описание обещает пароль вне аргументов.
4. **Raw-cookie режим слишком легко расширяется до всего browser context.** Список контекстных cookies может охватывать несколько сайтов профиля. Нужны одновременно операторский env-флаг, подтверждение и domain allowlist.
5. **Нет строгой валидации cookie invariants.** Нужны проверки `SameSite=None => Secure`, `__Host-`, `__Secure-`, размера, URL/domain и partition key.
6. **CDP — привилегированный канал.** Доступ к remote-debugging endpoint фактически означает управление вкладками, сетевыми запросами и browser storage. Endpoint нельзя публиковать наружу без отдельного аутентифицирующего reverse proxy.
7. **Слишком широкая поверхность из 40 инструментов.** Это увеличивает tools/list и риск неверного выбора. Дальнейшая оптимизация: capability profiles без удаления инструментов из кода, lazy schema exposure и компактный accessibility snapshot.

## Что исправляет пакет

- Добавляет fail-closed policy перед исполнением sensitive tools, не удаляя ни один MCP tool.
- Закрывает обход Cookie/storage через `browser_exec`, пока оператор явно не включил чувствительный режим.
- Отключает plaintext login arguments по умолчанию.
- Валидирует Cookie invariants и destructive clear.
- Принудительно редактирует sensitive responses и пишет redacted audit JSONL.
- Реализует durable filesystem bridge Browser → Memory Wiki → browser_recall.
- Проверяет hash, размер, symlink и схему bridge event.
- Сохраняет `hermes-memory-wiki/__init__.py` единым файлом.

## Что сознательно не делает пакет

- Не хранит raw cookies, passwords, authorization headers или web-storage в Memory Wiki.
- Не превращает Memory Wiki в HTTP-сервис.
- Не открывает CDP наружу и не считает `BROWSER_AUTH_TOKEN` защитой самого Chrome без proxy.
- Не обещает обход CAPTCHA, WebAuthn, device binding, anti-bot или MFA.
- Не гарантирует перенос сессии между несовместимыми Chrome-профилями/устройствами.

## Рекомендуемые следующие функции

- `browser_context_create/dispose` для настоящих изолированных контекстов.
- Storage-state export/import с envelope encryption и TTL.
- Download manager, dialog/permission handler, iframe/shadow-root aware locators.
- Accessibility snapshot со стабильными refs и snapshot diff.
- Domain-scoped policy profiles и per-tool rate limits.
- Typed browser evidence claim в Memory Wiki с provenance и freshness TTL.
