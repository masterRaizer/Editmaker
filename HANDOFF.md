# HANDOFF — EditMaker

## Состояние
- Ветка: `ccr-e57fc87f-b0z4sx` (единственная, она же основная).
- Релиз: v1.0.0 — https://github.com/masterRaizer/Editmaker/releases/tag/v1.0.0 (`EditMaker-1.0.0-x64.msi`, `EditMaker-1.0.0-portable.exe`).
- CI: «Windows build (exe + msi)», run 44955840855 на dee61aa — success: 47 тестов на Windows со встроенным ffmpeg n9.0, smoke `EditMaker.exe check/selftest`, установка и удаление MSI.
- Локально (Linux): 47 тестов, финальный рендер 1080×1920@60, сценарий UI в Playwright.
- Итераций CI в сессии: 3 (лимит достигнут). Следующие сборки — в новой сессии.

## Как выпустить следующий релиз
1. Поднять `__version__` в `src/__init__.py` и обновить `docs/RELEASE_NOTES.md`.
2. Actions → «Windows build (exe + msi)» → Run workflow → release = true. Тег `v<версия>` и релиз создаёт сам workflow (push тегов из облачной сессии заблокирован прокси).

## Не проверено
- Окно pywebview/WebView2 на реальной Windows (в CI только консольные check/selftest и установка MSI).
- NVENC (в CI нет GPU) и режим «Оптический поток» на Windows.
- Реальные загрузки на YouTube и TikTok (нужны ключи пользователя; издатели покрыты тестами с fake-сервером).
- Импорт FCPXML в DaVinci Resolve и EDL в Premiere на живых программах.

## Что дальше
- Прогнать установку по `docs/LAPTOP_AGENT_PROMPT.md` на ноутбуке и вписать сюда найденные баги.
- Цифровая подпись exe/msi, чтобы не было предупреждения SmartScreen.
- По отзывам о качестве: подстройка силы эффектов, новые переходы, пресеты грейда.
