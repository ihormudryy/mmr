"""Manual smoke-test helper for pycron: a long-running dummy job used to
exercise pycron's job-supervision behavior (start_on_pycron_start,
restart_if_found, restart_if_finished) end-to-end against a real process.

Not a pytest suite (no test_* functions) -- it is a standalone script meant
to be run directly (`python3 scripts/test_pycron.py`) as a pycron job target.
The sleep/print body is guarded behind `__main__` so that pytest, which
collects any `test_*.py` path handed to it on the command line (including
this one, per G0 Task 5's `pytest tests/test_compose_topology.py
scripts/test_pycron.py` invocation), can import this module for collection
without tripping the 61-120s sleep as a side effect of import.
"""
import random
import time


def main():
    rand_int = random.randint(61, 120)
    print('test_pycron! waiting {} secs'.format(str(rand_int)))
    time.sleep(rand_int)


if __name__ == '__main__':
    main()
