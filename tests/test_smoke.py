"""Smoke coverage for the empty backend package."""

import importlib


def test_backend_imports():
    backend = importlib.import_module("backend")

    assert backend.__name__ == "backend"
