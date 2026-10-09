#!/bin/bash
# Выполняется один раз при первом запуске контейнера (пустой volume).
# База хранилища создаётся самим образом (POSTGRES_DB), здесь добавляем базу метаданных.
set -euo pipefail

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "CREATE DATABASE \"$META_DB\";"
