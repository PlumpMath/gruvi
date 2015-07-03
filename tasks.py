#
# This file is part of Gruvi. Gruvi is free software available under the
# terms of the MIT license. See the file "LICENSE" that was provided
# together with this source file for the licensing terms.
#
# Copyright (c) 2012-2014 the Gruvi authors. See the file "AUTHORS" for a
# complete list.

from __future__ import absolute_import, print_function

from invoke import run, task


@task
def clean():
    run('find . -name __pycache__ | xargs rm -rf || :', echo=True)
    run('find . -name \*.so | xargs rm -f', echo=True)
    run('find . -name \*.pyc | xargs rm -f', echo=True)
    run('find . -name \*.egg-info | xargs rm -rf', echo=True)
    run('rm -rf build dist', echo=True)
    run('rm -rf docs/_build/*', echo=True)


@task(clean)
def develop():
    run('python setup.py build', echo=True)
    if develop:
        run('python setup.py develop', echo=True)
