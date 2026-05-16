"""Integration-level tests using the Flask test client."""


def test_app_module_does_not_enable_debug_at_import_time(app):
    # Guards the production path: gunicorn imports this module without running
    # __main__, so any module-level `app.debug = True` would expose the Werkzeug
    # debugger to anyone hitting an exception. This test locks that in.
    assert app.debug is False


def test_pending_downloads_is_capped(app_mod):
    # The download stash must use EphemeralStore so memory can't grow unbounded.
    from slack_channel_export_selfservice_1 import EphemeralStore
    assert isinstance(app_mod._pending_downloads, EphemeralStore)


def test_rejoin_sessions_is_capped(app_mod):
    from slack_channel_export_selfservice_1 import EphemeralStore
    assert isinstance(app_mod._rejoin_sessions, EphemeralStore)
