from app.db import Base, SyncLocal, engine

session = SyncLocal()                                  # test module global: reported, downgraded


def test_schema() -> None:
    Base.metadata.create_all(engine)                   # create_all in tests: not reported
