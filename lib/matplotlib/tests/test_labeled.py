from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import warnings
from matplotlib.externals import six

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.testing.decorators import image_comparison
import UserDict


@image_comparison(baseline_images=['basic_labeled_data'])
def test_basic_labeled_data():
    fig, axes = plt.subplots(2, 2)
    # labeling with strings
    axes[0, 0].plot('x', data={'x': [3, 1, 4, 1]})
    axes[0, 1].plot([1, 2, 3, 4], 'y', data={'y': [3, 1, 4, 1]})
    # labeling with other objects
    x, y = object(), object()
    axes[1, 0].plot(x, y, data={x: [1, 2, 3, 4], y: [3, 1, 4, 1]})
    axes[1, 1].plot(x, np.asarray([3, 1, 4, 1]), data={x: np.asarray([1, 2, 3, 4])})


# A demo of how to allow plotting expressions of variables
class Expr(object):
    def __add__(self, other):
        return BinOp(lambda a, b: a + b, self, other)

    def __rmul__(self, other):
        return BinOp(lambda a, b: a * b, other, self)

    def __pow__(self, other):
        return BinOp(lambda a, b: a ** b, self, other)

    @staticmethod
    def vars(names):
        return [Var(n) for n in names.split()]


class Var(Expr):
    """Placeholder for a plottable variable"""
    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name

    def eval(self, env):
        return env[self.name]


class BinOp(Expr):
    __slots__ = ('lhs', 'rhs', 'function')

    def __init__(self, function, lhs, rhs):
        self.function, self.lhs, self.rhs = function, lhs, rhs

    def eval(self, env):
        return self.function(evaluate(self.lhs, env), evaluate(self.rhs, env))


def evaluate(expr, env):
    if isinstance(expr, Expr):
        return expr.eval(env)
    else:
        # a number or similar
        return expr


class Evaluator(UserDict.DictMixin):
    __slots__ = ('env',)

    def __init__(self, env):
        self.env = env

    def __getitem__(self, expr):
        return evaluate(expr, self.env)


@image_comparison(baseline_images=['expression_of_labels'])
def test_expression_of_labels():
    fig, axes = plt.subplots(2, 2)
    x, y, z = Expr.vars('x y z')
    data = {'x': np.arange(10),
            'y': np.array([3, 1, 4, 1, 5, 9, 2, 6, 5, 3]),
            'z': np.array([2, 7, 1, 8, 2, 8, 1, 8, 2, 8])}
    ev = Evaluator(data)

    axes[0, 0].plot(x, y, data=ev)
    axes[0, 1].plot(x, 2 * y + 1, data=ev)
    axes[1, 0].plot(x, y ** 2, data=ev)
    axes[1, 1].plot(x, 2 * y ** z, data=ev)

