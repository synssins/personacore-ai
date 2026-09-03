"""One module per admin-UI screen (ADR-0020).

Each module here exposes ``register(router, ctx)`` and registers its routes on
the router the factory built - **not** on a router of its own that gets
included. That is deliberate: ``require_user`` is attached to that one router,
and a screen that built its own would be a second place for the guard to be
forgotten. It also keeps every route's real path and dependencies on the route
object itself, which is what ``tests/server/test_no_dead_controls.py`` walks.
"""
