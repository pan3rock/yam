"""
Tests for the rf package.

yam-runtests [-h] [-p] [-d]

-h    short help
-p    use permanent tempdir
-d    empty permanent tempdir at start
"""

from pkg_resources import resource_filename
import sys
import unittest

import matplotlib
matplotlib.use('Agg')


def run():
    if '-h' in sys.argv[1:]:
        print(__doc__)
        sys.exit()
    loader = unittest.TestLoader()
    test_dir = resource_filename('yam', 'tests')
    suite = loader.discover(test_dir)
    runner = unittest.runner.TextTestRunner()
    ret = not runner.run(suite).wasSuccessful()
    sys.exit(ret)