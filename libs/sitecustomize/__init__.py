import os


if 'DEPY_REQS' in os.environ and not os.environ.get('DEPY_DISABLE'):
    import sys
    from .injector import DepyInjectorFinder

    input_reqs = os.environ['DEPY_REQS']
    del os.environ['DEPY_REQS']

    # Remove this from the python path so it's not forced onto future processes that may be spawned from this
    if os.environ.get('PYTHONPATH'):
        os.environ['PYTHONPATH'] = os.environ['PYTHONPATH'].replace(os.path.abspath(os.path.dirname(os.path.realpath(__file__)) + '/../'), '').replace('::', ':').strip(':')

    # Add the finder as the first item in the meta path
    sys.meta_path.insert(0, DepyInjectorFinder(input_reqs))
