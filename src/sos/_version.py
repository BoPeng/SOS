#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import sys

__all__ = ['__version__', 'SOS_FULL_VERSION']

_py_ver = sys.version_info
if _py_ver.major == 2 or (_py_ver.major == 3 and
                          (_py_ver.minor, _py_ver.micro) < (6, 0)):
    raise SystemError(
        'SOS requires Python 3.6 or higher. Please upgrade your Python {}.{}.{}'
        .format(_py_ver.major, _py_ver.minor, _py_ver.micro))

# version of the SoS language
__sos_version__ = '1.0'
# version of the sos command
__version__ = '0.21.11'
__py_version__ = '{}.{}.{}'.format(_py_ver.major, _py_ver.minor, _py_ver.micro)

#
SOS_FULL_VERSION = '{} for Python {}.{}.{}'.format(__version__, _py_ver.major,
                                                   _py_ver.minor, _py_ver.micro)
SOS_COPYRIGHT = '''SoS {} : Copyright (c) 2016 Bo Peng'''.format(__version__)
SOS_CONTACT = '''Please visit http://github.com/vatlab/SoS for more information.'''
