# Overlay Browser Automation MCP + Memory Wiki

## Что исправляется

1. **P0 CDP dispatcher:** ответ на один CDP-запрос больше не отклоняет все остальные pending-запросы.
2. **Reconnect:** временный разрыв, переподключение и окончательное закрытие разделены; менеджер не создаёт конкурирующий WebSocket во время reconnect.
3. **Memory Wiki:** удаляется фиктивный HTTP-контракт `/api/claims`; Browser MCP вызывает существующий `hermes-memory-wiki/mcp-wrapper/server.py` по stdio MCP.
4. **Секреты:** перед записью страницы в Memory Wiki удаляются cookies, authorization/token/password-поля, JWT/Bearer-подобные значения и большие base64/body-поля.
5. **Артефакты:** один `close(fd)`, `O_NOFOLLOW`, проверка regular file, полный цикл записи и `fsync`.
6. **Cookie:** `SameSite=None` и partitioned cookie требуют `secure=true`; cookie URL проходит ту же сетевую проверку.
7. **SSRF allowlist:** allowlist больше не обходит проверку loopback/private/link-local адресов.
8. **Привилегированные инструменты:** `browser_exec` и `browser_cdp` скрыты и блокируются по умолчанию. Включение только явно:

```bash
export BROWSER_ALLOW_EXEC=1
export BROWSER_ALLOW_RAW_CDP=1
```

9. **Пароль:** deprecated plaintext `username/password` убираются из публичной schema `browser_login`; используется `credential_ref`.

## Применение

```bash
unzip browser-memory-wiki-overlay-20260725.zip
cd browser-memory-wiki-overlay-20260725
./install.sh /путь/browser-automation-mcp /путь/hermes-memory-wiki
```

Проверка без изменения файлов:

```bash
python3 apply_overlay.py \
  --browser-repo /путь/browser-automation-mcp \
  --memory-repo /путь/hermes-memory-wiki \
  --check
```

Откат:

```bash
./rollback.sh /путь/browser-automation-mcp
```

## Важно

- `hermes-memory-wiki/__init__.py` **не дробится и не изменяется**.
- Перед изменением создаётся резервная копия `browser-automation-mcp/.overlay-backups/<timestamp>/server.py`.
- Overlay рассчитан на структуру `browser-automation-mcp` v1.5.1. При несовпадении якорей он завершится ошибкой и не запишет частично изменённый файл.
- После применения перезапустите Hermes и MCP-процессы.
