import os
import re
import json
import sys
from pathlib import Path
from subprocess import check_call, check_output, STDOUT, CalledProcessError
from functools import lru_cache
from uuid import uuid4

from .logging import log


try:
    from packaging.version import Version
except BaseException as e:
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=UserWarning)
    from distutils.version import LooseVersion as Version


CACHE_FILE_TIMEOUT_S = 60 * 30
DEFAULT_URL          = 'https://pypi.org/simple'


class PackageStorage():
    @lru_cache()
    def get_module_structure(self, location: Path, depth_max: int=None) -> dict:
        """
        Get the module structure for the path provided. If a maximum depth is provided (>=1) then the
        structure will only be determined up until that depth.

        For a directory structure:
        <location>
            first
                __init__.py
                lib1.py
            second
                lib2.py
                lib3.so
            third

        The structure will look like:
        {
            "first"         : "first/__init__.py",
            "first.__init__": "first/__init__.py",
            "first.lib1"    : "first/lib1.py",
            "second"        : "second",
            "second.lib2"   : "second.lib2.py",
            "second.lib3"   : "second/lib3.so"
        }

        Note that the 'third' subdirectory will not be included because there is nothing loadable under that path.
        """
        if not os.path.isdir(location):
            return {}

        structure = {}
        depth     = 0

        try:
            location_root = None

            for root, dirnames, filenames in os.walk(location):
                if not location_root:
                    location_root = root

                depth += 1

                if location_root == root:
                    relative_root = ''
                else:
                    relative_root = root.replace(location_root + os.path.sep, '')

                for filename in filenames:
                    self._add_file_to_structure(filename, relative_root, structure)

                for dirname in dirnames:
                    if dirname != '__pycache__' and not dirname.endswith('.dist-info'):
                        if os.path.join(relative_root, dirname).replace(os.path.sep, '.') not in structure:
                            structure[os.path.join(relative_root, dirname).replace(os.path.sep, '.')] = os.path.join(relative_root, dirname)

                if depth_max and depth == depth_max:
                    break
        except CalledProcessError as exc:
            log('Error: Unable to get the structure for', location)
            log(exc.stdout)
            log(exc.stderr)

        return structure


    def _add_file_to_structure(self, filename, relative_root, structure):
        relative_filename = os.path.join(relative_root, filename)

        if filename == '__init__.py':
            structure[relative_root.replace(os.path.sep, '.')] = relative_filename
        elif filename.endswith('.so'):
            name      = os.path.basename(filename).split('.')[0]
            parent    = os.path.dirname(relative_filename).replace(os.path.sep, '.')
            full_name = (parent + '.' if parent else '') + name
            structure[full_name] = relative_filename

        if filename.endswith('.py'):
            structure[relative_filename.replace('.py', '').replace(os.path.sep, '.')] = relative_filename


    def _write_structure(self, location):
        with open(location / '.structure', 'w') as fh:
            json.dump(self.get_module_structure(location), fh)


    def _write_dependencies(self, name, location):
        import glob

        cmd     = [sys.executable, '-m', 'pip', 'freeze', '--path', location]
        results = check_output(cmd)

        with open(location / '.packages', 'wt') as fh:
            fh.write(results.decode('utf-8'))

        self._write_structure(location)
        glob_results = glob.glob(os.path.join(location, f'*-*.dist-info'))
        final_dir    = []

        for result in glob_results:
            if name.lower().replace("-","_") in result.lower():
                final_dir.append(result)

        if len(final_dir) == 1:
            metadata = os.path.join(final_dir[0], 'METADATA')

            if os.path.isfile(metadata):
                dependencies = ''

                with open(metadata, 'rt') as fh:
                    metadata_txt = fh.readlines()

                for line in metadata_txt:
                    match = re.match(r'Requires-Dist:\s*([^\(]+?)(?:\s*\(([^\)]+)\))?\s*(;.+)?\s*$', line.strip())

                    if match:
                        dependencies += match.group(1).strip()

                        if match.group(2):
                            dependencies += match.group(2).strip()

                        if match.group(3):
                            dependencies += match.group(3).strip()

                        dependencies += "\n"
                    elif 'Requires-Dist' in line:
                        print('Unknown dependency line:', line)
                        exit(1)

                if dependencies:
                    with open(location / '.dependencies', 'wt') as fh:
                        fh.write(dependencies)


    def _pip_install(self, name, version, location):
        item = name

        if version != 'latest':
            item += '==' + version

        log('Downloading Python module', name, 'version', version, 'to', location)

        index_offset = 0
        index        = self._get_index()
        successful   = False

        while index and not successful:
            cmd = [sys.executable, '-m', 'pip', 'install']
            cmd.extend(['-I', '--no-dependencies', '--retries', '1', '--no-warn-script-location', '--no-cache-dir', '--disable-pip-version-check', '-i', index, '--target', location, item])

            try:
                check_call(cmd, stdout=sys.stderr)
                index      = None
                successful = True
            except:
                index_offset += 1
                index = self._get_index(index_offset)

        if not successful:
            return False

        try:
            self._write_dependencies(name, location)
            installed_file_name = self._get_installed_cache_file(location)

            try:
                os.unlink(installed_file_name)
            except:
                pass

            return True
        except BaseException:
            log('Error: Package version installation failed -', name, version)
            return False


    @lru_cache()
    def _python_hash(self):
        import hashlib

        sha = hashlib.sha256()
        executable = os.path.realpath(sys.executable)
        sha.update(executable.encode('utf-8'))
        return sha.hexdigest()


    def _get_index(self, offset=0):
        if offset == 0:
            return DEFAULT_URL

        other_indexes = os.environ.get('DEPY_INDEXES', '').split(';')

        if offset - 1 < len(other_indexes):
            index = other_indexes[offset - 1].strip()
            checked_indexes = set()

            for name in index.split('/'):
                if name in checked_indexes:
                    continue

                checked_indexes.add(name)
                possible_token_file = Path.home() / '.ssh' / ('pypy_' + name.lower())

                if name and possible_token_file.exists():
                    with open(possible_token_file, 'r') as fh:
                        token = fh.read().strip()

                    username = os.environ.get('DEPY_USERNAME', 'pat')

                    if index.startswith('https://'):
                        index = index.replace('https://', 'https://' + username + ':' + token + '@')
                    else:
                        index = 'username:' + token + '@' + index

                    break

            return index

        return None


    def _install(self, name, version, location):
        os.makedirs(location, mode=0o777, exist_ok=True)
        return self._pip_install(name, version, location)


    def _get_installed_cache_file(self, location: Path) -> Path:
        return location.parent.parent / '.installed'


    def _get_installed_versions(self, path_root):
        import time
        import glob

        installed_file_name = path_root / '.installed'
        pyhash              = self._python_hash()

        try:
            stats = os.stat(installed_file_name)

            if stats.st_mtime > time.time() - (CACHE_FILE_TIMEOUT_S):
                with open(installed_file_name, 'r') as fh:
                    log('Loading the install file at', installed_file_name, min_level=2)
                    loaded = json.load(fh)

                    if pyhash in loaded:
                        return loaded[pyhash]
        except:
            pass

        versions = {}
        results  = glob.glob(str(path_root / '*' / '*' / '.cached'))

        for result in results:
            log('Discovered cached version:', result, min_level=3)
            root_dir       = os.path.dirname(result)
            python_version = os.path.basename(root_dir)
            version_number = os.path.basename(os.path.dirname(root_dir))

            if python_version not in versions:
                versions[python_version] = []

            versions[python_version].append(version_number)

        try:
            tmp_name = str(installed_file_name) + '.' + uuid4().hex

            with open(tmp_name, 'w') as fh:
                log('Writing the installation cache file', tmp_name, min_level=3)
                json.dump(versions, fh)

            os.chmod(tmp_name, 777)
            os.replace(tmp_name, installed_file_name)
        except BaseException as e:
            log('Exception while writing to the installed version cache', e, min_level=1)

            try:
                os.unlink(tmp_name)
            except:
                pass

        return versions.get(pyhash, [])


    def get_available_versions(self, name: str, path_root: Path):
        import time

        try:
            available_file_name = path_root / '.available'
            stats               = os.stat(available_file_name)

            if stats.st_mtime > time.time() - (CACHE_FILE_TIMEOUT_S):
                with open(available_file_name, 'r') as fh:
                    log('Loading the available version cache file', available_file_name, min_level=2)
                    return json.load(fh)
        except:
            pass

        index_offset = 0
        index        = self._get_index()
        result       = None

        while index and not result:
            cmd = [sys.executable, '-m', 'pip', 'index', 'versions', '--disable-pip-version-check', '--pre', '-i', index, name]

            try:
                result = check_output(cmd, stderr=STDOUT).decode('utf-8').strip()
            except CalledProcessError:
                index_offset += 1
                index = self._get_index(index_offset)

        if not result:
            log('Error: Could not get the available versions for', name)
            return []

        for line in result.splitlines(keepends=False):
            log('Processing version line:', line, min_level=3)

            if line.startswith('Available versions:'):
                available = line.replace('Available versions: ', '').split(',')

                try:
                    tmp_name = str(available_file_name) + '.' + uuid4().hex

                    with open(tmp_name, 'w') as fh:
                        log('Writing available version cache', tmp_name, min_level=3)
                        json.dump(available, fh)

                    os.chmod(tmp_name, 777)
                    os.replace(tmp_name, available_file_name)
                except BaseException as e:
                    log('Exception while writing to the available version cache', e, min_level=1)

                    try:
                        os.unlink(tmp_name)
                    except:
                        pass

                return available

        return []


    def parse_requirement_spec(self, version):
        match = re.fullmatch(r'^\s*((?:any|[!~=><]+))\s*(.+)?', version)

        if match:
            op  = match.group(1)
            ver = match.group(2)

            if ver and '*' in ver:
                ver = ver.replace('*', '0')

                if op == '==':
                    op = '~='

            return {'ver': ver, 'op': op}
        else:
            return {'ver': version, 'op': 'any'}


    def compare_versions(self, version1: Version, operation: str, version2: Version) -> bool:
        match = False

        if operation == '==':
            match = version1 == version2
        elif operation == '~=':
            if version1 >= version2:
                if len(version2.release) > 2:
                    match = version2.major == version1.major and version2.minor == version1.minor
                elif len(version2.release) > 1:
                    match = version2.major == version1.major
        elif operation == '<':
            match = version1 < version2
        elif operation == '>':
            match = version1 > version2
        elif operation == '<=':
            match = version1 <= version2
        elif operation == '>=':
            match = version1 >= version2
        elif operation == '!=':
            match = version1 != version2
        else:
            log('Error: Unknown Requirements Operation -', operation)
            exit(1)

        return match


    def _match_py_requirements(self, requirements, versions):
        requirement_specs = list(map(self.parse_requirement_spec, requirements.split(',')))
        matching_reqs     = set()

        for version in versions:
            try:
                ver = Version(version)
            except:
                continue

            for req in requirement_specs:
                match = False

                if req['op'] == 'any':
                    match = True
                else:
                    req_ver = Version(req['ver'])
                    match   = self.compare_versions(ver, req['op'], req_ver)

                if match:
                    matching_reqs.add(version)
                else:
                    if version in matching_reqs:
                        matching_reqs.remove(version)

                    break

        return list(matching_reqs)


    def _get_proper_version(self, spec, versions):
        matching_versions = self._match_py_requirements(spec, versions)

        if not matching_versions:
            return None

        return sorted(matching_versions, key=Version)[-1]


    def _quick_resolve_version(self, version):
        """
        If we only have a single requirement and it's just '==', then we don't have to perform
        additional processing, we know that's the desired version
        """
        if ',' not in version:
            specs = self.parse_requirement_spec(version)

            if specs['op'] == '==':
                log('Quickly resolved', version, 'to', specs['ver'], min_level=2)
                return specs['ver']

        return None


    def get_cache_version(self, name: str, spec: str, path_root: str):
        version = self._quick_resolve_version(spec)

        if not version:
            version = self._get_proper_version(spec, self._get_installed_versions(path_root))

            if not version:
                version = self._get_proper_version(spec, self.get_available_versions(name, path_root))

                if not version:
                    log('No matching versions found for', name, spec, min_level=2)
                    return None

        log('Resolved version', name, spec, ' - ', version, min_level=2)
        return version.strip()


    def cache(self, path_root: Path, name: str, spec: str) -> Path:
        path_root     = path_root / name.lower()
        cache_version = self.get_cache_version(name, spec, path_root)
        log('Caching', name, 'spec', '-', cache_version, min_level=1)

        if not cache_version:
            return None

        cache_location = path_root / cache_version / self._python_hash()

        if self.is_cached(cache_location):
            return cache_location

        if cache_location.exists():
            remove_tree(cache_location)

        temp_location = path_root / cache_version / ('.' + self._python_hash() + '_' + uuid4().hex)
        log('Temporary cache location:', temp_location, min_level=2)
        os.umask(0)

        if self._install(name, cache_version, temp_location):
            log('Install completed successfully', min_level=1)

            try:
                Path.touch(temp_location / '.cached')
                chmod(temp_location, 0o555)
                # Make the root of the cache writable so that we can create and maintain the available versions
                os.chmod(temp_location, 0o777)
                os.rename(temp_location, cache_location)
            except:
                pass

        try:
            if temp_location.exists():
                remove_tree(temp_location)
        except:
            pass

        if self.is_cached(cache_location):
            return cache_location

        return None


    def is_cached(self, path: Path) -> bool:
        return (path / '.cached').exists()


    def cache_requirements(self, cache_location: Path, dependencies: dict) -> Path:
        if self.is_cached(cache_location):
            return cache_location

        if cache_location.exists():
            remove_tree(cache_location)

        basename = '.' + str(os.path.basename(cache_location)) + '_' + uuid4().hex
        temp_location = cache_location.parent / basename
        os.makedirs(temp_location, mode=0o777, exist_ok=True)
        log('Temporary requirements cache location:', temp_location, min_level=2)
        os.umask(0)

        with open(temp_location / 'resolution', 'w') as fh:
            json.dump(dependencies, fh)

        try:
            Path.touch(temp_location / '.cached')
            chmod(temp_location, 0o555)
            # Make the root of the cache writable so that we can create rename it
            os.chmod(temp_location, 0o777)
            os.rename(temp_location, cache_location)
            os.chmod(cache_location, 0o555)
        except:
            pass

        try:
            if temp_location.exists():
                remove_tree(temp_location)
        except:
            pass

        if self.is_cached(cache_location):
            return cache_location

        return None



def remove_tree(cache_location):
    try:
        import shutil

        log('Removing the existing cache location', min_level=1)
        try:
            chmod(cache_location, 0o777)
        except:
            pass

        shutil.rmtree(cache_location)
    except:
        log('Error: Unable to remove the existing cache location', cache_location)


def chmod(path: Path, mode: int):
    for root, dirs, files in os.walk(path):
        # set perms on files
        for file in files:
            os.chmod(os.path.join(root, file), mode)

    os.chmod(path, mode)
    os.getcwd
