import os
import sys



def verbosity_level():
    value = os.environ.get('DEPY_DEBUG')

    if value:
        try:
            value = int(value)
        except:
            value = 1
    else:
        value = 0

    return value


def log(*args, min_level=0):
    if verbosity_level() >= min_level:
        print('DEPY: ', *args, file=sys.stderr)
