# Introduction
The goal of this project is to make dynamic library usage easy for Python developers. If a requirements.txt or poetry.lock file is present
in the root directory of the project then the dependencies specified in it will automatically be downloaded, cached, and used when running
the specified script.


# Getting Started
To execute a Python script, run 'depy <script name>'. This will install any dependencies listed in requirements.txt or poetry.lock file,
presuming they aren't already installed on their system, and then execute the script.

By default, this will install dependencies into the <HOME>/.local/depy_v1 directory.


# Environmental Variables
DEPY_DISABLE      - Disable the use of depy, and only run the script directly.
DEPY_REQS         - The path to a requirements file that will be used when, instead of finding it
DEPY_CACHE_PATH   - The path where dependencies are cached
DEPY_MODE         - Determes how dependencies are resolved. By default, this is 'strict'. It can also be 'newest' to pick up the newest
                    version of a dependency that is specified, and 'legacy' to pick up dependencies based on specified order.
DEPY_PROFILE      - Enable profiling when running.
DEPY_FORCEDLIBS   - Forces additional paths to be added to the searched library paths.
DEPY_ADD_PP       - Add all libraries to the PYTHONPATH environmental variable. This is true by default, but can be set to 0 in order to
                    disable.
DEPY_BYPASS_CACHE - Do not use the stored version of the resolved set of requirements.
DEPY_INDEXES      - Additional indexes to use when looking for dependencies, semi-colon separated.
DEPY_USERNAME     - The username to use with the indexes.
DEPY_DEBUG        - Set the verbosity level when running.
