"""Triage backends by architecture. Dispatch via TOCIFY_BACKEND; add new backends by registering here."""

import os


def _openai_backend():
    from integrations import openai_triage

    client = openai_triage.make_openai_client()
    return lambda interests, items: openai_triage.call_openai_triage(client, interests, items)


def _cursor_backend():
    from integrations import cursor_cli

    if not cursor_cli.is_available():
        raise RuntimeError("Cursor backend requested but CURSOR_API_KEY is not set.")
    return cursor_cli.call_cursor_triage


# Registry: TOCIFY_BACKEND value -> callable that returns (interests, items) -> dict
_BACKENDS = {
    "openai": _openai_backend,
    "cursor": _cursor_backend,
}


def get_triage_backend():
    """Return a callable (interests, items) -> dict with keys notes, ranked (and optionally week_of)."""
    backend = os.getenv("TOCIFY_BACKEND", "").strip().lower()
    if not backend:
        backend = "cursor" if os.getenv("CURSOR_API_KEY", "").strip() else "openai"
    if backend not in _BACKENDS:
        raise RuntimeError(
            f"Unknown TOCIFY_BACKEND={backend!r}. Known: {list(_BACKENDS)}. "
            "Set OPENAI_API_KEY or CURSOR_API_KEY for default backend."
        )
    return _BACKENDS[backend]()
