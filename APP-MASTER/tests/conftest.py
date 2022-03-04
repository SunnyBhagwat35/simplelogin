import os

# use the tests/test.env config fle
# flake8: noqa: E402

os.environ["CONFIG"] = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests/test.env")
)
import sqlalchemy

from app.db import Session, engine, connection
from app.models import Base

from psycopg2 import errors
from psycopg2.errorcodes import DEPENDENT_OBJECTS_STILL_EXIST

import pytest

from server import create_app
from init_app import add_sl_domains

app = create_app()
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False
app.config["SERVER_NAME"] = "sl.test"

# enable pg_trgm extension
with engine.connect() as conn:
    try:
        conn.execute("DROP EXTENSION if exists pg_trgm")
        conn.execute("CREATE EXTENSION pg_trgm")
    except sqlalchemy.exc.InternalError as e:
        if isinstance(e.orig, errors.lookup(DEPENDENT_OBJECTS_STILL_EXIST)):
            print(">>> pg_trgm can't be dropped, ignore")
        conn.execute("Rollback")

Base.metadata.create_all(engine)

add_sl_domains()


@pytest.fixture
def flask_app():
    yield app


@pytest.fixture
def flask_client():
    transaction = connection.begin()

    with app.app_context():
        try:
            client = app.test_client()
            yield client
        finally:
            # roll back all commits made during a test
            transaction.rollback()
            Session.rollback()
            Session.close()
