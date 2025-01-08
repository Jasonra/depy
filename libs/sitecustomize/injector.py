import os
import sys
from pathlib import Path
import time
import importlib.abc
import importlib.machinery
import importlib.util
from uuid import uuid4
import json
from copy import deepcopy
import hashlib
from functools import cmp_to_key

try:
    from packaging.version import Version
except BaseException as e:
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=UserWarning)
    from distutils.version import LooseVersion as Version

# This is handled differently in different versions of Python
try:
    from importlib.metadata import PathDistribution
except:
    from importlib_metadata import PathDistribution

from .logging import log, verbosity_level
from .package_storage import PackageStorage
from .pip_requirements_parser import RequirementsFile



STORAGE_NAME             = 'depy_v1'
STORAGE_ROOT             = Path(os.environ['DEPY_CACHE_PATH']) if os.environ.get('DEPY_CACHE_PATH') else Path.home() / '.local' / STORAGE_NAME
STORAGE_PATH_REQS        = STORAGE_ROOT / 'requirements'
STORAGE_PATH_PACKAGES    = STORAGE_ROOT / 'packages'
START_TIME               = time.time()
CONFLICT_RESOLUTION_MODE = os.environ.get('DEPY_MODE', 'strict')


def profile(func):
    if os.environ.get('DEPY_PROFILE'):
        if 'data' not in profile.__dict__:
            profile.data = {}

        def wrap(*args, **kwargs):
            if func.__name__ not in profile.data:
                profile.data[func.__name__] = {'calls': 0, 'time': 0}

            start  = time.time()
            result = func(*args, **kwargs)
            profile.data[func.__name__]['time']  += time.time() - start
            profile.data[func.__name__]['calls'] += 1
            return result

        return wrap

    return func


def print_profile_data():
    if 'data' not in profile.__dict__:
        return

    entries_in_order = sorted(profile.data, key=lambda k: profile.data[k]['time'], reverse=True)
    print('%-30s %-14s %-12s %-10s' % ('Function |', 'Number Calls |', 'Total Time |', 'Time per call'))

    for entry in entries_in_order:
        if profile.data[entry]['calls']:
            print('%-30s %-14s %-12s %-10s' % (entry, profile.data[entry]['calls'], round(profile.data[entry]['time'], 2), round((profile.data[entry]['time'] / profile.data[entry]['calls']), 2)))



class DepyInjectorFinder(importlib.abc.MetaPathFinder):
    """
    Cache and use resolved python requirements on the fly.

    When starting, it looks for load the provided 'requirements' file. It will then extract all requirements from those files in the form
    of a library name, a spec, and a marker.

    mysql-connector-python~=9.0.0; python_version>='3.0'
    ^---------------------^^-----^ ^-------------------^
    lib    spec         marker

    The spec and marker are optional components. Once the requirements have been extracted, they will resolved and cached.
    This will resolve the provided library and version specs into the most appropriate version based on what's installed and
    what is available.

    Once a set of libraries is resolved, their dependencies are determined and resolved again. This process continues to happen until
    all requirements and dependencies have been finalized. There are three modes of operation when it comes to conflicts:
        1. newest - If conflicts are found, then the specs are ordered based on the version number such that the largest versions are
            first. From that point, each spec is removed from the end of the list until either a resolution is found or there is only a
            single spec remaining.
        2. strict - Conflicts are not allowed and the process will error out if any are found.
        3. legacy - This emulates the legacy pip functionality, where conflicts roll back to the requirements based on the order that
            they have been seen.

    The complete set of resolved libraries and versions is then cached so that the resolution process can by bypassed the next time this
    same set of libraries is used.

    Once we have the complete set of libraries and versions, along with the directory that they are installed in, we gather the
    structures of them into a single dictionary. This overall structure will point from module names to the directory and file
    that contains that module. This is to prevent looking through the available directories and instead just "know" where the
    module is that we want to use.
    """

    library_mods = {}


    def __init__(self, requirements):
        self.sys_path_marker   = '/' + uuid4().hex
        sys.path.insert(0, self.sys_path_marker)
        self.ignored_sys_path  = self._setup_ignored_sys_paths()

        # Uniquify sys path
        sys.path = sorted(set(sys.path), key=sys.path.index)

        if isinstance(requirements, Path):
            requirements = str(requirements)

        log('Injector loaded:', requirements, min_level=1)
        self.requirements      = requirements
        self.storage           = PackageStorage()
        self.complete_reqs     = {}     # The complete set of requirements and their locations, updated as needed over time
        self.reqs_by_path      = {}     # Already processed system paths
        self.lib_metadata      = {}     # Used to find distribution data
        self.loadable_files    = {}     # Loadable files keyed by path
        self.bad_paths         = set()  # System paths that could not be analyzed
        self.resolved_reqs     = self._load_requirements()

        if 'DEPY_FORCEDLIBS' in os.environ:
            forced_paths = os.environ['DEPY_FORCEDLIBS'].split(':')
            sys.path     = forced_paths + sys.path

        self._add_requirements_to_sys_path(self.resolved_reqs, self.ignored_sys_path)


    @profile
    def _add_requirements_to_sys_path(self, resolved_reqs, ignored_paths):
        """
        Add all resolved requirements to the end of the system path so that they can be found in case any process is specificially
        referencing sys.path. This also adds them to the ignored paths so that we don't deal with them in a second way.
        """
        requirement_roots = set()
        path_additions    = set()

        for req, path in sorted(resolved_reqs.items(), key=lambda x: x[1]):
            directory = os.path.dirname(path)
            paths     = req.count('.')

            if path.endswith('__init__.py') and not req.endswith('__init__'):
                paths += 1

            for _ in range(paths):
                directory = os.path.dirname(directory)

            requirement_roots.add(directory)
            binary_dir = os.path.join(directory, 'bin')

            if binary_dir not in path_additions and os.path.isdir(binary_dir):
                log('Found binary directory', binary_dir, min_level=2)
                os.environ['PATH'] += ':' + binary_dir
                path_additions.add(binary_dir)

        if os.environ.get('DEPY_ADD_PP', '1') == '1':
            os.environ['PYTHONPATH'] += ':'.join(requirement_roots)

        sys.path.extend(requirement_roots)
        ignored_paths.update(requirement_roots)


    def __del__(self):
        print_profile_data()


    @profile
    def _setup_ignored_sys_paths(self):
        """
        Pre-filter system paths to make sure that we only look through valid directories
        """
        ignored = set()

        for location in sys.path:
            if location == self.sys_path_marker or not os.path.isdir(location):
                ignored.add(location)

        return ignored


    @profile
    def find_distributions(self, context):
        context_name = context.name.lower().replace("-","_") if context.name else None

        if context_name in self.complete_reqs:
            location = self.complete_reqs[context_name]

            if location.endswith('.so') or location.endswith('.py'):
                location = os.path.dirname(location)

            location = os.path.dirname(location)


            class LocalDistribution(PathDistribution):
                def __init__(self, name, path, version):
                    self.stored_metadata = {
                        'name':    name,
                        'path':    path,
                        'version': version
                    }
                    super().__init__(Path(path))

                @property
                def name(self):
                    return self.stored_metadata['name']

                @property
                def version(self):
                    if self.stored_metadata['version']:
                        return self.stored_metadata['version']
                    else:
                        return super().version

            real_version = None

            if context_name in self.resolved_reqs and self.resolved_reqs[context_name].startswith(location):
                if context_name in self.lib_metadata:
                    real_version = self.lib_metadata[context_name]['version']
                else:
                    for module_name, values in self.lib_metadata.items():
                        if str(values['path']) == location:
                            self.lib_metadata[context_name] = self.lib_metadata[module_name]
                            real_version = self.lib_metadata[context_name]['version']
                            break

            return [LocalDistribution(context.name, location, real_version)]

        return []


    @profile
    def find_spec(self, fullname, path, target=None):
        """
        This is automatically called by Python because it is a finder. This will pass in the full name of the desired module as
        well as an optional path if the root of the module has already been loaded.

        This function will use the stored structure of all libraries and return the appropriate location for the module. If it
        is a module that we don't have in the structure, we attempt to find it and then store it as well so that we have it for
        later reference.
        """

        log('Finding spec for', fullname, path, target, min_level=3)

        # Since we load system paths on the fly, we always need to look there to see if a system path contains what we want
        self._find_sys_path_file(fullname)

        if fullname not in self.complete_reqs and '.' in fullname:
            self._find_path_file(fullname, path)

        if fullname not in self.complete_reqs:
            self._find_appropriate_dir(fullname)

        if fullname in self.complete_reqs:
            resolved_req = self.complete_reqs.get(fullname)

            if resolved_req and self._is_loadable(resolved_req):
                log('Loading spec for', fullname, path, target, resolved_req, min_level=3)
                return importlib.util.spec_from_file_location(fullname, resolved_req)
            else:
                log('Loading namespace for', fullname, path, target, min_level=3)
                return importlib.util.spec_from_loader(name=fullname, loader=None, is_package=True)

        log('No spec for', fullname, path, target, min_level=2)
        return None


    @profile
    def _get_loadable_file(self, existing_path, module):
        """
        Find any "loadable" files in a given path with a given module name.
        These loadable files are either .so, .py, or __init__.py files.
        """
        if existing_path not in self.loadable_files:
            self.loadable_files[existing_path] = {}
            structure = self.storage.get_module_structure(existing_path, depth_max=1)

            for entry in structure:
                complete_path = os.path.join(existing_path, structure[entry] or entry)

                if not self._is_loadable(complete_path):
                    if os.path.isfile(os.path.join(complete_path, '__init__.py')):
                        complete_path = os.path.join(complete_path, '__init__.py')

                if self._is_loadable(complete_path):
                    self.loadable_files[existing_path][entry] = complete_path

        return self.loadable_files[existing_path].get(module)


    @property
    def library_modifications(self):
        """
        A set of modifications that may necessary because some packages are not restrictive enough in their requirements.
        """
        if not self.library_mods:
            with open(Path(os.path.abspath(__file__)).parent.parent.parent / 'etc' / 'library_modifications.json', 'r') as fh:
                self.library_mods = json.load(fh)

        return self.library_mods


    @profile
    def _is_loadable(self, path):
        """
        Determine if a provided path is loadable by Python.
        """
        return bool(path and path.endswith('.py') or path.endswith('.so'))


    @profile
    def _find_file_in_sys_path(self, sys_path, fullname):
        """
        Go down into a system path and see if it may contain the module that we're looking for. We may have just not gone deep enough into
        the path to find it yet.
        """
        pieces        = fullname.split('.')
        resolved_file = None
        root_name     = pieces[0]
        sys_path_reqs, sys_path_updated = self._process_sys_path(sys_path)

        for level in pieces[1:]:
            if not sys_path_reqs or root_name not in sys_path_reqs:
                return None, sys_path_updated

            sys_path_reqs, updated = self._process_sys_path(sys_path, root_name)
            root_name        += '.' + level
            sys_path_updated |= updated

        if root_name in sys_path_reqs:
            if self._is_loadable(sys_path_reqs[root_name]):
                resolved_file = sys_path_reqs[root_name]

        return resolved_file, sys_path_updated


    @profile
    def _find_path_file(self, fullname, path):
        """
        Look for a file that has not been indexed but meets the requirement. If one cannot be found using the path directly, look at the
        sys.path locations. Re-index any matches from those and then see if any file fits the need.
        """

        pieces    = fullname.split('.')
        root_name = '.'.join(pieces[:-1])
        module    = pieces[-1]

        if not path and root_name in self.complete_reqs:
            path = self.complete_reqs[root_name]
        else:
            return None

        if isinstance(path, list):
            existing_path = path[0]
        else:
            existing_path = str(path)

        resolved_file = self._get_loadable_file(existing_path, module)

        if resolved_file:
            # Update the root path, but only if we don't already have a loadable file
            if root_name not in self.complete_reqs or not self._is_loadable(self.complete_reqs[root_name]):
                self.complete_reqs[root_name] = existing_path

            self.complete_reqs[fullname] = resolved_file

        return resolved_file


    @profile
    def _find_sys_path_file(self, fullname):
        """
        See if a specified module exists in system paths. This may have to probe deeper into those directories if we haven't done it, yet.
        """
        sys_path_updated = False
        resolved         = None

        # Look in all system paths that are in the list before we "injected" the specified requirements.
        for sys_path in sys.path:
            if sys_path == self.sys_path_marker:
                break

            if sys_path not in self.ignored_sys_path:
                resolved, updated = self._find_file_in_sys_path(sys_path, fullname)
                sys_path_updated |= updated

                if resolved:
                    self.complete_reqs[fullname] = resolved
                    break

        # If we didn't find it in the newer system paths, look and see if we have them in the resolved requirements
        if not resolved and fullname in self.resolved_reqs and self._is_loadable(self.resolved_reqs[fullname]):
            resolved = self.resolved_reqs[fullname]

        # Finally, if we haven't found a loadable file, look in the old set of system paths
        if not resolved:
            processing = False

            for sys_path in sys.path:
                if processing:
                    if sys_path not in self.ignored_sys_path:
                        resolved_file, updated = self._find_file_in_sys_path(sys_path, fullname)
                        sys_path_updated      |= updated

                        if resolved_file:
                            self.complete_reqs[fullname] = resolved_file
                            break
                elif sys_path == self.sys_path_marker:
                    processing = True

        # The individual system path dictionaries may have been updated based on our searching,
        # re-create the complete requirements dictionary to include the updated versions
        if sys_path_updated:
            self._update_complete_requirements()


    @profile
    def _find_appropriate_dir(self, fullname):
        """
        Look to see if any indexed module location has the provided module name as a prefix. If so, we can create a valid namespace.
        """
        prefix = fullname + '.'

        # If we have lower-level modules with this prefix, then we can just create a namespace
        for module_name in self.complete_reqs:
            if module_name.startswith(prefix):
                self.complete_reqs[fullname] = None
                break


    @profile
    def _update_requirements(self, dest_reqs, input_requirements):
        """
        If we use just the update function to update one dictionary with another, we will miss out on the fact that a file
        (ie. __init__.py) should take precedence over just a path with no loadable file. This updates the destination requirements
        properly.
        """
        for key in input_requirements:
            if key not in dest_reqs or self._is_loadable(input_requirements[key]):
                dest_reqs[key] = input_requirements[key]


    @profile
    def _update_complete_requirements(self):
        """
        Create a single dictionary that we can use that is a map between a module name
        and a path. It uses the input requirements as well as the sys.path list in order
        to create this.
        """
        # return
        self.complete_reqs = {}

        # First add all files from the system paths that are after our own path marker
        processing = False

        for sys_path in sys.path:
            if processing:
                if sys_path not in self.ignored_sys_path:
                    sys_path_reqs, _ = self._process_sys_path(sys_path)
                    self._update_requirements(self.complete_reqs, sys_path_reqs)
            elif sys_path == self.sys_path_marker:
                processing = True

        # Next, add specified requirements
        self._update_requirements(self.complete_reqs, self.resolved_reqs)

        # Finally, overlay all system paths that are sooner in the list than our own marker
        for sys_path in sys.path:
            if sys_path == self.sys_path_marker:
                break

            if sys_path not in self.ignored_sys_path:
                sys_path_reqs, _ = self._process_sys_path(sys_path)
                self._update_requirements(self.complete_reqs, sys_path_reqs)


    @profile
    def _process_sys_path(self, location, module=None):
        """
        Get the module structure for a single path. This will automatically cache each
        location so that we don't have to look more than once. If a module is provided,
        that means that we're getting the structure farther into the given path, so determine
        and update that path with the additional structure.
        """
        if module:
            process_location = os.path.join(location, *(module.split('.')))
        else:
            process_location = location

        sys_path_updated = False

        if location not in self.bad_paths and process_location not in self.reqs_by_path:
            self._process_new_sys_path(process_location, location, module)
            sys_path_updated = True

        return self.reqs_by_path.get(location, {}), sys_path_updated


    @profile
    def _process_new_sys_path(self, process_location, location, module):
        """
        Process a system path that hasn't been seen before, as well as cache the results so that it doesn't have to be examined again.
        """
        log('Processing new system path', process_location, location, module, min_level=2)
        real_process_location = os.path.realpath(process_location)

        try:
            structure     = self.storage.get_module_structure(real_process_location, depth_max=1)
            new_structure = {}
            entry_prefix  = module if module else ''

            for entry in structure:
                complete_path = os.path.join(real_process_location, structure[entry] or entry)

                if not self._is_loadable(complete_path):
                    if os.path.isfile(os.path.join(complete_path, '__init__.py')):
                        complete_path = os.path.join(complete_path, '__init__.py')

                prefix                        = entry_prefix + '.' if entry_prefix and entry else entry_prefix
                new_structure[prefix + entry] = complete_path
                structure[entry]              = complete_path

            if location not in self.reqs_by_path:
                self.reqs_by_path[location] = {}

            if process_location not in self.reqs_by_path:
                self.reqs_by_path[process_location] = {}

            self.reqs_by_path[location].update(new_structure)

            if process_location != location:
                self.reqs_by_path[process_location].update(structure)
        except:
            log('Bad system path', location, min_level=2)
            self.bad_paths.add(location)


    @profile
    def _load_requirements(self):
        """
        Load the requirements file, resolve dependencies, and install any missing ones.
        """
        log('Loading Requirements', min_level=1)
        files           = self.requirements.split(':')
        requirements    = []
        processed_files = set()

        for input_file in files:
            additional_reqs = self._process_requirements_file(input_file, processed_files)

            if verbosity_level() > 1:
                differences = []

                for req in additional_reqs:
                    if req not in requirements:
                        differences.append(req)

                log('Requirements from ', input_file, differences, min_level=2)

            requirements.extend(additional_reqs)

        return self._install_requirements(requirements, processed_files)


    @profile
    def _combine_requirements(self, input_requirements, existing_requirements):
        for req in input_requirements:
            if req['lib'] not in existing_requirements:
                existing_requirements[req['lib']] = []

            if req['spec'] not in existing_requirements[req['lib']]:
                existing_requirements[req['lib']].append(req['spec'])

            # if req['lib'] not in existing_requirements:
            #     existing_requirements[req['lib']] = {'specs': [], 'extras': set()}

            # if req['spec'] not in existing_requirements[req['lib']]:
            #     existing_requirements[req['lib']]['specs'].append(req['spec'])

            # if req['extras']:
            #     existing_requirements[req['lib']]['extras'] = existing_requirements[req['lib']]['extras'].union(req['extras'])

        return existing_requirements


    @profile
    def _install_package(self, name, spec, processed_reqs):
        """
        Select and install the proper library based on the spec list that has been gathered.
        """
        requirement = name + spec

        if requirement not in processed_reqs:
            location = None
            log('Processing Library:', name, spec, min_level=3)

            # try:
            if True:
                location = self.storage.cache(STORAGE_PATH_PACKAGES, name, spec)
            # except:
            #     pass

            processed_reqs[requirement] = location

        return processed_reqs.get(requirement)


    @profile
    def _read_resolved_requirements(self, location):
        if os.environ.get('DEPY_BYPASS_CACHE'):
            return None

        try:
            with open(location / 'resolution', 'r') as fh:
                return json.load(fh)
        except:
            pass

        return None


    def sort_versions(self, spec1, spec2):
        parsed1 = self.storage.parse_requirement_spec(spec1)
        parsed2 = self.storage.parse_requirement_spec(spec2)

        if parsed1['op'] == 'any':
            return 1

        if parsed2['op'] == 'any':
            return -1

        v1 = Version(parsed1['ver'])
        v2 = Version(parsed2['ver'])

        if v1 > v2:
            return 1

        if v2 > v1:
            return -1

        return 0


    @profile
    def _install_requirements(self, requirements, processed_files):
        """
        Determine if the combined set of requirements has already been resolved and cached. If it has, use that existing cache instead of
        continuously resolving versions and dependencies.
        """
        sha = hashlib.sha256()
        sha.update(str(requirements).encode('utf-8'))
        sha.update(CONFLICT_RESOLUTION_MODE.encode('utf-8'))
        requirements_hash     = sha.hexdigest()
        resolved_requirements = None

        if self.storage.is_cached(STORAGE_PATH_REQS / requirements_hash):
            resolved_requirements = self._read_resolved_requirements(STORAGE_PATH_REQS / requirements_hash)

            if resolved_requirements:
                log('Using cached requirements:', STORAGE_PATH_REQS / requirements_hash, min_level=1)
                requirements = resolved_requirements

        locations      = {}
        errors         = []
        combined_reqs  = self._combine_requirements(requirements, {})
        current_reqs   = deepcopy(combined_reqs)
        processing     = True
        processed_reqs = {}
        req_history    = [deepcopy(current_reqs)]

        while processing:
            processing = False

            for req in sorted(combined_reqs.keys()):
                used_requirements = combined_reqs[req].copy()
                location          = None

                if CONFLICT_RESOLUTION_MODE == 'newest':
                    used_requirements = sorted(used_requirements, key=cmp_to_key(self.sort_versions), reverse=True)

                while used_requirements:
                    spec     = ','.join(used_requirements)
                    location = self._install_package(req, spec, processed_reqs)

                    if location or CONFLICT_RESOLUTION_MODE == 'strict':
                        break

                    used_requirements.pop()

                if location:
                    locations[req] = location
                else:
                    error_message = f'ERROR: Unable to cache {req} : {spec}'

                    if error_message not in errors:
                        errors.append(error_message)

            # We can short-cut this a bit if we're restoring from cached libraries
            if not resolved_requirements:
                for lib, location in locations.items():
                    extras = []

                    for req in requirements:
                        if req['lib'] == lib:
                            extras = req.get('extras')
                            break

                    new_reqs      = self._resolve_dependencies(location, processed_files, extras)
                    combined_reqs = self._combine_requirements(new_reqs, combined_reqs)

                if combined_reqs != current_reqs:
                    processing   = True
                    current_reqs = deepcopy(combined_reqs)
                    req_history.append(deepcopy(current_reqs))

        if errors:
            print('\n'.join(errors), file=sys.stderr)
            sys.exit(1)

        return self._get_module_names(locations, requirements_hash, using_cache=bool(resolved_requirements))


    @profile
    def _get_module_names(self, locations, requirements_hash, using_cache=False):
        """
        Combine all of the structures in the resolved dependencies into a single structure that will be used later when a module is
        imported. This will also cache the dependencies for later use.
        """
        modules_by_location   = {}
        resolved_dependencies = []

        for module in locations:
            version                   = os.path.basename(os.path.dirname(locations[module]))
            self.lib_metadata[module] = {'version': version, 'path': locations[module]}
            new_dep                   = {'lib': module, 'spec': '==' + version, 'version': version}
            resolved_dependencies.append(new_dep)
            log('Using Library:', new_dep['lib'] + new_dep['spec'], min_level=3)
            package_file = os.path.join(locations[module], '.structure')

            try:
                with open(package_file, 'r') as fh:
                    structure = json.load(fh)

                for entry in structure:
                    structure[entry] = os.path.join(locations[module], structure[entry])

                modules_by_location.update(structure)
            except:
                log('Unable to get the package for %s at %s', new_dep['lib'] + new_dep['spec'], package_file, min_level=1)
                continue

        refresh = False

        if not using_cache:
            refresh = self._post_process_requirements(resolved_dependencies)
            self.storage.cache_requirements(STORAGE_PATH_REQS / requirements_hash, resolved_dependencies)

            if refresh:
                return self._load_requirements()

        return modules_by_location


    @profile
    def _post_process_requirements(self, requirements):
        """
        There are some sets of libraries that do not work together, and their own dependencies do not catch these issues. This will post-
        process any sets of requirements to make sure that we're not pulling in specifically conflicting libraries.
        """
        dict_reqs = {}
        updated   = False

        for req in requirements:
            dict_reqs[req['lib'].lower()] = req

        for library_name in (self.library_modifications.keys() & dict_reqs.keys()):
            mod = self.library_modifications[library_name]

            if (mod['comparison']['name'] in dict_reqs and
                    self.storage.compare_versions(Version(dict_reqs[library_name]['version']), mod['op'], Version(mod['version'])) and
                    self.storage.compare_versions(Version(dict_reqs[mod['comparison']['name']]['version']), mod['comparison']['op'], Version(mod['comparison']['version']))):
                dict_reqs[mod['comparison']['name']]['version'] = mod['comparison']['new_version']
                dict_reqs[mod['comparison']['name']]['spec']    = '==' + mod['comparison']['new_version']
                updated = True

        return updated


    @profile
    def _process_requirements_file(self, file, processed_files, extras=[]):
        if file in processed_files:
            return []

        try:
            if os.path.basename(file) == 'poetry.lock':
                from .poetry import PoetryFile
                rf = PoetryFile.from_lock_file(file)
            else:
                rf = RequirementsFile.from_file(file, include_nested=True)
        except:
            return []

        processed_files.add(file)
        requirements = []

        for req in rf.requirements:
            # Dataclasses is a requirement that should not be used in versions of python >= 3.7, so make sure that
            # it's being properly filtered out
            if req.name.lower() == 'dataclasses':
                if not req.marker:
                    from packaging.markers import Marker
                    req.marker = Marker('python_version <= "3.6"')

            # This module is done here instead of in the modifications because I can't even get versions lower than this to install. Maybe
            # the library modifications should happen earlier, or even here as well?
            if req.name.lower() == 'cryptography':
                requirements.append({'lib': req.name, 'spec': '==41.0.2'})
            elif req.match_marker(extras):
                if req.specifier:
                    for spec in req.specifier:
                        requirements.append({'lib': req.name, 'spec': str(spec), 'extras': req.extras})
                else:
                    requirements.append({'lib': req.name, 'spec': 'any', 'extras': req.extras})

        return requirements


    @profile
    def _resolve_dependencies(self, location, processed_files, extras):
        """
        Get all library dependencies from the .dependencies file in the cached location and return the set of requirements
        """
        manifest = os.path.join(location, '.dependencies')

        if os.path.isfile(manifest):
            requirements = self._process_requirements_file(manifest, processed_files, extras)

            if requirements:
                return requirements

        return {}
