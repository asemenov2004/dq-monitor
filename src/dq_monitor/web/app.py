"""FastAPI + Jinja: локальный интерфейс настройки и наблюдения за ядром DQ."""

import hmac
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from dq_monitor.config import Settings, get_settings
from dq_monitor.connectors import connector_for
from dq_monitor.coordination import RunInProgressError
from dq_monitor.errors import error_text
from dq_monitor.meta.models import CheckResult, Connection, Dataset, DqCheck, Incident, Run
from dq_monitor.report import format_value
from dq_monitor.runner import as_aware_utc, run_checks
from dq_monitor.web.charts import metric_chart
from dq_monitor.web.forms import (
    CHECK_LABELS,
    DatasetFields,
    SourceFields,
    check_form,
    parameter_fields,
)

HERE = Path(__file__).resolve().parent


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    owns_engine = engine is None
    engine = engine or create_engine(settings.meta_url, connect_args={"connect_timeout": 5})

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owns_engine:
            engine.dispose()

    app = FastAPI(title="dq-monitor", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Один процесс сервера. При рестарте неподписанные старым ключом формы надо открыть заново.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_urlsafe(48),
        same_site="strict",
        session_cookie="dq_session",
        max_age=8 * 3600,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["number"] = format_value
    templates.env.filters["utc"] = lambda d: d.strftime("%d.%m.%Y %H:%M UTC") if d else "—"

    @app.middleware("http")
    async def headers(request, call_next):
        if (
            request.headers.get("content-length", "").isdigit()
            and int(request.headers["content-length"]) > 65536
        ):
            return JSONResponse({"detail": "Форма слишком большая"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def page(request, name, response_status=200, **context):
        token = request.session.setdefault("csrf", secrets.token_urlsafe(32))
        return templates.TemplateResponse(
            request=request,
            name=name,
            status_code=response_status,
            context={
                "csrf": token,
                "notice": request.session.pop("notice", None),
                "labels": CHECK_LABELS,
                "now": datetime.now(UTC),
                **context,
            },
        )

    def redirect(request, url, message=None):
        if message:
            request.session["notice"] = message[:1500]
        return RedirectResponse(url, status_code=303)

    async def protected_form(request):
        form = await request.form(max_fields=100, max_files=0, max_part_size=65536)
        expected = request.session.get("csrf", "")
        supplied = form.get("csrf", "")
        if (
            not expected
            or not isinstance(supplied, str)
            or not hmac.compare_digest(expected, supplied)
        ):
            raise HTTPException(
                403, "Форма устарела или отсутствует CSRF-токен. Откройте страницу заново."
            )
        return form

    def get_or_404(session, model, key):
        item = session.get(model, key)
        if item is None:
            raise HTTPException(404, "Запись не найдена")
        return item

    def latest_results(session):
        ids = select(func.max(CheckResult.id)).group_by(CheckResult.check_id)
        return {
            r.check_id: r
            for r in session.scalars(
                select(CheckResult)
                .where(CheckResult.id.in_(ids))
                .options(joinedload(CheckResult.run))
            )
        }

    def check_editor(request, session, check=None, posted=None, error=None):
        selected = (posted or {}).get("check_type", check.check_type if check else "row_count")
        if selected not in CHECK_LABELS:
            selected = "row_count"
        datasets = session.scalars(
            select(Dataset)
            .options(joinedload(Dataset.connection))
            .order_by(Dataset.schema_name, Dataset.table_name)
        ).all()
        return page(
            request,
            "check_form.html",
            response_status=422 if error else 200,
            check=check,
            form=posted or {},
            selected=selected,
            datasets=datasets,
            error=error,
            groups=parameter_fields(selected, check.params if check else {}, posted),
        )

    @app.get("/", name="dashboard")
    def dashboard(request: Request):
        with Session(engine) as session:
            checks = session.scalars(select(DqCheck)).all()
            results = latest_results(session)
            counts = {s: 0 for s in ("pass", "fail", "error", "skip", "pending")}
            for check in checks:
                if check.is_active:
                    counts[results[check.id].status if check.id in results else "pending"] += 1
            incidents = session.scalars(
                select(Incident)
                .where(Incident.state == "open")
                .options(joinedload(Incident.check))
                .order_by(Incident.id.desc())
            ).all()
            runs = session.scalars(select(Run).order_by(Run.id.desc()).limit(8)).all()
            return page(
                request,
                "dashboard.html",
                counts=counts,
                active=sum(c.is_active for c in checks),
                total=len(checks),
                incidents=incidents,
                runs=runs,
            )

    @app.get("/checks")
    def checks_list(request: Request, status: str = "all", dataset: str = ""):
        if status not in ("all", "pass", "fail", "error", "skip", "pending"):
            raise HTTPException(400, "Неверный фильтр статуса")
        try:
            dataset_id = int(dataset) if dataset else None
        except ValueError as exc:
            raise HTTPException(400, "Неверная таблица") from exc
        with Session(engine) as session:
            checks = session.scalars(
                select(DqCheck).options(joinedload(DqCheck.dataset)).order_by(DqCheck.id)
            ).all()
            results = latest_results(session)
            checks = [
                c
                for c in checks
                if (dataset_id is None or c.dataset_id == dataset_id)
                and (
                    status == "all"
                    or (results[c.id].status if c.id in results else "pending") == status
                )
            ]
            datasets = session.scalars(select(Dataset).order_by(Dataset.table_name)).all()
            return page(
                request,
                "checks.html",
                checks=checks,
                results=results,
                datasets=datasets,
                status=status,
                dataset_id=dataset_id,
            )

    @app.get("/checks/new")
    def new_check(request: Request):
        with Session(engine) as session:
            return check_editor(request, session)

    @app.get("/checks/{check_id}")
    def check_detail(request: Request, check_id: int):
        with Session(engine) as session:
            check = get_or_404(session, DqCheck, check_id)
            rows = session.scalars(
                select(CheckResult)
                .where(CheckResult.check_id == check_id)
                .options(joinedload(CheckResult.run))
                .order_by(CheckResult.id.desc())
                .limit(100)
            ).all()
            ordered = sorted(rows, key=lambda r: (r.run.logical_now, r.id))
            incidents = session.scalars(
                select(Incident)
                .where(Incident.check_id == check_id)
                .order_by(Incident.id.desc())
                .limit(20)
            ).all()
            return page(
                request,
                "check_detail.html",
                check=check,
                rows=rows,
                chart=metric_chart(ordered),
                incidents=incidents,
            )

    @app.get("/checks/{check_id}/edit")
    def edit_check(request: Request, check_id: int):
        with Session(engine) as session:
            return check_editor(request, session, get_or_404(session, DqCheck, check_id))

    async def save_check(request, check_id=None):
        form = await protected_form(request)
        with Session(engine) as session:
            check = get_or_404(session, DqCheck, check_id) if check_id else None
            try:
                values = check_form(form)
                get_or_404(session, Dataset, values["dataset_id"])
                if check is None:
                    check = DqCheck(**values)
                    session.add(check)
                else:
                    for key, value in values.items():
                        setattr(check, key, value)
                session.commit()
                return redirect(request, f"/checks/{check.id}", "Проверка сохранена")
            except (ValueError, IntegrityError) as exc:
                session.rollback()
                if check_id is None:
                    check = None
                else:
                    check = session.get(DqCheck, check_id)
                error = (
                    "Проверка с таким именем для этой таблицы уже существует"
                    if isinstance(exc, IntegrityError)
                    else error_text(exc)
                )
                return check_editor(request, session, check, form, error)

    @app.post("/checks/new")
    async def create_check_route(request: Request):
        return await save_check(request)

    @app.post("/checks/{check_id}/edit")
    async def update_check_route(request: Request, check_id: int):
        return await save_check(request, check_id)

    @app.post("/checks/{check_id}/toggle")
    async def toggle_check(request: Request, check_id: int):
        await protected_form(request)
        with Session(engine) as session, session.begin():
            check = get_or_404(session, DqCheck, check_id)
            check.is_active = not check.is_active
        return redirect(
            request, f"/checks/{check_id}", "Активность проверки изменена. История сохранена."
        )

    @app.post("/runs")
    async def launch_run(request: Request):
        form = await protected_form(request)
        try:
            now = (
                as_aware_utc(datetime.fromisoformat(form["logical_now"]))
                if form.get("logical_now")
                else datetime.now(UTC)
            )
            ids = [int(form["check_id"])] if form.get("check_id") else None
            if ids:
                with Session(engine) as session:
                    get_or_404(session, DqCheck, ids[0])
            report = await run_in_threadpool(run_checks, engine, now, check_ids=ids)
            return redirect(
                request, f"/runs/{report.run_id}", "Прогон завершён, результаты сохранены"
            )
        except (ValueError, RunInProgressError) as exc:
            return redirect(request, "/runs", str(exc))

    @app.get("/runs")
    def runs_list(request: Request, page_number: int = Query(1, ge=1, le=100000)):
        with Session(engine) as session:
            runs = session.scalars(
                select(Run).order_by(Run.id.desc()).offset((page_number - 1) * 30).limit(31)
            ).all()
            return page(
                request, "runs.html", runs=runs[:30], more=len(runs) > 30, page_number=page_number
            )

    @app.get("/runs/{run_id}")
    def run_detail(request: Request, run_id: int):
        with Session(engine) as session:
            run = get_or_404(session, Run, run_id)
            rows = session.scalars(
                select(CheckResult)
                .where(CheckResult.run_id == run_id)
                .options(joinedload(CheckResult.check))
                .order_by(CheckResult.check_id)
            ).all()
            return page(request, "run_detail.html", run=run, rows=rows)

    @app.get("/incidents")
    def incidents_list(
        request: Request, state: str = "open", page_number: int = Query(1, ge=1, le=100000)
    ):
        if state not in ("all", "open", "closed"):
            raise HTTPException(400, "Неверное состояние инцидента")
        with Session(engine) as session:
            query = (
                select(Incident).options(joinedload(Incident.check)).order_by(Incident.id.desc())
            )
            if state != "all":
                query = query.where(Incident.state == state)
            rows = session.scalars(query.offset((page_number - 1) * 30).limit(31)).all()
            return page(
                request,
                "incidents.html",
                incidents=rows[:30],
                more=len(rows) > 30,
                state=state,
                page_number=page_number,
            )

    @app.get("/sources")
    def sources_list(request: Request):
        with Session(engine) as session:
            sources = session.scalars(select(Connection).order_by(Connection.id)).all()
            datasets = session.scalars(
                select(Dataset).options(joinedload(Dataset.connection)).order_by(Dataset.id)
            ).all()
            return page(request, "sources.html", sources=sources, datasets=datasets)

    @app.post("/sources")
    async def add_source(request: Request):
        form = await protected_form(request)
        try:
            values = SourceFields.model_validate(
                {k: form.get(k) for k in SourceFields.model_fields}
            )
            with Session(engine) as session, session.begin():
                session.add(Connection(db_type="postgres", **values.model_dump()))
            return redirect(
                request,
                "/sources",
                "Подключение добавлено. Пароль читается из указанной переменной окружения.",
            )
        except (ValueError, IntegrityError) as exc:
            return redirect(
                request,
                "/sources",
                "Имя подключения уже занято"
                if isinstance(exc, IntegrityError)
                else error_text(exc),
            )

    @app.post("/datasets")
    async def add_dataset(request: Request):
        form = await protected_form(request)
        try:
            data = {k: (form.get(k) or None) for k in DatasetFields.model_fields}
            values = DatasetFields.model_validate(data)
            with Session(engine) as session, session.begin():
                get_or_404(session, Connection, values.connection_id)
                session.add(Dataset(**values.model_dump()))
            return redirect(request, "/sources", "Таблица зарегистрирована")
        except (ValueError, IntegrityError) as exc:
            return redirect(
                request,
                "/sources",
                "Эта таблица уже зарегистрирована"
                if isinstance(exc, IntegrityError)
                else error_text(exc),
            )

    @app.post("/sources/{source_id}/test")
    async def test_source(request: Request, source_id: int):
        await protected_form(request)
        with Session(engine) as session:
            source = get_or_404(session, Connection, source_id)
            try:

                def probe():
                    with connector_for(source, 3) as connector:
                        return connector.fetch_one("SELECT 1", {})

                await run_in_threadpool(probe)
                message = "Подключение работает: SELECT 1 выполнен"
            except Exception as exc:
                message = error_text(exc)
        return redirect(request, "/sources", message)

    @app.get("/api/demo-clock")
    def demo_clock():
        with Session(engine) as session:
            source = session.scalar(select(Connection).where(Connection.name == "tpch_warehouse"))
            if source is None:
                raise HTTPException(404, "Полигон TPC-H не зарегистрирован")
            try:
                with connector_for(source, 3) as connector:
                    value = connector.fetch_one("SELECT model_now FROM stand.clock", {})[0]
                return {"logical_now": value.strftime("%Y-%m-%dT%H:%M")}
            except Exception as exc:
                return JSONResponse({"detail": error_text(exc)}, status_code=400)

    @app.get("/health")
    def health():
        with engine.connect() as connection:
            connection.execute(select(1))
        return {"status": "ok"}

    return app
