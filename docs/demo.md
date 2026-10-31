# Демонстрация ядра проверок (этап 3)

Команды выполняются из корня репозитория в Git Bash.

## Подготовка

```bash
source .venv/Scripts/activate
docker compose up -d
python -m dq_monitor ping
alembic upgrade head
python -m dq_stand register
python -m dq_monitor checks load checks/tpch.yaml
python -m dq_stand status
```

Полигон должен быть инициализирован командой `python -m dq_stand init`.
Не запускайте `init --force` для обычной демонстрации: команда пересоздаёт данные.

## Чистые данные

Следующая команда сама загружает новый день и запускает 22 проверки с модельным временем:

```bash
python -m dq_stand cycle --days 1
python -m dq_monitor checks list
```

Для повторного прогона без загрузки нового дня используйте модельное время из `status`.
Пример для состояния на 31.10.2026: 203 партии, часы 2025-07-23 02:00:

```bash
python -m dq_monitor run --now 2025-07-23T02:00
```

После `cycle` время изменится. Подставляйте актуальное значение.

## Дефект и жизненный цикл инцидента

В SQL-клиенте подключитесь к базе `warehouse`. Запрос портит 10 строк последней партии;
возвращаемая дата подсказывает, какую партию затронули. Рассчитано на чистый полигон SF 1.

```sql
UPDATE dwh.orders
SET o_custkey = NULL
WHERE ctid IN (
    SELECT ctid FROM dwh.orders
    WHERE batch_date = (SELECT max(batch_date) FROM dwh.orders)
    LIMIT 10
)
RETURNING o_orderkey, batch_date;
```

Дважды выполните `dq_monitor run --now ...` с неизменным временем из `status`.
Первый прогон обнаружит NULL и откроет инцидент, второй оставит тот же инцидент открытым.
Код выхода 1 здесь ожидаем: найдены плохие данные.

В SQL-клиенте восстановите значения из полного слепка:

```sql
UPDATE dwh.orders o
SET o_custkey = s.o_custkey
FROM src.orders s
WHERE s.o_orderkey = o.o_orderkey
  AND o.o_custkey IS NULL;
```

Повторите прогон с тем же временем: все проверки должны пройти, инцидент закроется.

## Что показать в базе meta

```sql
SELECT id, name, check_type, params FROM dq_check ORDER BY id;
SELECT id, logical_now, started_at, finished_at FROM run ORDER BY id DESC LIMIT 10;
SELECT * FROM check_result ORDER BY id DESC LIMIT 22;
SELECT * FROM incident ORDER BY id DESC;
```

Результаты фиксируются по одному: результат проверки и изменение её инцидента сохраняются
вместе. Уже сохранённые результаты переживают прерывание прогона; у незавершённого прогона
`finished_at` остаётся NULL. В текущем `update_incident` нет `FOR UPDATE`; одновременные прогоны
ограничены согласно О-05 в журнале решений.

## Подключение SQL-клиента

Для DBeaver / DataGrip / pgAdmin: PostgreSQL, host `localhost`, port `5433`, user `dq`.
Создайте подключения к базам `warehouse` и `meta`. Пароль смотрите в локальном `.env`,
переменная `POSTGRES_PASSWORD`. Для локального стенда SSL можно отключить.
В `warehouse`: `src` — полный слепок, `dwh` — контролируемые данные, `stand` — часы и журнал.
В `meta` все таблицы приложения находятся в `public`.
