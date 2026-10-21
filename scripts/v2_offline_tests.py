"""Run the current suite with per-run SQLite/network/credential isolation."""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / 'scripts' / 'offline_test_support'


def main(arguments):
    os.chdir(ROOT)
    sys.path[:0] = [str(SUPPORT), str(ROOT)]
    from offline_guard import install, scrub_environment
    scrub_environment()
    inherited = os.environ.get('V2_OFFLINE_RUN_ROOT')
    parent = Path(inherited).resolve(strict=True) if inherited else None
    with tempfile.TemporaryDirectory(prefix='steel-v2-offline-', dir=parent) as directory:
        run_root = Path(directory).resolve()
        requested = None
        for index, arg in enumerate(arguments):
            if arg.startswith('--basetemp='):
                requested = arg.split('=', 1)[1]
            elif arg == '--basetemp':
                if index + 1 == len(arguments):
                    raise ValueError('--basetemp requires a directory')
                requested = arguments[index + 1]
        if requested:
            base = Path(requested).resolve()
            # pytest deletes basetemp. Reject arbitrary existing/user directories
            # before entering pytest, and never grant an ancestor directory.
            if parent is None or not base.is_relative_to(parent) or base == parent or base.exists():
                raise ValueError('--basetemp must be a new directory inside the inherited offline run')
            base.mkdir(parents=True)
            run_root = base
        os.environ['V2_OFFLINE_RUN_ROOT'] = str(run_root)
        os.environ['PYTHONPATH'] = os.pathsep.join([str(SUPPORT), str(ROOT)])
        install()
        import pytest
        args = ['-q', '-p', 'no:cacheprovider', *arguments]
        if requested is None:
            args += ['--basetemp=' + str(run_root / 'pytest')]
        return pytest.main(args)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
