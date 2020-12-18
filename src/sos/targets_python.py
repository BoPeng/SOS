#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

from .targets import BaseTarget
from .utils import env, textMD5


class Py_Module(BaseTarget):
    '''A target for a Python module.'''

    LIB_STATUS_CACHE = {}

    def __init__(self, module, version=None, autoinstall=False):
        super(Py_Module, self).__init__()
        if not isinstance(module, str):
            raise ValueError('A string is expected for module name.')
        self._module = module.strip()
        self._version = version.strip() if isinstance(version, str) else version
        for opt in ('==', '>=', '>', '<=', '<', '!='):
            if opt in self._module:
                if self._version is not None:
                    raise ValueError(
                        f"Specifying 'version=' option in addition to '{module}' is not allowed"
                    )
                self._module, self._version = [
                    x.strip() for x in self._module.split(opt, 1)
                ]
                if ',' in self._version:
                    raise ValueError(
                        f'SoS does not yet support multiple version comparisons. {self._mdoule} provided'
                    )
                self._version = opt + self._version
                break
        self._autoinstall = autoinstall

    def _install(self, name, autoinstall):
        '''Check existence of Python module and install it using command
        pip install if necessary.'''
        import importlib
        import pkg_resources
        spam_spec = importlib.util.find_spec(name)
        reinstall = False
        if spam_spec is not None:
            if self._version:
                mod = importlib.__import__(name)
                if hasattr(mod, '__version__'):
                    ver = mod.__version__
                else:
                    try:
                        ver = pkg_resources.get_distribution(name).version
                    except Exception as e:
                        env.logger.debug(
                            f'Failed to get version of {name}: {e}')
                env.logger.debug(
                    f'Comparing exiting version {ver} against requested version {self._version}'
                )
                if self._version.startswith(
                        '==') and pkg_resources.parse_version(
                            ver) == pkg_resources.parse_version(
                                self._version[2:]):
                    pass
                elif self._version.startswith(
                        '<=') and pkg_resources.parse_version(
                            ver) <= pkg_resources.parse_version(
                                self._version[2:]):
                    pass
                elif self._version.startswith(
                        '<') and not self._version.startswith(
                            '<=') and pkg_resources.parse_version(
                                ver) < pkg_resources.parse_version(
                                    self._version[1:]):
                    pass
                elif self._version.startswith(
                        '>=') and pkg_resources.parse_version(
                            ver) >= pkg_resources.parse_version(
                                self._version[2:]):
                    pass
                # the case of >
                elif self._version.startswith(
                        '>') and not self._version.startswith(
                            '>=') and pkg_resources.parse_version(
                                ver) > pkg_resources.parse_version(
                                    self._version[1:]):
                    pass
                elif self._version.startswith(
                        '!=') and pkg_resources.parse_version(
                            ver) != pkg_resources.parse_version(
                                self._version[2:]):
                    pass
                elif self._version[0] not in (
                        '=', '>', '<', '!') and pkg_resources.parse_version(
                            ver) == pkg_resources.parse_version(self._version):
                    pass
                else:
                    env.logger.warning(
                        f'Version {ver} of installed {name} does not match specified version {self._version}.'
                    )
                    reinstall = True
        if spam_spec and not reinstall:
            return True
        if not autoinstall:
            return False
        # try to install it?
        import subprocess
        cmd = ['pip', 'install'] + ([] if self._version else ['-U']) + [
            self._module + (self._version if self._version else '')
            if self._autoinstall is True else self._autoinstall
        ]
        env.logger.info(
            f'Installing python module {name} with command {" ".join(cmd)}')
        ret = subprocess.call(cmd)
        if reinstall:
            import sys
            importlib.reload(sys.modules[name])
        # try to check version
        return ret == 0 and self._install(name, False)

    def target_exists(self, mode='any'):
        if (self._module, self._version) in self.LIB_STATUS_CACHE:
            return self.LIB_STATUS_CACHE[(self._module, self._version)]
        else:
            ret = self._install(self._module, self._autoinstall)
            self.LIB_STATUS_CACHE = {
                x: y
                for x, y in self.LIB_STATUS_CACHE.items()
                if x[0] != self._module
            }
            self.LIB_STATUS_CACHE[(self._module, self._version)] = ret
            return ret

    def target_name(self):
        return self._module

    def target_signature(self, mode='any'):
        # we are supposed to get signature of the module, but we cannot
        return textMD5('Python module ' + self._module)
