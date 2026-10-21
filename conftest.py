"""Select suites before importing historical test modules."""
from pathlib import Path
import os
import pytest
from test_suite_inventory import SUITES
ROOT = Path(__file__).resolve().parent

def pytest_addoption(parser):
    parser.addoption('--suite', choices=tuple(SUITES), default='current', help='Explicit test collection scope')

def pytest_configure(config):
    if not config.option.collectonly and not os.environ.get('V2_OFFLINE_RUN_ROOT'):
        raise pytest.UsageError('Use python -B scripts/v2_offline_tests.py for isolated offline execution')
    # Initial paths bypass pytest_ignore_collect in pytest. Check explicit files
    # and node IDs before importing them, including historical test helpers.
    for argument in config.args:
        path = Path(str(argument).split('::', 1)[0]).resolve()
        try:
            relative = path.relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if relative.startswith('tests/') and path.name.startswith('test_') and path.suffix=='.py' and relative not in SUITES['all']:
            raise pytest.UsageError('Unclassified test module: '+relative+'; update test_suite_inventory.py')
        if relative in SUITES['all'] and relative not in SUITES[config.getoption('--suite')]:
            raise pytest.UsageError(relative+' is outside --suite='+config.getoption('--suite'))

def pytest_ignore_collect(collection_path, config):
    path = Path(collection_path)
    try:
        relative = path.relative_to(ROOT).as_posix()
    except ValueError:
        return None  # External launcher probes are allowed.
    if not relative.startswith('tests/') or not path.name.startswith('test_') or path.suffix != '.py':
        return None
    if relative not in SUITES['all']:
        raise pytest.UsageError('Unclassified test module: '+relative+'; update test_suite_inventory.py')
    return relative not in SUITES[config.getoption('--suite')]
