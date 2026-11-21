# Задание агенту Claude Code на Windows-ноутбуке: установить и проверить EditMaker

Скопируй этот файл агенту целиком. Агент работает на ноутбуке пользователя с Windows 10/11.

---

Ты — агент на Windows-ноутбуке пользователя. Цель: поставить EditMaker (утилита для аниме- и dark-эдитов) из готовой сборки, проверить её на тестовых и реальных данных, подготовить к публикации на YouTube и TikTok и коротко отчитаться. Отвечай по-русски. Отчёт — не длиннее 6 строк: Сделано / Проверка / Артефакт / Следующий шаг. Пиши только то, что проверил сам; остальное помечай «не проверено».

## Жёсткие правила

1. Не нажимай «Одобрить» и «Опубликовать одобренное» сам. Одобряет и публикует только пользователь.
2. Секреты (client_secret, токены, пароли) не выводи в чат и не коммить. Если пользователь прислал секрет в чат — не повторяй его и предложи сменить.
3. Ничего не удаляй в пользовательских папках (исходники, музыка, Видео). Опасные операции на ПК — через скилл `win-admin-safe`, если он есть. Если действие заблокировано, сразу дай пользователю готовую команду PowerShell и не зависай.
4. Репозиторий `masterRaizer/Editmaker` публичный: в него нельзя писать пути пользователя, имена ПК, IP, e-mail, ключи.

## Шаг 1. Получить сборку

Сборка делается в GitHub Actions, workflow «Windows build (exe + msi)», ветка `ccr-e57fc87f-b0z4sx`.

```powershell
gh auth status
gh run list -R masterRaizer/Editmaker -w "Windows build (exe + msi)" -L 5
# возьми ID последнего запуска со статусом success
gh run download <RUN_ID> -R masterRaizer/Editmaker -D $env:USERPROFILE\Downloads\EditMaker-build
Get-ChildItem $env:USERPROFILE\Downloads\EditMaker-build -Recurse | Get-FileHash -Algorithm SHA256
```

В артефакте два файла:
- `EditMaker-<версия>-x64.msi` — установщик в `C:\Program Files\EditMaker`, ярлыки в «Пуске» и на рабочем столе;
- `EditMaker-<версия>-portable.exe` — один файл без установки; первый запуск дольше на 5–15 с, пока программа распаковывается.

Если успешных запусков нет — открой лог упавшего шага (`gh run view <RUN_ID> --log-failed`), опиши ошибку в отчёте и переходи к сборке из исходников (шаг 1б).

### Шаг 1б. Сборка из исходников (только если CI не дал артефакт)

```powershell
winget install -e --id Python.Python.3.12
git clone -b ccr-e57fc87f-b0z4sx https://github.com/masterRaizer/Editmaker
cd Editmaker
py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-build.txt
# ffmpeg: скачай ffmpeg-n7.1-latest-win64-gpl-7.1.zip из https://github.com/BtbN/FFmpeg-Builds/releases
# и положи ffmpeg.exe и ffprobe.exe в packaging\ffmpeg\
python -m pytest -q
pyinstaller --noconfirm --clean packaging\editmaker.spec
dotnet tool install --global wix --version 5.0.2
python packaging\make_wxs.py dist\EditMaker build\EditMaker.wxs 1.0.0
wix build -arch x64 build\EditMaker.wxs -o dist\EditMaker-1.0.0-x64.msi
```

## Шаг 2. Установить и запустить

```powershell
$msi = Get-ChildItem $env:USERPROFILE\Downloads\EditMaker-build -Recurse -Filter *.msi | Select-Object -First 1
Start-Process msiexec -ArgumentList "/i `"$($msi.FullName)`" /l*v $env:TEMP\editmaker-install.log" -Wait
Test-Path "$env:ProgramFiles\EditMaker\EditMaker.exe"
Test-Path "$env:ProgramFiles\EditMaker\_internal\ffmpeg\ffmpeg.exe"
```

- Windows SmartScreen может предупредить о неподписанном файле: «Подробнее» → «Выполнить в любом случае». Это ожидаемо, сборка без код-подписи.
- Окно программы работает на WebView2. Если окно пустое или не открылось, проверь и установи среду выполнения:
  ```powershell
  winget list Microsoft.EdgeWebView2Runtime
  winget install -e --id Microsoft.EdgeWebView2Runtime
  ```
  Без WebView2 программа сама откроет интерфейс в браузере. В этом режиме в шапке есть кнопка «Завершить».
- Данные и логи: `%APPDATA%\EditMaker\data\` (`config.json`, `editmaker.db`, `editmaker.log`, `cli.log`). Ключи: `%APPDATA%\EditMaker\secrets\`.

## Шаг 3. Самопроверка без UI

```powershell
Start-Process "$env:ProgramFiles\EditMaker\EditMaker.exe" -ArgumentList "check" -Wait   # оконная программа: без -Wait PowerShell не ждёт
Get-Content "$env:APPDATA\EditMaker\data\cli.log"      # версия ffmpeg, nvenc: true/false, GPU
$p = Start-Process "$env:ProgramFiles\EditMaker\EditMaker.exe" -ArgumentList "selftest","$env:TEMP\EditMakerSelftest" -Wait -PassThru
$p.ExitCode                                             # 0 = успех
Get-ChildItem "$env:TEMP\EditMakerSelftest\selftest"    # variant_01..04.mp4, .jpg, meta.json
```

Открой два-три `variant_0X.mp4` и проверь: вертикальный кадр, склейки попадают в бит, водяной знак `@editmaker_test` в углу и крупно в чёрной концовке.

## Шаг 4. Настройка под пользователя

Спроси у пользователя три пути, сам их не выдумывай:
1. Папка (или несколько) с исходниками на SSD.
2. Папка вывода готовых эдитов.
3. Где лежат его треки.

Музыка раскладывается по папкам вайбов в `%USERPROFILE%\Music\EditMaker\music_library\` (на русской Windows может быть `Музыка`):

| Папка | Звучание |
|---|---|
| `dexter_cold_dark` | холодный, напряжённый dark thriller |
| `dark_synth_tension` | тёмный синтвейв, пульсирующий саспенс |
| `phonk_aggressive` | агрессивный фонк, плотный бас |
| `moody_cinematic` | атмосферный кинематографичный звук |
| `custom` | любые треки |

Треки пользователя копируй (`Copy-Item`), не переноси. В EditMaker нажми «Сканировать музыку».

## Шаг 5. Проверка на реальном клипе

В окне EditMaker:
1. «Исходники» → «Добавить папку» → папка с SSD → «Сканировать». Проверь, что названия и серии распознаны (например, `Dexter — S01E03 …`).
2. «Папка вывода» → «Выбрать».
3. Вайб «Dexter / Dark Cold», трек — «Автоподбор по темпу видео».
4. «Водяной знак» — ник пользователя (спроси), «В ролике» и «В конце ролика» включены. Превью над полем должно показывать знак в углу и в концовке.
5. Формат 1080×1920, 60 fps, 20 с, 4 варианта, «Финал», кодек «Авто».
6. «Создать варианты». Засеки время рендера одного варианта. В строке «Видео» под плеером должно быть `h264_nvenc`, если в ноутбуке есть NVIDIA, иначе `libx264`.
7. Проверь: переключение вариантов клавишами 1–4, переход по склейкам стрелками ←/→, «Сравнить два варианта», «Показать файл».
8. Экспорт: «DaVinci Resolve / Final Cut», «Premiere (EDL)», «Пакет для CapCut». Если стоит DaVinci Resolve — импортируй `.fcpxml` (File → Import → Timeline) и проверь, что клипы и музыка на месте.

## Шаг 6. Подключение YouTube и TikTok (вместе с пользователем)

YouTube:
1. Пользователь в Google Cloud Console создаёт проект, включает YouTube Data API v3, настраивает OAuth consent screen (свой аккаунт в Test users), создаёт OAuth client ID типа **Desktop app** и скачивает JSON.
2. Ты сохраняешь файл как `%APPDATA%\EditMaker\secrets\youtube_client_secret.json`. Содержимое не читай вслух и не выводи.
3. В EditMaker: «Аккаунты» → «Подключить YouTube». Откроется системный браузер, пользователь входит сам. После входа статус — «подключён».

TikTok:
1. Пользователь на developers.tiktok.com создаёт приложение, добавляет Login Kit и Content Posting API со scopes `video.upload` и `video.publish`, регистрирует https redirect URI.
2. Ты создаёшь `%APPDATA%\EditMaker\secrets\tiktok_app.json` вида `{"client_key": "...", "client_secret": "..."}` — значения вводит пользователь.
3. «Подключить TikTok» → вход → скопировать адрес, на который вернул TikTok, в поле «Код TikTok или адрес после входа» → «Отправить».
4. Пока приложение не прошло аудит TikTok, используйте режим «Черновик в TikTok» или видимость «Только я».

Первую публикацию делает пользователь: проверяет ролик и тексты, жмёт «Одобрить», затем «Опубликовать одобренное», лучше с доступом «Ограниченный доступ» на YouTube. Ты только проверяешь, что под кнопкой появилась ссылка.

## Шаг 7. Если что-то не так

- Сначала `%APPDATA%\EditMaker\data\editmaker.log` и `cli.log`.
- Ошибку рендера видно под плеером у варианта со статусом `failed`. Скопируй текст ffmpeg из неё.
- Порт 8765 занят — программа сама берёт свободный. В браузерном режиме адрес есть в `editmaker.log`.
- Антивирус удалил `ffmpeg.exe` → восстанови из карантина и добавь `C:\Program Files\EditMaker` в исключения (с согласия пользователя).

Найденные баги оформи в `HANDOFF.md` в корне репозитория (ветка `ccr-e57fc87f-b0z4sx`): что делал, что ожидал, что получил, текст ошибки из лога. Перед коммитом убери оттуда пути пользователя, имена ПК, e-mail и ключи: репозиторий публичный.

## Итоговый отчёт (≤ 6 строк)

```
Сделано: установлен EditMaker <версия> (msi), WebView2 <есть/поставлен>, музыка: <N> треков в <M> вайбах
Проверка: selftest <ok/ошибка>; реальный проект: 4 варианта за <мин>, кодек <nvenc/x264>; экспорт FCPXML <ok/не проверено>
Артефакт: <путь к папке вывода>, YouTube <подключён/нет>, TikTok <подключён/нет>
Следующий шаг: <что нужно от пользователя или разработчика>
```
