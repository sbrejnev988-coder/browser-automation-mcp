# Hermes Browser + Memory Wiki hardening patch v1

Пакет предназначен для актуальных веток:

- `browser-automation-mcp`, где `server.py` содержит `_persist_to_wiki`, `_recall_from_wiki`, `browser_cookie_list`, `browser_exec`;
- `hermes-memory-wiki`, где монолитный `__init__.py` содержит `MemoryWikiProvider` и `_ingest_text`.

## Установка

```bash
unzip hermes-browser-memory-hardening-v1.zip
cd hermes-browser-memory-hardening-v1

./install.sh \
  /путь/browser-automation-mcp \
  /путь/hermes-memory-wiki
```

Либо напрямую:

```bash
python3 apply_patch.py \
  --browser-root /путь/browser-automation-mcp \
  --memory-root /путь/hermes-memory-wiki
```

Установщик:

1. проверяет опорные символы текущего кода;
2. создаёт timestamped backup обоих основных файлов;
3. копирует два zero-dependency overlay-модуля;
4. добавляет короткие install-блоки в `server.py` и в конец `__init__.py`;
5. запускает `py_compile`;
6. автоматически откатывает файлы при ошибке синтаксиса.

`hermes-memory-wiki/__init__.py` остаётся одним файлом. Никакие классы или функции из него не выносятся.

## Проверка

```bash
python3 verify_patch.py \
  --browser-root /путь/browser-automation-mcp \
  --memory-root /путь/hermes-memory-wiki

python3 -m unittest discover -s tests -v
```

## Безопасные настройки по умолчанию

Все raw cookies/storage, sensitive `browser_exec` и plaintext login отключены. Для редкого ручного доступа одновременно нужны env-флаг и явное подтверждение в tool arguments. См. `env.example`.

## Memory bridge

Browser создаёт redacted capsule в:

```text
$HERMES_HOME/memory-wiki/browser_bridge/inbox/
```

Memory Wiki забирает capsule при `initialize`, `prefetch` или `sync_turn`, прогоняет через собственный `_ingest_text`, перемещает событие в `processed/` и добавляет безопасную запись в `index.jsonl`. `browser_recall` читает только этот redacted index.

## Откат

```bash
python3 uninstall_patch.py \
  --browser-root /путь/browser-automation-mcp \
  --memory-root /путь/hermes-memory-wiki
```

Timestamped backups сохраняются рядом с исходными файлами.
