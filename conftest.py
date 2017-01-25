from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from six.moves import reduce
import inspect
import os
import pytest
import unittest

import matplotlib
matplotlib.use('agg')

from matplotlib import default_test_modules
from matplotlib.testing import _conversion_cache as ccache


IGNORED_TESTS = {
    'matplotlib': [],
}


def blacklist_check(path):
    """Check if test is blacklisted and should be ignored"""
    head, tests_dir = os.path.split(path.dirname)
    if tests_dir != 'tests':
        return True
    head, top_module = os.path.split(head)
    return path.purebasename in IGNORED_TESTS.get(top_module, [])


def whitelist_check(path):
    """Check if test is not whitelisted and should be ignored"""
    left = path.dirname
    last_left = None
    module_path = path.purebasename
    while len(left) and left != last_left:
        last_left = left
        left, tail = os.path.split(left)
        module_path = '.'.join([tail, module_path])
        if module_path in default_test_modules:
            return False
    return True


COLLECT_FILTERS = {
    'none': lambda _: False,
    'blacklist': blacklist_check,
    'whitelist': whitelist_check,
}


def is_nose_class(cls):
    """Check if supplied class looks like Nose testcase"""
    return any(name in ['setUp', 'tearDown']
               for name, _ in inspect.getmembers(cls))


def pytest_addoption(parser):
    group = parser.getgroup("matplotlib", "matplotlib custom options")

    group.addoption('--collect-filter', action='store',
                    choices=COLLECT_FILTERS, default='blacklist',
                    help='filter tests during collection phase')

    group.addoption('--no-pep8', action='store_true',
                    help='skip PEP8 compliance tests')
    group.addoption("--conversion-cache-max-size", action="store",
                    help="conversion cache maximum size in bytes")
    group.addoption("--conversion-cache-report-misses",
                    action="store_true",
                    help="report conversion cache misses")


def pytest_configure(config):
    matplotlib._called_from_pytest = True
    matplotlib._init_tests()

    if config.getoption('--no-pep8'):
        IGNORED_TESTS['matplotlib'] += 'test_coding_standards'
    max_size = config.getoption('--conversion-cache-max-size')
    if max_size is not None:
        ccache.conversion_cache = \
            ccache.ConversionCache(max_size=int(max_size))
    else:
        ccache.conversion_cache = ccache.ConversionCache()
    if config.pluginmanager.hasplugin('xdist'):
        config.pluginmanager.register(DeferPlugin())


def pytest_unconfigure(config):
    ccache.conversion_cache.expire()
    matplotlib._called_from_pytest = False


def pytest_sessionfinish(session):
    if hasattr(session.config, 'slaveoutput'):
        session.config.slaveoutput['cache-report'] = ccache.conversion_cache.report()


def pytest_terminal_summary(terminalreporter):
    tr = terminalreporter
    if hasattr(tr.config, 'cache_reports'):
        reports = tr.config.cache_reports
        data = {'hits': reduce(lambda x, y: x.union(y),
                               (rep['hits'] for rep in reports)),
                'gets': reduce(lambda x, y: x.union(y),
                               (rep['gets'] for rep in reports))}
    else:
        data = ccache.conversion_cache.report()
    tr.write_sep('=', 'Image conversion cache report')
    tr.write_line('Hit rate: %d/%d' % (len(data['hits']), len(data['gets'])))
    if tr.config.getoption('--conversion-cache-report-misses'):
        tr.write_line('Missed files:')
        for filename in sorted(data['gets'].difference(data['hits'])):
            tr.write_line('  %s' % filename)


def pytest_ignore_collect(path, config):
    if path.ext == '.py':
        collect_filter = config.getoption('--collect-filter')
        return COLLECT_FILTERS[collect_filter](path)


def pytest_pycollect_makeitem(collector, name, obj):
    if inspect.isclass(obj):
        if is_nose_class(obj) and not issubclass(obj, unittest.TestCase):
            # Workaround unittest-like setup/teardown names in pure classes
            setup = getattr(obj, 'setUp', None)
            if setup is not None:
                obj.setup_method = lambda self, _: obj.setUp(self)
            tearDown = getattr(obj, 'tearDown', None)
            if tearDown is not None:
                obj.teardown_method = lambda self, _: obj.tearDown(self)
            setUpClass = getattr(obj, 'setUpClass', None)
            if setUpClass is not None:
                obj.setup_class = obj.setUpClass
            tearDownClass = getattr(obj, 'tearDownClass', None)
            if tearDownClass is not None:
                obj.teardown_class = obj.tearDownClass

            return pytest.Class(name, parent=collector)


class DeferPlugin(object):
    def pytest_testnodedown(self, node, error):
        if not hasattr(node.config, 'cache_reports'):
            node.config.cache_reports = []
        node.config.cache_reports.append(node.slaveoutput['cache-report'])
