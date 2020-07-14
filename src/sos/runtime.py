#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import pkg_resources

# backward compatibility #1337
from .pattern import expand_pattern
from .targets import path, paths
from .utils import get_output, sos_get_param

# silent pyflakes
sos_get_param, get_output, path, paths, expand_pattern


def _load_group(group: str) -> None:
    for _entrypoint in pkg_resources.iter_entry_points(group=group):
        # import all targets and actions from entry_points
        # Grab the function that is the actual plugin.
        _name = _entrypoint.name
        try:
            _plugin = _entrypoint.load()
            globals()[_name] = _plugin
        except Exception as e:
            # look for sos version requirement
            from .utils import get_logger
            if 'Requirement.parse' in str(e):
                import re
                from ._version import __version__
                from pkg_resources import parse_version
                m = re.search(r"Requirement.parse\('sos>=([^)]*)'\)", str(e))
                if m:
                    if parse_version(__version__) < parse_version(m.group(1)):
                        get_logger().warning(
                            f'Failed to load target {_entrypoint.name}: please upgrade your version of sos from {__version__} to at least version {m.group(1)}'
                        )
                        continue
            if _name == 'run':
                # this is critical so we print the warning
                get_logger().warning(
                    f'Failed to load target {_entrypoint.name}: {e}')
            else:
                get_logger().debug(
                    f'Failed to load target {_entrypoint.name}: {e}')


_load_group('sos_targets')
_load_group('sos_actions')
_load_group('sos_functions')
