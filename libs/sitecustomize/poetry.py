import os





class PoetryRequirement():
    def __init__(self, package, version, marker=None):
        self.package = package
        self.version = version


    @property
    def name(self):
        return self.package


    @property
    def specifier(self):
        return ['==' + self.version]


    def match_marker(self):
        return True


    @property
    def marker(self):
        return None


class PoetryResults():
    def __init__(self, packages):
        self.requirements = packages



class PoetryFile():
    @staticmethod
    def from_lock_file(input_file):
        import toml

        with open(input_file, 'r') as fh:
            loaded = toml.load(fh)

        packages = []

        for package in loaded.get('package', {}):
            packages.append(PoetryRequirement(package['name'], package['version']))

        return PoetryResults(packages)

