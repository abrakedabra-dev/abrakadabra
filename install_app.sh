#!/bin/zsh
# Создаёт Абракадабру как фоновое приложение macOS:
#   ~/Applications/Abrakadabra.app — без окна Терминала и без значка в Доке,
#   сама перезапускается, если упала. Выключается фразой «Абракадабра, выключись».
set -e

PROJECT="$HOME/abrakadabra"
APP="$HOME/Applications/Abrakadabra.app"

if [[ ! -x "$PROJECT/.venv/bin/python" ]]; then
  echo "❌ Не найден $PROJECT/.venv/bin/python. Сначала запусти помощника через: uv run abra.py"
  exit 1
fi

mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.abrakadabra.app</string>
  <key>CFBundleName</key><string>Abrakadabra</string>
  <key>CFBundleExecutable</key><string>abrakadabra</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>2</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Абракадабра слушает голосовые команды.</string>
  <key>NSAppleEventsUsageDescription</key><string>Абракадабра управляет приложениями по голосовым командам.</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/abrakadabra" <<'SH'
#!/bin/zsh
PROJECT="$HOME/abrakadabra"
LOG="$PROJECT/abra.log"
cd "$PROJECT"

# Второй экземпляр не запускаем
if pgrep -f "abrakadabra/abra.py" >/dev/null; then exit 0; fi

# Держим лог небольшим
if [[ -f "$LOG" ]] && (( $(stat -f%z "$LOG") > 2000000 )); then mv "$LOG" "$LOG.old"; fi

# Перезапуск при падении. Код выхода 0 («выключись») — остановка без перезапуска.
until "$PROJECT/.venv/bin/python" -u "$PROJECT/abra.py" >> "$LOG" 2>&1; do
  echo "--- упала, перезапуск $(date '+%H:%M:%S') ---" >> "$LOG"
  sleep 3
done
SH

chmod +x "$APP/Contents/MacOS/abrakadabra"

cat > "$PROJECT/stop.sh" <<'SH'
#!/bin/zsh
pkill -f "Abrakadabra.app/Contents/MacOS/abrakadabra" 2>/dev/null
pkill -f "abrakadabra/abra.py" 2>/dev/null
echo "Абракадабра остановлена."
SH
chmod +x "$PROJECT/stop.sh"

echo "✅ Готово: $APP"
echo "Запуск:     open ~/Applications/Abrakadabra.app"
echo "Остановка:  ~/abrakadabra/stop.sh   (или голосом: «Абракадабра, выключись»)"
echo "Лог:        tail -f ~/abrakadabra/abra.log"
