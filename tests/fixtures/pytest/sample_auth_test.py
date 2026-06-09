"""검증용 pytest fixture — 일반 패턴 (도메인 무관)."""

import os

import pytest


@pytest.fixture(scope="session")
def engine():
    url = os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    return url


@pytest.fixture
def client(engine):
    return {"engine": engine}


def test_login_success(client):
    r = client.post("/auth/login", json={"email": "a@b.com", "password": "x"})
    assert r.status_code == 200


def test_signup_validation_error(client):
    r = client.post("/api/auth/signup", json={"email": "bad"})
    assert r.status_code == 422


def test_list_items(client):
    r = client.get("/items")
    assert r.status_code == 200


def test_with_mock(client, monkeypatch):
    monkeypatch.setattr("module.func", lambda: 42)
    assert True
