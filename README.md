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
pip install -e ".[dev,stand]"

# 4. Проверить подключение к базам
python -m dq_monitor ping

# 5. Создать таблицы базы метаданных (миграции Alembic)
alembic upgrade head
```

Модель данных базы метаданных: [docs/erd.md](docs/erd.md).

## Проверки качества

Семь типов проверок: `not_null` (доля пустых значений), `unique` (уникальность ключа), `freshness` (давность
последней загрузки), `row_count` (объём), `range` (границы и допустимые значения), `referential` (висячие ссылки),
`custom_sql` (произвольный запрос, возвращающий число нарушений). Для таблиц со столбцом партии проверяется
ожидаемая партия, то есть данные за сутки до логического времени прогона.

Набор проверок описывается в YAML-файле (пример: [checks/tpch.yaml](checks/tpch.yaml)):

```yaml
connection: tpch_warehouse
checks:
  - name: orders_custkey_not_null
    dataset: dwh.orders
    type: not_null
    params: {column: o_custkey, max_null_ratio: 0}
```

```bash
python -m dq_monitor checks load checks/tpch.yaml    # загрузить или обновить набор проверок
python -m dq_monitor checks list                     # список проверок и открытых инцидентов
python -m dq_monitor checks sql 8                    # показать SQL-запрос проверки
python -m dq_monitor run --now 2025-01-31T02:00      # прогон на заданное логическое время
```

Прогон сохраняет результаты в базе метаданных, открывает инцидент при первом нарушении и закрывает его
при первом успешном результате. Код выхода `run`: 0, если все проверки прошли, иначе 1.
Запросы проверок выполняются в режиме только чтения и с ограничением времени (`CHECK_STATEMENT_TIMEOUT_SEC`).

## Аномалии по истории метрик

Тип `anomaly` сравнивает число строк, долю NULL или среднее значение столбца с историей
предыдущих дней UTC. Используется двусторонняя z-оценка, по умолчанию окно 30 дней и порог 3.

```bash
alembic upgrade head
python -m dq_monitor checks load checks/tpch-anomalies.yaml
python -m dq_stand cycle --days 31
```

Набор добавляет 4 проверки к 22 правилам. Первые 30 дней накапливают историю со статусом
`SKIP`, с 31-го начинается оценка `PASS`/`FAIL`. Повторные прогоны одного дня не заменяют
разогрев. `SKIP` виден в отчёте, не меняет инцидент и сам по себе не делает код выхода ненулевым.

Параметры и ограничения: [docs/anomalies.md](docs/anomalies.md).
Шпаргалка демонстрации правил: [docs/demo.md](docs/demo.md).

## Полигон TPC-H

Пакет `dq_stand` создаёт экспериментальное хранилище на данных TPC-H. Схема `src` хранит полный слепок данных,
в схему `dwh` заказы (`orders`) и их строки (`lineitem`) догружаются партиями «по дням». Каждый цикл загрузки
сдвигает модельные часы на сутки и загружает партию за предыдущий день.

```bash
python -m dq_stand init            # генерация TPC-H и создание схем (SF 1: около минуты)
python -m dq_stand load --days 30  # 30 циклов загрузки
python -m dq_stand status          # модельное время, число партий, отставание
python -m dq_stand reset           # очистить факты и вернуть часы в начало
python -m dq_stand register        # зарегистрировать таблицы dwh в базе метаданных
python -m dq_stand cycle --days 30 # 30 циклов «загрузить партию + прогнать проверки»
```

Полный путь с нуля до первого прогона:

```bash
python -m dq_stand init && alembic upgrade head && python -m dq_stand register
python -m dq_monitor checks load checks/tpch.yaml
python -m dq_stand cycle --days 30
```

Масштаб данных и начальная дата задаются в `.env` (`STAND_SCALE_FACTOR`, `STAND_START_DATE`).

## Расписание: Airflow

```bash
docker compose -f compose.yaml -f compose.airflow.yaml up -d --build
```

UI: http://localhost:8081, пользователь `admin`, пароль из `AIRFLOW_ADMIN_PASSWORD`
(по умолчанию `airflow_local_password`). DAG `dq_monitor_checks` изначально на паузе.
Автоматический тик раз в минуту выполняет наступившие проверки из `meta` по cron в UTC.
Ручной Trigger выполняет все активные проверки и не сдвигает расписание. На модельных
данных для ручного запуска задаётся параметр `logical_now` из `dq_stand status`.

Тот же выбор расписаний доступен без Airflow: `python -m dq_monitor run --due`.
Шпаргалка запуска, демонстрация и устройство: [docs/airflow.md](docs/airflow.md).

## Разработка

```bash
pytest          # тесты (интеграционные создают временные базы, нужен запущенный PostgreSQL)
pytest --cov=dq_monitor   # тесты с покрытием кода
ruff check .    # линтер
ruff format .   # форматирование
```

Принятые технические решения и их обоснование: [docs/decisions.md](docs/decisions.md).

## Лицензия

MIT, см. [LICENSE](LICENSE).
