"""Integration-level tests using the Flask test client."""


def test_app_module_does_not_enable_debug_at_import_time(app):
    # Guards the production path: gunicorn imports this module without running
    # __main__, so any module-level `app.debug = True` would expose the Werkzeug
    # debugger to anyone hitting an exception. This test locks that in.
    assert app.debug is False
