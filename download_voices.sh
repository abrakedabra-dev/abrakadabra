#!/bin/zsh
# Скачивает русские мужские голоса Piper в ~/abrakadabra/voices
set -e
DIR="$(cd "$(dirname "$0")" && pwd)/voices"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU"
mkdir -p "$DIR"
for v in dmitri denis ruslan; do
  for ext in onnx onnx.json; do
    f="$DIR/ru_RU-$v-medium.$ext"
    if [[ -s "$f" ]]; then
      echo "✓ уже есть: ru_RU-$v-medium.$ext"
    else
      echo "↓ качаю: ru_RU-$v-medium.$ext"
      curl -fL --progress-bar -o "$f" "$BASE/$v/medium/ru_RU-$v-medium.$ext"
    fi
  done
done
echo "Готово. Переключение голоса: «Абракадабра, смени голос»"
