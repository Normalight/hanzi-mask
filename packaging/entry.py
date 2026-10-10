"""Frozen bootstrap; handle worker dispatch before importing Qt."""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    if sys.argv[1:] == ['--smoke-test']:
        from smoke_test import smoke_test
        smoke_test()
    else:
        from app import main
        raise SystemExit(main())
