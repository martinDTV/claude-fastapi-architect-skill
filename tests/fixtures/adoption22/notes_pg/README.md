# notes_pg

A hand-written FastAPI application with SQLAlchemy (async) and Alembic, for the adoption tests of FastAPI Architect
(v2.2, increment 4, scenario B). One table (`notes`), one revision (`0001_notes`), a declarative base and a session
dependency in `app/db.py`, an Alembic environment under `alembic/` that targets `Base.metadata`. The database URL
comes from `NOTES_DATABASE_URL`. The tests of the project itself do not need a database (they check the application
and its routes); the migrations run with `uv run --group dev alembic upgrade head`.
