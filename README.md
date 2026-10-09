# dq-monitor

Система автоматизированного контроля качества данных в хранилище данных.

Ядро выполняет проверки качества (полнота, уникальность, свежесть, объём, допустимые значения,
ссылочная целостность, произвольный SQL), хранит историю метрик и ищет в ней аномалии.
Хранилище данных в проекте построено на PostgreSQL с тестовым набором TPC-H.

## Требования

- Python 3.12 или новее
- Docker с Docker Compose

## Запуск

```bash
# 1. Настройки: скопировать пример и при необходимости поправить
cp .env.example .env

# 2. Поднять PostgreSQL (базы warehouse и meta создаются автоматически)
docker compose up -d

# 3. Виртуальное окружение и установка пакета
python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash); в Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

# 4. Проверить подключение к базам
python -m dq_monitor ping
```

## Разработка

```bash
pytest          # тесты
ruff check .    # линтер
ruff format .   # форматирование
```

Принятые технические решения и их обоснование: [docs/decisions.md](docs/decisions.md).

## Лицензия

MIT, см. [LICENSE](LICENSE).
