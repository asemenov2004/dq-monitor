# Модель данных базы метаданных

Схема соответствует миграции `20261031_c3f5b7d9e021_schedule_slots`.
Во всех таблицах заданы первичные и внешние ключи; внешние ключи с `ON DELETE RESTRICT`,
поэтому удалить запись, на которую ссылается история, невозможно.

```mermaid
erDiagram
    connection ||--o{ dataset : "содержит"
    dataset ||--o{ dq_check : "проверяется"
    dq_check ||--o{ check_result : "результаты"
    run ||--o{ check_result : "включает"
    dq_check ||--o{ incident : "порождает"
    check_result ||--o{ incident : "открывает и закрывает"
    incident ||--o{ notification : "оповещает"

    connection {
        int id PK
        varchar name UK
        varchar db_type "postgres или clickhouse"
        varchar host
        int port
        varchar database
        varchar username
        varchar password_env "имя переменной окружения с паролем"
        timestamptz created_at
    }
    dataset {
        int id PK
        int connection_id FK
        varchar schema_name
        varchar table_name
        varchar batch_column "столбец партии, может быть пустым"
        varchar loaded_at_column "столбец времени загрузки"
        text description
        timestamptz created_at
    }
    dq_check {
        int id PK
        int dataset_id FK
        varchar name
        varchar check_type "7 правил и anomaly"
        jsonb params
        varchar severity "warning или critical"
        varchar schedule "cron, пусто означает каждый прогон"
        bool is_active
        text description
        timestamptz created_at
        timestamptz updated_at
    }
    run {
        bigint id PK
        timestamptz logical_now "время, с которым выполнялись проверки"
        varchar trigger "manual или schedule"
        timestamptz started_at
        timestamptz finished_at
    }
    check_result {
        bigint id PK
        bigint run_id FK
        int check_id FK
        varchar status "pass, fail, error или skip"
        numeric observed_value
        numeric expected_low
        numeric expected_high
        text message
        int duration_ms
        varchar config_hash "SHA-256 версии параметров и источника"
        jsonb details "диагностика детектора"
        timestamptz scheduled_for "обработанный UTC-интервал cron; NULL у ручного запуска"
    }
    incident {
        int id PK
        int check_id FK
        bigint opened_result_id FK
        bigint closed_result_id FK
        varchar state "open или closed"
        timestamptz opened_at
        timestamptz closed_at
    }
    notification {
        int id PK
        int incident_id FK
        varchar channel "telegram"
        varchar event "opened или closed"
        varchar status "sent или failed"
        text error
        timestamptz sent_at
    }
```

## Ограничения сверх ключей

| Таблица | Ограничение | Смысл |
|---|---|---|
| dataset | UNIQUE (connection_id, schema_name, table_name) | таблица регистрируется один раз |
| dq_check | UNIQUE (dataset_id, name) | имена проверок не повторяются в пределах таблицы |
| check_result | UNIQUE (run_id, check_id) | в одном прогоне проверка выполняется один раз |
| check_result | UNIQUE (check_id, scheduled_for) | один результат на cron-интервал; ручные NULL не конфликтуют |
| incident | уникальный индекс по check_id при state = 'open' | не больше одного открытого инцидента на проверку |
| incident | CHECK state_consistency | у открытого инцидента нет даты закрытия, у закрытого она обязательна |
| все перечисления | CHECK ... IN (...) | недопустимое значение статуса или типа не запишется |
