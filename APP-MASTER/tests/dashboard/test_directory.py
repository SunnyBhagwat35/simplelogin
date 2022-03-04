from flask import url_for

from app.config import MAX_NB_DIRECTORY
from app.models import Directory
from tests.utils import login


def test_create_directory(flask_client):
    login(flask_client)

    r = flask_client.post(
        url_for("dashboard.directory"),
        data={"form-name": "create", "name": "test"},
        follow_redirects=True,
    )

    assert r.status_code == 200
    assert f"Directory test is created" in r.data.decode()
    assert Directory.get_by(name="test") is not None


def test_delete_directory(flask_client):
    """cannot add domain if user personal email uses this domain"""
    user = login(flask_client)
    directory = Directory.create(name="test", user_id=user.id, commit=True)

    r = flask_client.post(
        url_for("dashboard.directory"),
        data={"form-name": "delete", "dir-id": directory.id},
        follow_redirects=True,
    )

    assert r.status_code == 200
    assert f"Directory test has been deleted" in r.data.decode()
    assert Directory.get_by(name="test") is None


def test_create_directory_in_trash(flask_client):
    user = login(flask_client)

    directory = Directory.create(name="test", user_id=user.id, commit=True)

    # delete the directory
    r = flask_client.post(
        url_for("dashboard.directory"),
        data={"form-name": "delete", "dir-id": directory.id},
        follow_redirects=True,
    )
    assert Directory.get_by(name="test") is None

    # try to recreate the directory
    r = flask_client.post(
        url_for("dashboard.directory"),
        data={"form-name": "create", "name": "test"},
        follow_redirects=True,
    )

    assert r.status_code == 200
    assert "test has been used before and cannot be reused" in r.data.decode()


def test_create_directory_out_of_quota(flask_client):
    user = login(flask_client)

    for i in range(MAX_NB_DIRECTORY):
        Directory.create(name=f"test{i}", user_id=user.id, commit=True)

    assert Directory.count() == MAX_NB_DIRECTORY

    flask_client.post(
        url_for("dashboard.directory"),
        data={"form-name": "create", "name": "test"},
        follow_redirects=True,
    )

    # no new directory is created
    assert Directory.count() == MAX_NB_DIRECTORY
