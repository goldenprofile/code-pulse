# Code Pulse — план релиза v0.2

## 1. Статус, цель, объём

**Статус:** база — рабочий v0.1 (коммит `132fb39`): дашборд на stdlib Python 3.11+, vanilla JS без сборки, SQLite (WAL + RLock), ноль зависимостей. Рабочее дерево чистое (только отчёты разведки `.scout-*.md` не закоммичены).

**Цель:** добавить три фичи без нарушения ограничений (только stdlib, без npm/сборки):
- **F1 — живое автообновление (SSE):** `GET /api/events`, события `scan_started / scan_progress / scan_done / project_updated`, фронт на `EventSource` с фолбэком-поллингом, индикатор живого статуса в шапке.
- **F2 — алерты деградации:** дельта скоринга за 7 дней (`delta7`), порог в конфиге, бейдж «▼N» на карточке и в шапке проекта, фильтр «Проблемные».
- **F3 — экспорт отчёта по проекту:** `GET /api/report?name=&days=30` → скачиваемый markdown; кнопка «Экспорт .md» в шапке проекта.

**Исполнение:** строго последовательное — Фаза 0 → Фаза 1 (worker-1, F1) → Фаза 2 (worker-2, F2+F3) → Фаза 3 (приёмка). Параллелить worker'ов нельзя: оба правят `server.py` (do_GET) и `web/app.js` (DOMContentLoaded).

---

## 2. Фазы

### Фаза 0 — базлайн (перед любыми правками)

- [ ] Зафиксировать чистое дерево: `git status --short` → только `.scout-*.md` (untracked), `git log --oneline -1` → `132fb39`.
- [ ] Статическая проверка ДО правок: `python -c "import ast,pathlib; [ast.parse(p.read_text(encoding='utf-8'), str(p)) for p in pathlib.Path('.').rglob('*.py') if '__pycache__' not in p.parts]"` → exit 0.
- [ ] `node --check web/app.js` → exit 0.
- [ ] Сервер стартует на текущем коде: `python run.py --no-backfill`, затем `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/api/overview` → `200`.
- **Критерий приёмки фазы:** все четыре команды дают ожидаемый результат; это эталон для сравнения после правок.

### Фаза 1 — F1 «Живое автообновление» (worker-1)

Ключевое проектное решение: `pulse/scanner.py` **не трогаем**. `scan_started/scan_done/project_updated` эмитятся из `run_refresh`/`run_backfill` (`server.py:172–199`), а `scan_progress` ретранслирует сам SSE-хендлер, раз в ~1с читая `app.status.snapshot()` (`pulse/scanner.py:282–305`) — так прогресс доезжает и от сканов, запущенных `run.py:100–101` при старте сервера.

- [ ] `server.py`, `App.__init__` (:20): реестр подписчиков — `self._subs: list[queue.Queue] = []` + `self._subs_lock = threading.Lock()`; импорт `queue`.
- [ ] Методы `App`: `subscribe() -> queue.Queue(maxsize=100)`, `unsubscribe(q)`, `emit(event, data)` — под локом итерируем копию списка, `q.put_nowait(...)`, при `queue.Full` — молча дропаем событие (медленный клиент не должен блокировать поток скана); при пустом списке подписчиков — мгновенный выход (дёшево для фонового скана из `run.py`).
- [ ] Эмиссия в `run_refresh` (:172): на входе `emit("scan_started", {...})`; в `finally` (:186–189) после `invalidate()` — `emit("scan_done", {"status": self.status.snapshot()})` и `emit("project_updated", {"names": sorted(names) if names else []})`. То же в `run_backfill` (:191–199) с `"phase": "backfill"`.
- [ ] Роут в `do_GET` (:264): `elif route == "/api/events": self._events()` — **строго до** ветки `/api/*` → 404 (:283). Метод `_events()`:
  - заголовки: `send_response(200)`, `Content-Type: text/event-stream`, `Cache-Control: no-store`, `end_headers()`;
  - `q = app.subscribe()`, телом цикла — `try/finally: app.unsubscribe(q)`;
  - первое сообщение: `retry: 3000\n\n` (авто-reconnect EventSource);
  - цикл: `q.get(timeout=1)` → `event: <имя>\ndata: <json>\n\n` + `flush()`; по таймауту — если `app.status.snapshot()` изменился или `phase != "idle"` → послать `scan_progress {phase, done, total, current}`; если 15с полной тишины — `: ping\n\n` (keep-alive + детект мёртвого сокета);
  - на `(BrokenPipeError, ConnectionResetError, OSError)` — выйти (finally уберёт подписчика).
- [ ] `Handler.protocol_version = "HTTP/1.1"` — безопасно: `_json` (:318–325) и `_static` (:327–346) уже шлют `Content-Length`. Если регресс покажет артефакты — откатить (SSE работает и на HTTP/1.0 как стрим без длины).
- [ ] `web/index.html`: в шапку рядом с `.brand` — `<span id="liveDot" class="dot-live"></span><span id="liveText" class="muted"></span>`. Кнопку ⟳ (`#btnRefresh`, :24) НЕ трогаем, новую не добавляем.
- [ ] `web/app.js`: функции `connectEvents()` и `updateLiveIndicator(st)`:
  - `new EventSource("/api/events")`; `scan_started`/`scan_progress` → индикатор: `сканирование ${done}/${total}`; `scan_done` → индикатор idle + `loadOverview()` + если `state.selected` — перезагрузить открытый проект (`api("/api/project?name=")` + `renderProject`); `project_updated` → `loadOverview()`;
  - `es.onerror` → `es.close()` и фолбэк: `setInterval` 30с `GET /api/status`; переход busy→idle → `loadOverview()` (+проект). Второй постоянный поллинг данных не заводить — только этот фолбэк;
  - вызов `connectEvents()` в блоке DOMContentLoaded (:671+); существующий `pollStatus` (:84–103) и его использование кнопками (:680–692) не менять (двойной `loadOverview` после scan_done безвреден — GET идемпотентен).
- [ ] `web/styles.css`: `.dot-live` (зелёная точка 8–10px, `border-radius:50%`), `.dot-live.busy` (янтарная, `@keyframes` пульсация).
- **Критерии приёмки фазы:**
  - `python -c "import ast,pathlib; ..."` (как в Фазе 0) → exit 0; `node --check web/app.js` → exit 0.
  - `curl -si -N --max-time 3 http://127.0.0.1:8787/api/events | head -6` → `Content-Type: text/event-stream`, тело начинается с `retry: 3000`.
  - Терминал A: `curl -N http://127.0.0.1:8787/api/events`; терминал B (когда `curl /api/status` показывает `phase: idle`): `curl -X POST http://127.0.0.1:8787/api/refresh` → в A появляются `event: scan_started`, затем `event: scan_progress`, в конце `event: scan_done`.
  - Обрыв: `curl --max-time 2 http://127.0.0.1:8787/api/events` (5 циклов с POST /api/refresh между ними) → после каждого цикла `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/api/overview` → `200` (подписчик удалён в finally, сервер не течёт и не падает).
  - Регресс старых GET после смены protocol_version: `curl -s http://127.0.0.1:8787/api/overview | head -c 80` — валидный JSON; `/api/status`, `/api/settings`, `/api/graph` → 200.
  - Руками: открытый дашборд обновляет карточки сам после `POST /api/refresh` без F5; при остановленном сервере в DevTools виден 30-секундный фолбэк-поллинг `/api/status`.

### Фаза 2 — F2 «Алерты деградации» + F3 «Экспорт .md» (worker-2; старт после приёмки Фазы 1)

**F2:**
- [ ] `pulse/store.py`: хелпер `snapshots_since(project, since_ts) -> list[dict]` — как `snapshots()` (:110–128), но `AND ts >= ? ORDER BY ts`, тот же JSON-разбор, строго под `with self._lock`.
- [ ] `run.py`: в `DEFAULT_CONFIG` (:19–27) добавить `"alerts": {"score_drop_7d": 5}`; при создании `App` (:78) передать порог: `App(root, store, status, alert_drop=cfg.get("alerts", {}).get("score_drop_7d", 5))`.
- [ ] `server.py`: в `App.__init__` (:20) новый параметр `alert_drop: float = 5.0`; в `overview()` (:32–66) рядом с `delta30` (:47–55) посчитать `delta7`: свежие = снапшоты с `ts >= now - 7*86400`; `delta7 = round(последний.score - первый_свежий.score, 1)` при ≥2 снапшотах и обоих score не None, иначе `None`. В payload проекта — только новые поля: `"delta7": delta7`, `"alert": delta7 is not None and delta7 <= -self.alert_drop`. Ничего не переименовывать.
- [ ] `server.py`, `project_detail()` (:68–122): те же `delta7`/`alert` в ответ `/api/project` — иначе шапке проекта (`renderProject`) неоткуда их взять.
- [ ] `web/app.js`, `cardHTML` (:139–172): рядом с `deltaHTML` (:145–147) — при `p.alert` бейдж `<span class="badge alert" title="падение за 7 дней">▼ ${Math.abs(p.delta7)}</span>`; в `renderProject` (:215–222) — тот же бейдж в `#projectHeader` (имя — через `esc()`, как заведено в остальном шаблоне).
- [ ] `web/index.html`, `#filters` (:41–55): `<select id="alertFilter"><option value="">все</option><option value="alerts">⚠ проблемные</option></select>`.
- [ ] `web/app.js`, `renderCards` (:174–199): читать `$("#alertFilter").value`; в фильтре (:177–191) добавить `if (af === "alerts" && !p.alert) return false;`; binding в DOMContentLoaded рядом с `#gitFilter` (:676–678).
- [ ] `web/styles.css`: рядом с `.delta-down` (:88–89) — `.badge.alert { color: var(--red); border-color: var(--red); }`.

**F3:**
- [ ] `server.py`, роут в `do_GET` до 404-ветки: `elif route == "/api/report": self._report(parse_qs(parsed.query))`; метод `_report(qs)`:
  - `name = qs.get("name",[""])[0]`; `days = int(qs.get("days",["30"])[0])`, clamp 1..365; `detail = app.project_detail(name)` → 404 `{"error":"not found"}` при None (симметрично `/api/project`);
  - markdown-секции: заголовок (имя/путь/стек/дата/«период: N дней»); итог: скор + `delta30` + `delta7`; подындексы (mi, cog, dup, arch, test, todo из последней `subs`); топ-10 хотспотов; таблица пакетов Ca/Ce/I/A/D (+циклы, нарушения); возраст кода (бакеты `evo.age.buckets` + доля застоя); незакоммиченные файлы `live.dirty_files`;
  - хотспоты за `days`: при `days == 30` взять из `detail.evo`, иначе пересчитать на лету `commit_log_numstat(path, days)` + `gitstats.hotspots(...)` (без кэша; данные из БД вычитать до тяжёлых вызовов git — не держать RLock);
  - хедеры: `Content-Type: text/markdown; charset=utf-8`, `Content-Disposition: attachment; filename="code-pulse-<slug>-<days>d.md"`, где `slug = re.sub(r"[^A-Za-z0-9._-]", "_", name)` (защита от CRLF/кавычек/не-ASCII в header), `Content-Length` обязателен (HTTP/1.1).
- [ ] `web/app.js`, `renderProject`: в шаблон `#projectHeader` добавить `<a class="btn-export" href="/api/report?name=${encodeURIComponent(d.name)}&days=30" download>Экспорт .md</a>` — обычная ссылка, НЕ fetch.
- [ ] `web/styles.css`: `.btn-export` — стилизовать как кнопки шапки (`.actions button`, :38).
- **Критерии приёмки фазы:**
  - Статика: ast-парс всех `*.py` → exit 0; `node --check web/app.js` → exit 0.
  - Хелпер store: на временной БД (`Store(Path(tempdir)/"t.db")`) сохранить снапшоты с `ts=now` и `ts=now-8*86400`; `snapshots_since(p, now-7*86400)` → ровно 1 запись.
  - Порог: мини-скрипт с временной БД — `Store(tmp)` + `App(root=tmpdir, store, scanner.Status())`, два снапшота (ts=now-6д score 80, ts=now score 70) → `overview()["projects"][0]["delta7"] == -10.0` и `"alert" is True`.
  - Payload: `curl -s http://127.0.0.1:8787/api/overview | python -c "import json,sys; d=json.load(sys.stdin); ps=d['projects']; assert all('delta7' in p and 'alert' in p for p in ps); p=ps[0]; assert all(k in p for k in ('name','score','delta30','series','live'))"` (старые ключи на месте — обратная совместимость).
  - Конфиг: без ключа `alerts` в `config.json` сервер стартует (порог 5 из DEFAULT_CONFIG); с `"alerts": {"score_drop_7d": 3}` порог меняется (проверить тем же мини-скриптом).
  - Отчёт: `curl -si "http://127.0.0.1:8787/api/report?name=<реальный>&days=30" | head -8` → `Content-Type: text/markdown`, `Content-Disposition: attachment`; тело содержит `# Code Pulse`, `## Хотспоты`, `## Пакеты`, `## Незакоммиченные`; `curl -si ".../api/report?name=нет_такого" | head -1` → `404`; `days=7` → в теле строка «период: 7 дней».
  - Руками: фильтр «проблемные» показывает подмножество карточек с красными бейджами ▼N; кнопка «Экспорт .md» скачивает валидный .md.

### Фаза 3 — приёмка релиза

См. раздел 5. Входит: smoke-прогон `python run.py --no-backfill`, регресс всех старых эндпоинтов, `git diff --stat` (только ожидаемые файлы), ревью диффа на предмет троганья запретных зон из раздела 3.

---

## 3. Разбивка по worker'ам (строго последовательно)

### Worker-1 = F1 (СЕЙЧАС)

**Правки по файлам:**

| Файл | Что |
|---|---|
| `server.py` | `import queue`; реестр `_subs`+lock в `App.__init__` (:20); `subscribe/unsubscribe/emit`; эмиссия в `run_refresh` (:172–189) и `run_backfill` (:191–199); роут `/api/events` в `do_GET` (:264–289); метод `_events()`; `Handler.protocol_version = "HTTP/1.1"` |
| `web/app.js` | `connectEvents()`, `updateLiveIndicator(st)`, фолбэк-поллинг 30с; вызов в DOMContentLoaded (:671+) |
| `web/index.html` | `#liveDot` + `#liveText` в шапке (:17–22) |
| `web/styles.css` | `.dot-live`, `.dot-live.busy`, keyframes пульса |

**Критерии приёмки** — см. конец Фазы 1 (curl `text/event-stream` + событие `scan_done` после `POST /api/refresh`; 5 циклов обрыва → сервер жив; ast-парс + `node --check`; регресс старых GET).

**НЕ трогать:** `pulse/` целиком (scanner.py, store.py, score.py), `run.py`, `web/chart.umd.min.js`; POST-ветки `do_POST` и busy-guard (:291–316); `cardHTML`/`renderCards`/`renderProject` (:139–224); существующие обработчики кнопок (:680–692); `pollStatus` (:84–103).

### Worker-2 = F2 + F3 (ПОСЛЕ мержа и приёмки worker-1)

**Правки по файлам:**

| Файл | Что |
|---|---|
| `pulse/store.py` | `snapshots_since(project, since_ts)` под RLock |
| `server.py` | `alert_drop` в `App.__init__`; `delta7`/`alert` в `overview()` и `project_detail()`; роут `/api/report` + `_report()` |
| `run.py` | `"alerts"` в DEFAULT_CONFIG; передача `alert_drop` в `App` (:78) |
| `web/app.js` | бейдж ▼N в `cardHTML` и `renderProject`; фильтр в `renderCards` + binding; кнопка «Экспорт .md» в `renderProject` |
| `web/index.html` | `#alertFilter` в `#filters` (:41–55) |
| `web/styles.css` | `.badge.alert` (рядом с :88–89), `.btn-export` |

**Критерии приёмки** — см. конец Фазы 2 (мини-скрипт `delta7 == -10.0` / `alert is True`; assert новых И старых полей в payload; curl отчёта с хедерами и 404; ast-парс + `node --check`).

**НЕ трогать:** написанный worker-1 SSE-код (`_subs`, `emit`, `_events`, `connectEvents`) — только не конфликтовать строками; `pulse/scanner.py`, `pulse/score.py`; сигнатуры/имена существующих полей payload (`delta30`, `series`, `totals`, `coupling` — только добавлять рядом); расчёт `score` в `pulse/score.py`; POST `/api/settings/exclude` и `/api/backfill`.

---

## 4. Риски и как проверять

| Риск | Суть | Проверка / защита |
|---|---|---|
| SSE + ThreadingHTTPServer | Поток на соединение: каждая вкладка = поток; мёртвый клиент обнаруживается только при записи; медленный клиент может переполнить очередь | `finally: unsubscribe(q)`; `put_nowait` + дроп при `queue.Full` (скан не блокируется); ping каждые 15с. Тест: 5 циклов `curl --max-time 2 /api/events` + POST /api/refresh → `/api/overview` каждый раз 200 |
| Смена `protocol_version` на HTTP/1.1 | keep-alive ломает ответы без `Content-Length` | Все текущие ответы уже шлют длину (`_json` :318–325, `_static` :327–346). Регресс: curl-цикл по всем GET после правки; при артефактах — откатить (SSE ок и на HTTP/1.0) |
| Store: RLock + WAL, один conn | Новые чтения мимо лока → гонки; тяжёлый gitstats под локом → фризы | `snapshots_since` только под `with self._lock`; в `_report()` данные из БД вычитать ДО вызовов gitstats (`snapshots()/live_all()` возвращают копии). Проверка: тесты Фазы 2 на временной БД + сервер отзывчив при открытом отчёте |
| XSS / инъекции | Рендер через `innerHTML`; имя проекта в URL и в `Content-Disposition` | Все новые вставки через `esc()`; `encodeURIComponent(d.name)` в href; `re.sub(r"[^A-Za-z0-9._-]", "_", name)` для filename (CRLF/кавычки). Проверка: `node --check` + grep, что новые `${}` в шаблонах обёрнуты в `esc()` |
| Обратная совместимость payload | Старые скрипты/клиенты читают `/api/overview` | Только аддитивные поля (`delta7`, `alert`); ничего не переименовывать/удалять. Проверка: assert старых ключей в критериях Фазы 2 |
| Конфиг без нового ключа | `config.json` пользователей не содержит `alerts` | `load_config()` мержит поверх DEFAULT_CONFIG (`run.py:20–40`) — ключ опционален. Проверка: старт без ключа в конфиге |
| Дубли перезагрузок UI | pollStatus кнопок + SSE scan_done → двойной loadOverview | Безвредно (идемпотентный GET), но не заводить второй постоянный поллинг данных — только фолбэк при падении EventSource. Проверка: в DevTools Network нет двух периодических циклов |
| busy-guard POST | POST /api/refresh во время скана → `{"ok":false}` (:293–295) — смоук ложно «падает» | POST в проверках делать только при `phase == "idle"` (предварительно `curl /api/status`) |

`ASSUMPTION:` стриминг SSE через `BaseHTTPRequestHandler.wfile` (небуферизованный, `flush()` — no-op) работает как есть — подтверждается curl-критерием Фазы 1; если нет, добавить явный flush-цикл на месте.

---

## 5. Проверка релиза

1. **Статика:** `python -c "import ast,pathlib; [ast.parse(p.read_text(encoding='utf-8'), str(p)) for p in pathlib.Path('.').rglob('*.py') if '__pycache__' not in p.parts]"` → exit 0; `node --check web/app.js` → exit 0.
2. **Smoke:** `python run.py --no-backfill` (на существующей БД `data/code-pulse.db`) → в консоли `✦ Code Pulse → http://127.0.0.1:8787`; дождаться фонового скана → `curl -s http://127.0.0.1:8787/api/status` → `phase: idle`.
3. **Ручной smoke UI:** открыть `/` — карточки отрисованы; зелёная точка в шапке; вкладка «Граф качества» рисует симуляцию; открыть проект — графики, таблицы, кнопка «Экспорт .md» скачивает валидный .md; ⚙ настройки открываются/закрываются; нажать ⟳ → прогресс-бар, текст «сканирование N/M» у точки в шапке, после idle карточки обновились БЕЗ F5.
4. **F1 по curl:** `curl -N /api/events` в фоне + `POST /api/refresh` (при idle) → события `scan_started`…`scan_done` получены (см. Фазу 1).
5. **F2:** `curl /api/overview` → у всех проектов есть `delta7`/`alert`; фильтр «проблемные» в UI даёт подмножество; мини-скрипт с временной БД даёт `delta7 == -10.0`, `alert is True`.
6. **F3:** `curl -si "/api/report?name=<реальный>&days=30"` → `text/markdown` + `Content-Disposition: attachment`; неизвестное имя → 404; `days=7` → «период: 7 дней».
7. **Регресс старых эндпоинтов:** `curl -s -o /dev/null -w "%{http_code}\n"` по очереди: `/api/overview`, `/api/project?name=<реальный>`, `/api/graph`, `/api/status`, `/api/settings`, `/` (статика) → все `200`; `/api/несуществующее` → `404`; `/api/report?name=нет` → `404`.
8. **Дифф-гигиена:** `git diff --stat` → только `server.py`, `run.py`, `pulse/store.py`, `web/app.js`, `web/index.html`, `web/styles.css` (+ новый `docs/PLAN-v0.2.md`); `pulse/scanner.py`, `pulse/score.py`, `web/chart.umd.min.js` не изменены.
