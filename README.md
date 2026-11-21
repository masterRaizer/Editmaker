# EditMaker

Локальная утилита для аниме- и dark-эдитов: исходники с SSD → 4 варианта монтажа под бит → водяной знак в ролике и в конце → ручное одобрение → публикация на YouTube и TikTok.

## Windows: .exe и .msi

Сборки делает GitHub Actions (`.github/workflows/windows-build.yml`) на каждый push:

- `EditMaker-<версия>-x64.msi` — установка в `C:\Program Files\EditMaker`, ярлыки в «Пуске» и на рабочем столе;
- `EditMaker-<версия>-portable.exe` — один файл без установки.

ffmpeg 7.1 с NVENC уже внутри. Окно приложения работает на WebView2 (есть в Windows 10/11); без него интерфейс откроется в браузере. Данные — `%APPDATA%\EditMaker\data`, ключи — `%APPDATA%\EditMaker\secrets`, музыка — `%USERPROFILE%\Music\EditMaker\music_library`, вывод по умолчанию — `%USERPROFILE%\Videos\EditMaker`.

Скачать: Actions → «Windows build (exe + msi)» → последний зелёный запуск → Artifacts. Для тега `v*` файлы публикуются в Releases. Пошаговая установка и проверка на ноутбуке — [docs/LAPTOP_AGENT_PROMPT.md](docs/LAPTOP_AGENT_PROMPT.md).

Консольные команды есть и в собранной программе: `EditMaker.exe check`, `EditMaker.exe selftest <папка>`; вывод пишется в `%APPDATA%\EditMaker\data\cli.log`.

## Запуск из исходников

```bash
pip install -r requirements.txt      # Python 3.11+, ffmpeg и ffprobe в PATH
python run.py check                  # ffmpeg, ffprobe, NVENC/GPU
python run.py                        # UI в браузере: http://127.0.0.1:8765
python run.py --desktop              # UI в отдельном окне (pip install pywebview)
python run.py selftest output/test   # синтетический клип + бит → 4 варианта без UI
python -m pytest -q                  # тесты
```

Сервер слушает только `127.0.0.1`. Настройки — `data/config.json`, база — `data/editmaker.db`, ключи API — `secrets/` (в `.gitignore`).

## Работа в интерфейсе

Левая панель:

1. **Исходники** — «Добавить папку» открывает обзор дисков (на Windows — буквы дисков, на Linux/macOS — `/`, `/media`, `/mnt`, `/Volumes`). «Сканировать» индексирует `.mp4 .mkv .webm .mov`: разрешение, FPS, длительность, кодек, название и серия из имени файла, монтажные склейки и динамика кадра.
2. **Папка вывода** — любая папка на любом диске. Результат: `<папка>/<проект>/variant_01.mp4 … variant_04.mp4`, превью `.jpg` и `meta.json`.
3. **Музыка** — вайб и трек. «Автоподбор по темпу видео» выбирает трек из вайба по BPM и динамике исходника. Без треков используется звук исходника с `loudnorm` до −14 LUFS.
4. **Водяной знак** — текст или PNG с прозрачностью, позиция, цвет, прозрачность, размер. Флажки «В ролике» и «В конце ролика» независимы, по умолчанию включены оба. «Плавный сдвиг» медленно смещает знак, чтобы его не срезал кроп.
5. **Формат** — 1080×1920 / 1920×1080 / 4K, 60 или 30 fps, длина, число вариантов, качество, кодек.

Центр: вкладки вариантов со статусом (`draft`, `rendered`, `approved`, `published`, `failed`), плеер, полоса битов (серые — доли, высокие — сильные доли, красные — склейки, серый блок — концовка), тайминги, BPM, музыка, водяной знак, файл. «Ещё 2 варианта» добавляет варианты с новым сидом.

Под плеером: щелчок по полосе битов — перемотка, ←/→ — к предыдущей или следующей склейке, 1–9 — вариант, пробел — пауза. «Сравнить два варианта» ставит второй плеер рядом, он повторяет паузу, воспроизведение и перемотку первого.

Правая панель: заголовок, описание, теги для YouTube; подпись, описание, хештеги, режим и видимость для TikTok; приписка; выбор платформ. «Одобрить» фиксирует хеш файла и текста. «Опубликовать одобренное» доступно только после одобрения.

## Доработка в редакторе

Рендер делает ffmpeg без ручной работы. Чтобы довести эдит руками, у каждого варианта есть экспорт в `<проект>/export/variant_XX/`:

| Кнопка | Что получится | Где открыть |
|---|---|---|
| DaVinci Resolve / Final Cut | `.fcpxml`: клипы встык по склейкам, музыка с метками долей, `beats.txt` | Resolve: File → Import → Timeline; Final Cut: File → Import → XML |
| Premiere (EDL) | `.edl` CMX3600: видео и музыка, `beats.txt` | Premiere: File → Import; Resolve, Vegas, Avid |
| Пакет для CapCut | клипы `01_…mp4` уже обрезаны, кадрированы и с грейдом, `music.wav`, `watermark.png`, `beats.txt`, инструкция | CapCut: импорт файлов по порядку имён |

У CapCut нет импорта таймлайна, поэтому для него — пакет готовых нарезок. Переходы и эффекты в экспорт не входят, их добавляют в редакторе.

## Варианты монтажа

| Вариант | Склейки | Переходы и эффекты |
|---|---|---|
| 01 Cold / Dexter Pace | 2–8 долей | в основном жёсткие склейки, fadeblack, медленный наезд, холодный грейд |
| 02 High-Impact Beat / Drop | 1–2 доли | slide/whip, zoomin, вспышки на сильных долях (не чаще 0.45 с), shake на басу, punch-zoom |
| 03 Smooth / Flow | 4–8 долей | fade, smooth, circleopen, hblur по 0.4–0.5 с, плавный зум туда-обратно |
| 04 Fast Hook / Retention Cut | полдоли в первые ~1.2 с, затем 1–2 доли | самый динамичный план первым, частые смены |

Переходы центрируются на долях. BPM и доли считаются на numpy (spectral flux + автокорреляция + DP-трекер), сильные доли и бас — отдельно.

Кодирование: `libx264 -crf 16 -preset slow -pix_fmt yuv420p -movflags +faststart` или `h264_nvenc -cq 18 -preset p6`, если NVENC реально работает; звук `aac 320k 48 kHz`, `loudnorm I=-14`. «Черновик» — `crf 20 veryfast` для быстрой проверки.

## Правило одобрения

- Публикация проходит через одну проверку `ensure_publishable`: статус `approved` и совпадение хеша файла и метаданных с моментом одобрения.
- Правка текста после одобрения снимает одобрение. Перерендер снимает одобрение. Изменённый на диске файл блокирует публикацию.
- API отвечает `409 approval_required` на попытку публикации без одобрения. Автопубликации нет.
- Все действия пишутся в таблицу `audit`, статусы и ссылки — в `meta.json`.

## Ключи YouTube

1. Google Cloud Console → новый проект → включить **YouTube Data API v3**.
2. OAuth consent screen → добавить свой аккаунт в Test users.
3. Credentials → OAuth client ID → тип **Desktop app** → скачать JSON.
4. Сохранить как `secrets/youtube_client_secret.json`.
5. В UI: «Аккаунты» → «Подключить YouTube». Токен сохранится в `secrets/youtube_token.json` (scope `youtube.upload`).

Загрузка — resumable upload, доступ `private` / `unlisted` / `public`.

## Ключи TikTok

1. developers.tiktok.com → приложение → продукты **Login Kit** и **Content Posting API**, scopes `video.upload`, `video.publish`.
2. Зарегистрировать redirect URI (TikTok принимает только https). Для локальной работы: после входа скопируйте адрес, на который вернул TikTok, в поле «Код TikTok» в UI.
3. Сохранить `secrets/tiktok_app.json`: `{"client_key": "...", "client_secret": "..."}`.
4. Режим «Черновик в TikTok» отправляет видео во входящие приложения TikTok. «Опубликовать сразу» до аудита приложения TikTok разрешает только видимость «Только я».

## Структура

```
run.py                      запуск из исходников (браузер, --desktop, check, selftest)
src/cli.py                  команды check / selftest / serve
src/desktop.py              окно pywebview + локальный сервер, родные диалоги Windows
src/server.py               FastAPI: API + статика
src/core/config.py          настройки, вайбы, разрешения
src/core/db.py              SQLite: sources, tracks, projects, variants, publications, audit
src/core/ffmpeg.py          ffprobe, запуск ffmpeg с прогрессом, NVENC
src/core/media.py           индексация, название из имени файла, склейки и динамика кадра
src/core/audio.py           BPM, доли, сильные доли, бас
src/core/music.py           music_library по вайбам, автоподбор трека
src/core/planner.py         4 стиля, план склеек по долям, выбор фрагментов
src/core/render.py          filtergraph: zoompan, shake, вспышки, грейд, xfade, звук
src/core/watermark.py       водяной знак в ролике и в концовке
src/core/metadata.py        заголовки, описания, приписка, теги и хештеги
src/core/service.py         проекты, рендер-очередь, meta.json, одобрение, публикация
src/core/export_nle.py      FCPXML, EDL, пакет для CapCut
src/core/paths.py           пути: исходники или собранный .exe, встроенный ffmpeg
src/core/synthetic.py       тестовые клипы и биты
packaging/                  PyInstaller spec, генератор WiX (.msi), значок
docs/LAPTOP_AGENT_PROMPT.md задание агенту на Windows-ноутбуке
src/publishers/             youtube_publisher.py, tiktok_publisher.py
web/                        index.html, app.css, app.js
music_library/              dexter_cold_dark, dark_synth_tension, phonk_aggressive, moody_cinematic, custom
tests/                      pytest
```
