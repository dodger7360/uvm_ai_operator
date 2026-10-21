"""Install guards in Python children before application imports."""
import os

if os.environ.get('V2_OFFLINE_RUN_ROOT'):
    try:
        from offline_guard import install
        install()
    except BaseException:
        # Python ignores failed sitecustomize imports; fail closed instead.
        os._exit(86)
