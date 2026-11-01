# workshop

A hand-written FastAPI application for the adoption tests of FastAPI Architect (v2.2, increment 4): an application
factory (`create_app`) that mounts two routers under `/api`, module-level dictionaries as storage, a small service
module and a dependency module that an adopted router may be extended with. Formatting and comments are
deliberately idiosyncratic: a transformation must leave every byte it was not authorised to touch exactly as it is.
Run with `uvicorn app.main:app`.
