"""一键运行全部测试。

    python -m database_system.tests.run_all
    python -m database_system.tests.run_all -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

SUITES = [
    "database_system.tests.test_sql",
    "database_system.tests.test_storage",
    "database_system.tests.test_db",
    "database_system.tests.test_fuzz",
]


def main(argv=None) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in SUITES:
        suite.addTests(loader.loadTestsFromName(name))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
