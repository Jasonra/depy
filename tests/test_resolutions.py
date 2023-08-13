import os
import sys
from pathlib import Path
from glob import glob
import pytest
import shutil
import json


TEST_PATH = Path(os.path.realpath(__file__)).parent
sys.path.insert(0, str(TEST_PATH.parent))
os.environ['DEPY_CACHE_PATH']   = str(TEST_PATH / '.tmp')
os.environ['DEPY_BYPASS_CACHE'] = '1'

from libs.sitecustomize.injector        import DepyInjectorFinder
from libs.sitecustomize.package_storage import remove_tree



def test_resolutions():
    path_list = glob(str(TEST_PATH / 'resolutions' / '*'))

    for path in sorted(path_list, reverse=True):
        clean_cache_dir()

        try:
            injector = DepyInjectorFinder(Path(path) / 'requirements.txt')
        except:
            assert False, os.path.basename(path) + ' resolution - Unable to process requirements'

        comparison = load_comparison(Path(path) / 'comparison.txt')
        compare_results(injector.lib_metadata, comparison, os.path.basename(path) + ' resolution')


def load_comparison(path: Path):
    with open(path, 'r') as fh:
        return json.load(fh)


def compare_results(initial, comparison, test_name):
    processed = set()

    for item in initial:
        assert item.lower() in comparison, test_name + ' contains the requirement ' + item
        assert initial[item]['version'] == comparison[item.lower()], test_name + ' versions are the same'
        processed.add(item.lower())

    for item in comparison:
        assert item in processed, test_name + ' requirement has been handled'


def clean_cache_dir():
    if os.path.exists(os.environ['DEPY_CACHE_PATH']):
        remove_tree(os.environ['DEPY_CACHE_PATH'])
