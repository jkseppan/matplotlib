#!/usr/bin/env python
#
# This allows running the matplotlib tests from the command line: e.g.
# python tests.py -v -d
# See http://somethingaboutorange.com/mrl/projects/nose/1.0.0/usage.html
# for options.

import os

import matplotlib
matplotlib.use('agg')
matplotlib.verbose.set_level('debug-annoying')

import nose
from matplotlib.testing.noseclasses import KnownFailure
from matplotlib import default_test_modules
from matplotlib.transforms import *
from numpy import *

def test_transforms():
    idt = IdentityTransform()
    print 'idt', idt
    bl_idt = TransformWrapper(BlendedAffine2D(idt,idt))
    print 'bl_idt', bl_idt
    eighty = Affine2D(array([[80., 0., 0.], [0., 80., 0.], [0., 0., 1.]]))
    print 'eighty', eighty
    bb = lambda x: Bbox(array(x))
    tbb = lambda x, y: TransformedBbox(bb(x), y)
    tbbfrom = tbb([[0.,0.],[1.,1.]], bl_idt)
    print 'tbbfrom', tbbfrom
    tbbto = tbb([[0.125, 0.1], [0.9, 0.9]], BboxTransformTo(tbb([[0., 0.], [8., 6.]], eighty)))
    print 'tbbto', tbbto
    cgt = CompositeGenericTransform
    bboxt = cgt(bl_idt, cgt(BboxTransformFrom(tbbfrom), BboxTransformTo(tbbto)))
    print 'bboxt', bboxt
    points_inverted = bboxt.inverted().transform([[80.,48.],[5040.,3888.]])
    print 'points_inverted', points_inverted


def run():
    try:
        nose.main(addplugins=[KnownFailure()],
                  defaultTest=default_test_modules)
    except SystemExit as e:
        # When running in TRAVIS, we want to write the failed images
        # out to the log.
        if e.code == 0 or 'TRAVIS' not in os.environ:
            raise

        import base64
        import io
        import tarfile
        output = io.BytesIO()

        tar = tarfile.open(fileobj=output, mode="w|bz2")
        for root, dirs, files in os.walk('.'):
            for file in files:
                if os.path.splitext(file)[0].endswith('-failed-diff'):
                    path = os.path.join(root, file)
                    tar.add(path)
                    path = path.replace('-failed-diff', '')
                    tar.add(path)
        tar.close()

        print(
            "\nThe following is a base64-encoded tar.bz2 file containing the\n"
            "failed images from the test run.  Use the\n"
            "get_travis_results.py script to download it from Travis and\n"
            "extract it.\n\n"
            ">>>>>>>>TARBALL>>>>>>>>\n")

        print(base64.b64encode(output.getvalue()))

        raise

if __name__ == '__main__':
    test_transforms()
    run()

