from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import warnings
from matplotlib.externals import six

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.testing.decorators import image_comparison


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



