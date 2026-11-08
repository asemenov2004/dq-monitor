# Шпаргалка: демонстрация этапа 3

Актуальная демонстрация всего приложения, двух каналов уведомлений и Airflow:
[mvp-demo.md](mvp-demo.md). Ниже сохранён отдельный сценарий изучения этапа 3.

Выполняй по порядку. Нужны Git Bash и SQL-клиент, например DBeaver.
Сценарий рассчитан на созданный чистый полигон SF 1. Статистического поиска аномалий
пока нет: вручную внесём дефект и проверим, что заданные правила его обнаружат.

## 1. Запустить стенд

Где: Git Bash.

```bash
cd "/c/Documents/4 курс/Диплом/dq-monitor"
source .venv/Scripts/activate
docker compose up -d
python -m dq_monitor ping
```

Что делает: активирует Python-окружение, запускает PostgreSQL, проверяет обе базы.
Ожидаем: две строки с версией PostgreSQL. Docker Desktop должен быть запущен.

## 2. Подготовить проверки

Где: тот же Git Bash.

```bash
alembic upgrade head
python -m dq_stand register
python -m dq_monitor checks load checks/tpch.yaml
python -m dq_monitor checks list
```

Что делает: создаёт таблицы приложения, регистрирует 8 таблиц полигона, загружает
22 правила из YAML. Повторный запуск не создаёт дубли.
Ожидаем: список 22 проверок.

Если полигон ещё не создан, перед этим шагом один раз выполни:

```bash
python -m dq_stand init
```

Генерирует полный слепок SF 1, справочники и пустые факты. На уже созданном полигоне
не требуется. `init --force` пересоздаёт данные, для демонстрации его не используем.

## 3. Загрузить день и проверить чистые данные

Где: Git Bash.

```bash
python -m dq_stand cycle --days 1
python -m dq_stand status
```

Что делает: сдвигает модельные часы на сутки, загружает следующую партию, запускает проверки.
Ожидаем: `pass 22, fail 0, error 0`.

Сохраняем текущее время автоматически, чтобы не подставлять дату вручную:

```bash
DEMO_NOW=$(docker exec dq-postgres psql -U dq -d warehouse -Atc "SELECT to_char(model_now, 'YYYY-MM-DD') || 'T' || to_char(model_now, 'HH24:MI:SS') FROM stand.clock")
echo "$DEMO_NOW"
python -m dq_monitor run --now "$DEMO_NOW"
```

Что делает: берёт время из `stand.clock` и повторно проверяет ту же партию.
Ожидаем: все проверки PASS. Новые партии команда `run` не загружает.
Далее используй тот же терминал: там хранится переменная DEMO_NOW.
До конца демонстрации больше не запускай load или cycle, чтобы не сменить партию.

## 4. Открыть базу в SQL-клиенте

Создай два подключения:

| Поле | Значение |
|---|---|
| Драйвер | PostgreSQL |
| Host | localhost |
| Port | 5433 |
| User | dq |
| Password | POSTGRES_PASSWORD из локального .env |
| Database | warehouse для данных, meta для результатов |
| SSL | отключён для локального стенда |

В SQL-клиенте включи Auto-commit: изменения должны сразу становиться видны ядру.

Где: SQL-редактор подключения warehouse.

```sql
SELECT model_now FROM stand.clock;
SELECT max(batch_date) AS last_batch, count(*) AS all_orders FROM dwh.orders;
SELECT * FROM dwh.orders ORDER BY batch_date DESC LIMIT 10;
```

Что показать: отдельные модельные часы, дату партии batch_date и время загрузки loaded_at.

## 5. Испортить 10 заказов

Где: SQL-редактор warehouse.

```sql
UPDATE dwh.orders
SET o_custkey = NULL
WHERE ctid IN (
    SELECT ctid FROM dwh.orders
    WHERE batch_date = (SELECT max(batch_date) FROM dwh.orders)
    LIMIT 10
)
RETURNING o_orderkey, o_custkey, batch_date;
```

Что делает: стирает номер клиента в 10 заказах последней партии.
Ожидаем: 10 изменённых строк с NULL. Исходный слепок src.orders остаётся целым.

## 6. Обнаружить дефект

Где: Git Bash.

```bash
python -m dq_monitor run --now "$DEMO_NOW"
python -m dq_monitor checks list
```

Ожидаем: `pass 21, fail 1, error 0`, `открыто 1`.
FAIL у orders_custkey_not_null. Код выхода 1 ожидаем: найдены плохие данные.
Проверка внешнего ключа NULL пропускает: отсутствие значения проверяет not_null.

Где: SQL-редактор meta.

```sql
SELECT i.id, c.name, i.state, i.opened_at, i.closed_at
FROM incident i JOIN dq_check c ON c.id = i.check_id
WHERE c.name = 'orders_custkey_not_null'
ORDER BY i.id DESC;
```

Ожидаем: верхняя запись open, closed_at пустое. Запомни её ID.
Ниже могут быть старые инциденты от прошлых демонстраций.

## 7. Повторить провал

Где: Git Bash.

```bash
python -m dq_monitor run --now "$DEMO_NOW"
```

Ожидаем: снова один FAIL, но `открыто 0`. Существующий инцидент остаётся открытым.
Повтори SQL из шага 6: последний ID прежний, новой записи нет.

## 8. Исправить данные

Где: SQL-редактор warehouse.

```sql
UPDATE dwh.orders o
SET o_custkey = s.o_custkey
FROM src.orders s
WHERE s.o_orderkey = o.o_orderkey
  AND o.o_custkey IS NULL
  AND o.batch_date = (SELECT max(batch_date) FROM dwh.orders);
```

Что делает: восстанавливает номер клиента из полного слепка.
Ожидаем: на чистом до демонстрации полигоне исправлено 10 строк.

## 9. Закрыть инцидент

Где: Git Bash.

```bash
python -m dq_monitor run --now "$DEMO_NOW"
```

Ожидаем: `pass 22, fail 0, error 0`, `закрыто 1`.
Повтори SQL из шага 6 в meta: тот же инцидент теперь closed, closed_at заполнено.
Время открытия и закрытия может совпасть: модельные часы в этом сценарии не двигались.

## 10. Показать историю

Где: SQL-редактор meta.

```sql
SELECT r.id AS run_id, r.logical_now, c.name, cr.status,
       cr.observed_value, cr.message, cr.duration_ms
FROM check_result cr
JOIN run r ON r.id = cr.run_id
JOIN dq_check c ON c.id = cr.check_id
WHERE c.name = 'orders_custkey_not_null'
ORDER BY cr.id DESC
LIMIT 10;
```

Что показать: последние результаты в обратном порядке PASS, FAIL, FAIL, PASS.
Каждый прогон оставляет результат; один инцидент объединяет последовательные провалы.

## Дополнительно: показать SQL проверки

Где: Git Bash. Найди ID нужной проверки в checks list и подставь вместо 4.

```bash
python -m dq_monitor checks sql 4 --now "$DEMO_NOW"
```

Что делает: печатает построенный SQL и параметры, запрос не выполняет.
Для not_null база считает строки и NULL, Python сравнивает долю с порогом.
