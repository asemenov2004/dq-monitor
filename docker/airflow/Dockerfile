FROM apache/airflow:3.3.2

USER root
RUN mkdir -p /opt/airflow/config /opt/airflow/logs \
    && chown airflow:root /opt/airflow/config /opt/airflow/logs
USER airflow

# Ставим ядро как обычный пакет. Airflow остаётся закреплённым своей версией.
COPY --chown=airflow:root pyproject.toml README.md LICENSE /opt/dq-monitor/
COPY --chown=airflow:root src /opt/dq-monitor/src
RUN pip install --no-cache-dir "apache-airflow==3.3.2" /opt/dq-monitor \
    --constraint /home/airflow/constraints.txt && pip check

COPY --chown=airflow:root docker/airflow/bootstrap.py /opt/dq-bootstrap.py
COPY --chown=airflow:root dags /opt/airflow/dags
