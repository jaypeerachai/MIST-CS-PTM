"""Use the running interpreter for library lookup."""

import sys

import jedi


def project(repo, paths):
    result = jedi.Project(path=str(repo), added_sys_path=paths, environment_path=sys.executable)
    if result.get_environment().executable != sys.executable:
        raise RuntimeError("Jedi selected a different Python interpreter")
    return result
