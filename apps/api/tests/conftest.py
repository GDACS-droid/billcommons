"""Shared pytest fixtures for API contract tests.

These tests run against the live Railway Postgres DB (0001 schema applied,
mostly-empty tables). Tests must be empty-DB tolerant: assert response
shape/status/pagination math, never row counts.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from billcommons_api.app import create_app


@pytest.fixture(scope="session")
def app():
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c
