#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import copy
import glob
import os
import pickle
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from copy import deepcopy
from itertools import combinations, tee
from pathlib import Path, WindowsPath, PosixPath
from shlex import quote
from typing import Union, Dict, Any, List
import fasteners
import pkg_resources

from .utils import (Error, env, pickleable, short_repr, stable_repr)
from .pattern import extract_pattern
from .eval import interpolate
from .controller import request_answer_from_controller, send_message_to_controller

try:
    from xxhash import xxh64 as hash_md5
except ImportError:
    from hashlib import md5 as hash_md5

__all__ = ['dynamic', 'executable', 'env_variable', 'sos_variable']


def is_basic_type(obj):
    if isinstance(obj, (bool, int, float, str, bytes)):
        return True
    if isinstance(obj, (tuple, list, set)):
        return all(is_basic_type(x) for x in obj)
    if isinstance(obj, dict):
        return all(is_basic_type(x) for x in obj.keys()) and all(
            is_basic_type(x) for x in obj.values())
    if isinstance(obj, (file_target, path, paths)):
        return True
    # we support types defined in numpy and pandas, but not others
    module = obj.__class__.__module__
    if 'pandas' in module or 'numpy' in module:
        return True
    return False


class UnknownTarget(Error):

    def __init__(self, target: 'BaseTarget'):
        Error.__init__(self, 'Target unavailable: %s' % target)
        self.target = target


class RemovedTarget(Error):

    def __init__(self, target: 'BaseTarget'):
        if isinstance(target, invalid_target):
            Error.__init__(self, 'Invalid target from errored steps')
        else:
            Error.__init__(self, 'Target removed: %s' % target)
        self.target = target


class UnavailableLock(Error):
    """Raised when there are errors in prepare mode. Such errors are not raised
    immediately, but will be collected and raised at the end """

    def __init__(self, signature):
        Error.__init__(
            self,
            f'Failed to obtain lock {signature[2]} for input {short_repr(signature[0])} and output {short_repr(signature[1])}. It is likely '
            +
            'that these files are protected by another SoS process or concurrant task that is generating the same set of files. Please manually remove the lockfile '
            + 'if you are certain that no other process is using the lock.')
        self.input = signature[0]
        self.output = signature[1]
        self.sig_file = signature[2]


#
# Runtime signature
#


def textMD5(text):
    '''Get md5 of a piece of text'''
    m = hash_md5()
    if isinstance(text, str):
        m.update(text.encode())
    else:
        m.update(text)
    return m.hexdigest()


def objectMD5(obj):
    '''Get md5 of an object'''
    if hasattr(obj, 'target_name'):
        return obj.target_name()
    try:
        return textMD5(pickle.dumps(obj))
    except Exception:
        return ''


def fileMD5(filename, partial=True):
    '''Calculate partial MD5, basically the first and last 8M
    of the file for large files. This should signicicantly reduce
    the time spent on the creation and comparison of file signature
    when dealing with large bioinformat ics datasets. '''
    filesize = os.path.getsize(filename)
    # calculate md5 for specified file
    md5 = hash_md5()
    block_size = 2**20  # buffer of 1M
    try:
        # 2**24 = 16M
        if (not partial) or filesize < 2**24:
            with open(filename, 'rb') as f:
                while True:
                    data = f.read(block_size)
                    if not data:
                        break
                    md5.update(data)
        else:
            count = 16
            # otherwise, use the first and last 8M
            with open(filename, 'rb') as f:
                while True:
                    data = f.read(block_size)
                    count -= 1
                    if count == 8:
                        # 2**23 = 8M
                        f.seek(-2**23, 2)
                    if not data or count == 0:
                        break
                    md5.update(data)
    except IOError as e:
        sys.exit(f'Failed to read {filename}: {e}')
    return md5.hexdigest()


class BaseTarget(object):
    '''A base class for all targets (e.g. a file)'''

    def __init__(self, *args, **kwargs):
        self._sigfile = None
        self._dict = kwargs
        self.traced = False

    def set_traced(self):
        self.traced = True

    def set(self, *args, **kwargs):
        if args:
            if len(args) != 2:
                raise ValueError(
                    'set(name, value) or set(name=value) is expected.')
            if not is_basic_type(args[1]):
                env.logger.warning(
                    f'Failed to set attribute: {args[1]} is or contains unsupported data type.'
                )
                return self
            if hasattr(self, args[0]):
                raise ValueError(
                    f'Attribute {args[0]} conflicts with another attribute of type {self.__class__.__name__}.'
                )
            self._dict[args[0]] = args[1]
        #
        if kwargs:
            for name, value in kwargs.items():
                if not is_basic_type(value):
                    env.logger.warning(
                        f'Failed to set attribute: {value} is or contains unsupported data type.'
                    )
                    return self
                if hasattr(self, name):
                    raise ValueError(
                        f'Attribute {name} conflicts with another attribute of {value.__class__.__name__}.'
                    )
            self._dict.update(kwargs)
        return self

    def get(self, name, default=None):
        return self._dict.get(name, default)

    def __getattr__(self, name):
        try:
            return self._dict[name]
        except Exception:
            # if name in self._dict:
            # return self._dict.get(name)
            raise AttributeError(
                f'{self.__class__.__name__} object has no attribute {name}')

    def target_exists(self, mode='any'):
        # mode should be 'any', 'target', or 'signature'
        raise RuntimeError('Undefined base function target_exists')

    def target_name(self):
        # name of the target, which should be able to differentiate
        # this object with other targets of the same type.
        raise RuntimeError('Undefined base function target_name')

    def target_signature(self):
        # signature of the content of the target, which should be
        # able to detect changes of the content of target
        raise RuntimeError('Undefined base function target_signature')

    def create_placeholder(self):
        pass

    def __repr__(self):
        return f'{self.__class__.__name__}("{self.target_name()}")'

    def __hash__(self):
        return hash(repr(self))

    def validate(self, sig):
        return self.target_signature() == sig

    def __eq__(self, obj):
        return isinstance(obj, self.__class__) and self.target_signature(
        ) == obj.target_signature()


class sos_variable(BaseTarget):
    '''A target for a SoS variable.'''

    def __init__(self, var):
        super(sos_variable, self).__init__()
        self._var = var

    def target_exists(self, mode='any'):
        return self._var in env.sos_dict

    def target_name(self):
        return self._var

    def target_signature(self):
        return objectMD5(env.sos_dict[self._var])

    def __hash__(self):
        return hash(repr(self))

    def __eq__(self, other):
        return isinstance(other, sos_variable) and self._var == other._var

    def __format__(self, format_spec):
        # handling special !q conversion flag
        if format_spec and format_spec[0] == 'R':
            return self._var.__format__(format_spec[1:])
        else:
            return str(self).__format__(format_spec)


class env_variable(BaseTarget):
    '''A target for an environmental variable.'''

    def __init__(self, var):
        super(env_variable, self).__init__()
        self._var = var

    def target_exists(self, mode='any'):
        return self._var in os.environ

    def target_name(self):
        return self._var

    def target_signature(self, mode='any'):
        return textMD5(repr(os.environ[self._var]))

    def __eq__(self, other):
        return isinstance(other, env_variable) and self._var == other._var

    def __hash__(self):
        return hash(repr(self))

    def __format__(self, format_spec):
        # handling special !q conversion flag
        if format_spec and format_spec[0] == 'R':
            return self._var.__format__(format_spec[1:])
        else:
            return str(self).__format__(format_spec)


class invalid_target(BaseTarget):
    '''A target for an environmental variable.'''

    def __init__(self):
        super(invalid_target, self).__init__()

    def target_exists(self, mode='any'):
        return False

    def target_name(self):
        return 'invalid'

    def target_signature(self, mode='any'):
        return ''


class system_resource(BaseTarget):
    '''A target for required computing resource.'''

    def __init__(self, mem=None, disk=None):
        super(system_resource, self).__init__()
        self._mem = mem
        self._disk = disk

    def target_exists(self, mode='any'):
        if self._mem:
            import psutil
            from .utils import expand_size
            avail = psutil.virtual_memory().available
            if avail < expand_size(self._mem):
                #env.logger.warning(f'System available memory {avail} is less than request {self._mem}')
                return False
        if self._disk:
            import psutil
            from .utils import expand_size
            avail = psutil.disk_usage(os.path.abspath('.')).free
            if avail < expand_size(self._disk):
                #env.logger.warning(f'System available diskspace {avail} is less than request {self._disk}')
                return False
        return True

    def target_name(self):
        res = []
        if self._mem:
            res.append(f'mem={repr(self._mem)}')
        if self._disk:
            res.append(f'disk={repr(self._disk)}')
        return f'system_resource({",".join(res)})'

    def target_signature(self):
        return ''

    def __repr__(self):
        return self.target_name()


class sos_step(BaseTarget):
    '''A target for a step of sos.'''

    def __init__(self, step_name, **kwargs):
        super(sos_step, self).__init__(**kwargs)
        self._step_name = str(step_name)

    def target_exists(self, mode='any'):
        # the target exists only if it has been executed?
        # which is indicated by a variable
        return request_answer_from_controller(['sos_step', self._step_name])

    def target_name(self):
        return self._step_name

    def target_signature(self, mode='any'):
        return textMD5(f'sos_step({self._step_name})')

    def __eq__(self, other):
        return isinstance(other,
                          sos_step) and self._step_name == other._step_name

    def __hash__(self):
        return hash(repr(self))

    def __format__(self, format_spec):
        # handling special !q conversion flag
        if format_spec and format_spec[0] == 'R':
            return self._step_name.__format__(format_spec[1:])
        return str(self).__format__(format_spec)


class named_output(BaseTarget):
    '''A target for a named output'''

    def __init__(self, output_name):
        super(named_output, self).__init__()
        if not isinstance(output_name, str):
            raise ValueError('named_output() only accept one output name')
        self._output_name = output_name

    def target_exists(self, mode='any'):
        # this target is handled specifically and will not be used directly
        return False

    def target_name(self):
        return self._output_name

    def target_signature(self, mode='any'):
        return textMD5(f'named_output({self._output_name})')

    def __eq__(self, other):
        return isinstance(
            other, named_output) and self._output_name == other._output_name

    def __hash__(self):
        return hash(repr(self))


class dynamic(BaseTarget):
    '''A dynamic executable that only handles input files when
    it is available. This target is handled directly with its `resolve`
    function called by the executor. '''

    def __init__(self, target):
        self._target = target

    def target_name(self):
        return self._target

    def target_exists(self, mode='any'):
        return False

    def target_signature(self, mode='any'):
        return ''

    def resolve(self):
        if isinstance(self._target, str):
            return sorted(
                [x for x in glob.glob(self._target) if os.path.isfile(x)])
        elif isinstance(self._target, Sequence) and all(
                isinstance(x, str) for x in self._target):
            return sorted(
                sum([[x
                      for x in glob.glob(t)
                      if os.path.isfile(x)]
                     for t in self._target], []))
        else:
            return self._target

    def __format__(self, format_spec):
        # handling special !q conversion flag
        if format_spec and format_spec[0] == 'R':
            return sos_targets(self._target).__format__(format_spec[1:])
        else:
            return str(self).__format__(format_spec)


class remote(BaseTarget):
    '''A remote target is not tracked and not translated during task execution'''

    def __init__(self, *targets):
        super(remote, self).__init__()
        self.__unresolvable_object__ = True
        if len(targets) == 1:
            self._target = targets[0]
        else:
            # multi-item targets
            self._target = targets
        if isinstance(self._target,
                      Sequence) and not isinstance(self._target, str):
            self.__flattenable__ = True

    def target_name(self):
        if isinstance(self._target, str):
            return file_target(self._target).target_name()
        elif isinstance(self._target, BaseTarget):
            return self._target.target_name()
        else:
            return repr(self._target)

    def target_exists(self, mode='any'):
        return True

    def target_signature(self, mode='any'):
        return textMD5(self.target_name())

    def resolve(self):
        return self._target

    def flatten(self):
        return [remote(x) for x in self._target]

    def __format__(self, format_spec):
        # handling special !q conversion flag
        if format_spec and format_spec[0] == 'R':
            return sos_targets(self._target).__format__(format_spec[1:])
        else:
            return str(self).__format__(format_spec)


class executable(BaseTarget):
    '''A target for an executable command.'''

    def __init__(self, cmd, version=None):
        super(executable, self).__init__()
        self._cmd = cmd
        self._md5 = None
        if version is None:
            self._version = ()
        elif isinstance(version, str):
            self._version = (version,)
        else:
            self._version = tuple(version)

    def __eq__(self, other):
        return isinstance(
            other, executable
        ) and self._cmd == other._cmd and self._version == other._version

    def target_exists(self, mode='any'):
        if mode in ('any', 'target') and shutil.which(
                shlex.split(self._cmd)[0]):
            if self._version:
                try:
                    output = subprocess.check_output(
                        self._cmd,
                        stderr=subprocess.STDOUT,
                        shell=True,
                        timeout=5).decode()
                except subprocess.TimeoutExpired as e:
                    env.logger.warning(str(e))
                    return False
                except subprocess.CalledProcessError as e:
                    env.logger.warning(str(e))
                    return False
                for ver in self._version:
                    if ver in output:
                        return True
                return False
            else:
                return True
        return False

    def target_name(self):
        if self._version:
            return f'{self._cmd} (version={self._version})'
        else:
            return self._cmd

    def target_signature(self):
        # we do not care if the target actually exist
        # or its content has changed
        return textMD5(self._cmd)

    def __hash__(self):
        return hash(repr(self))

    def __format__(self, format_spec):
        # handling special !q conversion flag
        if format_spec and format_spec[0] == 'R':
            return self._cmd.__format__(format_spec[1:])
        else:
            return str(self).__format__(format_spec)


def collapseuser(path):
    home = os.path.expanduser('~')
    if path == home:
        return '~'
    elif path.startswith(home + os.sep):
        return '~' + path[len(home):]
    else:
        return path


class path(type(Path())):
    '''A regular target for files.
    '''
    CONVERTERS = {
        'u':
            os.path.expanduser,
        'U':
            collapseuser,
        'e':
            lambda x: x.replace(' ', '\\ '),
        'a':
            lambda x: os.path.abspath(x),
        'l':
            lambda x: os.path.realpath(x),
        'd':
            lambda x: os.path.dirname(x) or '.',
        'b':
            os.path.basename,
        'n':
            lambda x: os.path.splitext(x)[0],
        'x':
            lambda x: os.path.splitext(x)[1],
        'q': (lambda x: subprocess.list2cmdline([x]))
             if sys.platform == 'win32' else quote,
        'p':
            lambda x: ('/' if len(x) > 1 and x[1] == ':' else '') + x.replace(
                '\\', '/').replace(':/', '/').replace(':', '/'),
        'r':
            repr,
        's':
            str,
        # these are handled elsewhere
        ',':
            lambda x: x,
        '!':
            lambda x: x,
        'R':
            lambda x: x,
    }

    def __new__(cls, *args, **kwargs):
        if cls is Path:
            cls = WindowsPath if os.name == 'nt' else PosixPath
        return cls._from_parts(args).expanduser().expandnamed(
            host=kwargs.get('host', None))

    @staticmethod
    def names(host=None):
        if host is None:
            if '__host__' not in env.sos_dict:
                env.logger.warning(
                    'Incomplete sos environment: missing __host__ definition, assuming "localhost".'
                )
            host = env.sos_dict.get('__host__', 'localhost')
        if 'CONFIG' not in env.sos_dict or 'hosts' not in env.sos_dict['CONFIG']:
            raise RuntimeError(
                'Incomplete sos environment: missing hosts definition.')
        if host not in env.sos_dict['CONFIG']['hosts']:
            raise RuntimeError(
                'Incomplete sos environment: undefined host {host}')
        if 'paths' not in env.sos_dict['CONFIG']['hosts'][host]:
            return []
        else:
            return list(env.sos_dict['CONFIG']['hosts'][host]['paths'].keys())

    def _init(self, template=None):
        super(path, self)._init(template)

        if not (self._drv or self._root) and self._parts:
            if self._parts[0][:1] == '~':
                expanded = self.expanduser()
                self._parts = expanded._parts
                self._drv = expanded._drv
                self._root = expanded._root
            elif self._parts[0][:1] == '#':
                expanded = self.expandnamed()
                self._parts = expanded._parts
                self._drv = expanded._drv
                self._root = expanded._root

    def expandnamed(self, host=None):
        if not self._parts or self._parts[0][:1] != '#':
            return self
        try:
            return self._from_parts([
                env.sos_dict['CONFIG']['hosts']
                [env.sos_dict.get('__host__', 'localhost'
                                 ) if host is None else host]['paths'][
                                     self._parts[0][1:]]
            ] + self._parts[1:])
        except Exception:
            if host is None and '__host__' not in env.sos_dict:
                raise RuntimeError(
                    'Incomplete sos environment: missing __host__ definition.')
            if 'CONFIG' not in env.sos_dict or 'hosts' not in env.sos_dict[
                    'CONFIG']:
                raise RuntimeError(
                    'Incomplete sos environment: missing hosts definition.')
            if host is not None and host not in env.sos_dict['CONFIG']['hosts']:
                raise RuntimeError(
                    f'Incomplete sos environment: undefined host {host}')
            elif env.sos_dict.get(
                    '__host__',
                    'localhost') not in env.sos_dict['CONFIG']['hosts']:
                raise RuntimeError(
                    f'Incomplete sos environment: undefined host {env.sos_dict.get("__host__", "locahost")}'
                )
            if 'paths' not in env.sos_dict['CONFIG']['hosts'][env.sos_dict.get(
                    '__host__', 'localhost')]:
                raise RuntimeError(
                    f'Incomplete sos environment: paths not defined for host {env.sos_dict.get("__host__", "localhost")}'
                )
            name = self._parts[0][1:]
            if name not in env.sos_dict['CONFIG']['hosts'][
                    env.sos_dict.get('__host__', 'localhost'
                                    ) if host is None else host]['paths']:
                raise ValueError(
                    f'Named path "{name}" not defined for host {env.sos_dict.get("__host__", "localhost") if host is None else host}'
                )

    def is_external(self):
        try:
            return os.path.relpath(self.fullname(),
                                   env.exec_dir).startswith('..')
        except Exception:
            # under windows the file might be on different volume
            return True

    def fullname(self):
        return os.path.abspath(str(self))

    def __fspath__(self):
        return self.fullname()

    def __eq__(self, other):
        return os.path.abspath(self.fullname()) == os.path.abspath(
            (other
             if isinstance(other, file_target) else path(other)).fullname())

    def __add__(self, part):
        if isinstance(part, (str, path)):
            return self.__class__(str(self) + str(part))
        else:
            raise ValueError(
                f"Cannot concatenate path to {part} of type {type(part).__name__}: expect a string or path"
            )

    def __format__(self, format_spec):
        # handling special !q conversion flag
        obj = str(self)
        for i, c in enumerate(format_spec):
            if c in self.CONVERTERS:
                obj = self.CONVERTERS[c](obj)
            else:
                # other defined format
                return obj.__format__(format_spec[i:])
        return obj

    def __lt__(self, other):
        return str(self) < str(other)

    def __hash__(self):
        return hash(repr(self))

    def zap(self):
        zap_file = self + '.zapped'
        if not self.exists() and zap_file.is_file():
            return
        if not self.exists() or not self.is_file():
            raise FileNotFoundError(str(self))
        with open(zap_file, 'w') as md5:
            md5.write(
                f'{self.resolve()}\t{os.path.getmtime(self)}\t{os.path.getsize(self)}\t{fileMD5(self)}\n'
            )
        self.unlink()


class file_target(path, BaseTarget):
    '''A regular target for files.
    '''

    def __init__(self, *args, **kwargs):
        # this is path segments
        super(file_target, self).__init__(*args, **kwargs)
        if len(args) == 1 and isinstance(args[0], file_target):
            self._md5 = args[0]._md5
        else:
            self._md5 = None

    def _init(self, template=None):
        super(file_target, self)._init(template)
        self._md5 = None

    def create_placeholder(self):
        # create an empty placeholder file
        if 'TARGET' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                'SOS_DEBUG']:
            env.log_to_file('TARGET', f'Create placeholder target {self}')
        self.touch()
        send_message_to_controller(
            ['workflow_sig', 'placeholder', 'file_target',
             str(self)])

    def target_exists(self, mode='any'):
        try:
            if mode in ('any', 'target') and self.exists():
                return True
            elif mode == 'any' and (self + '.zapped').exists():
                return True
            return False
        except Exception as e:
            env.logger.debug(f"Invalid file_target {self}: {e}")
            return False

    def size(self):
        try:
            return os.path.getsize(self)
        except Exception:
            if (self + '.zapped').is_file():
                with open(self + '.zapped') as sig:
                    line = sig.readline()
                    _, _, s, _ = line.strip().rsplit('\t', 3)
                    return int(s)

    def target_name(self):
        return str(self)

    def __fspath__(self):
        return super(file_target, self).__fspath__()

    def target_signature(self):
        '''Return file signature'''
        if self.exists():
            if not self._md5:
                self._md5 = fileMD5(self)
            return (os.path.getmtime(self), os.path.getsize(self), self._md5)
        elif (self + '.zapped').is_file():
            with open(self + '.zapped') as sig:
                line = sig.readline()
                _, mtime, size, md5 = line.strip().rsplit('\t', 3)
                self._md5 = md5
                return (float(mtime), int(size), md5)
        else:
            raise ValueError(f'{self} does not exist.')

    def sig_file(self):
        return os.path.join(env.exec_dir, '.sos',
                            f'{textMD5(str(self.resolve()))}.file_info')

    def validate(self, sig=None):
        '''Check if file matches its signature'''
        if sig is not None:
            sig_mtime, sig_size, sig_md5 = sig
        else:
            try:
                with open(self.sig_file()) as sig:
                    sig_mtime, sig_size, sig_md5 = sig.read().strip().split()
            except Exception:
                return False
        if not self.exists():
            if (self + '.zapped').is_file():
                with open(self + '.zapped') as sig:
                    line = sig.readline()
                    return sig_md5 == line.strip().rsplit('\t', 3)[-1]
            else:
                return False

        if int(sig_size) != os.path.getsize(self):
            return False
        if sig_mtime == os.path.getmtime(self):
            return True
        return fileMD5(self) == sig_md5

    def write_sig(self):
        '''Write signature to sig store'''
        if not self._md5:
            self._md5 = fileMD5(self)
        with open(self.sig_file(), 'w') as sig:
            sig.write(
                f'{os.path.getmtime(self)}\t{os.path.getsize(self)}\t{self._md5}'
            )

    def __hash__(self):
        return hash(repr(self))

    def __eq__(self, obj):
        return isinstance(
            obj, file_target) and os.path.abspath(self) == os.path.abspath(obj)

    def __deepcopy__(self, memo):
        ft = file_target(self)
        ft._dict = copy.deepcopy(self._dict)
        return ft

    def __reduce__(self):
        return tuple([
            self.__class__,
            super(file_target, self).__reduce__()[1], {
                '_md5': self._md5,
                '_dict': self._dict
            }
        ])


class sos_tempfile(file_target):

    def __new__(cls, path=None, name=None, suffix=None, prefix=None, dir=None):
        if cls is Path:
            cls = WindowsPath if os.name == 'nt' else PosixPath
        filename = request_answer_from_controller(
            ['sos_tempfile', path, name, suffix, prefix, dir])
        return cls._from_parts([filename])


class paths(Sequence, os.PathLike):
    '''A collection of targets'''
    # check if string contains wildcard character
    wildcard = re.compile(r'[*?\[]')

    def __init__(self, *args):
        self._paths = []
        for arg in args:
            self.__append__(arg)

        for t in self._paths:
            if isinstance(t, paths):
                raise RuntimeError(
                    f"Nested paths {t} were introduced by {args}")
            if not isinstance(t, path):
                raise RuntimeError(f"Unrecognized path {t}")

    def paths(self):
        return [str(x) for x in self._paths]

    def __append__(self, arg):
        if isinstance(arg, paths):
            self._paths.extend(arg._paths)
        elif isinstance(arg, str):
            if self.wildcard.search(arg):
                matched = sorted(glob.glob(os.path.expanduser(arg)))
                if matched:
                    self._paths.extend([path(x) for x in matched])
                else:
                    env.logger.debug(f'Pattern {arg} does not match any file')
            else:
                self._paths.append(path(arg))
        elif isinstance(arg, sos_targets):
            if not all(isinstance(x, file_target) for x in arg._targets):
                raise ValueError(
                    f'Cannot convert a sos_targets object {arg} with non-file target to paths'
                )
            self._paths.extend([path(str(x)) for x in arg._targets])
        elif isinstance(arg, file_target):
            self._paths.append(path(str(arg)))
        elif isinstance(arg, Iterable):
            for t in list(arg):
                self.__append__(t)
        elif arg is not None:
            self._paths.append(path(str(arg)))

    def zap(self):
        for p in self._paths:
            p.zap()

    def __getstate__(self):
        return self._paths

    def __setstate__(self, paths):
        self._paths = paths

    def __len__(self):
        return len(self._paths)

    def __getitem__(self, i):
        return self._paths[i]

    def __fspath__(self):
        if len(self._paths) == 1:
            return self._paths[0].__fspath__()
        if not self._paths:
            raise ValueError(f"Cannot treat an empty paths as single path")
        raise ValueError(
            f'Cannot treat an paths object {self} with more than one paths as a single path'
        )

    def __format__(self, format_spec):
        if ',' in format_spec:
            fmt_spec = format_spec.replace(',', '')
            return ','.join(x.__format__(fmt_spec) for x in self._paths)
        return ' '.join(x.__format__(format_spec) for x in self._paths)

    def __deepcopy__(self, memo):
        return paths(deepcopy(self._paths))

    def __getattr__(self, key):
        if len(self._paths) == 1:
            return getattr(self._paths[0], key)
        if len(self._paths) == 0:
            raise AttributeError(
                f"Cannot get attribute {key} from empty target list")
        raise AttributeError(
            f'Cannot get attribute {key} from group of {len(self)} targets {self!r}'
        )

    def __hash__(self):
        return hash(repr(self))

    def __eq__(self, other):
        return self._paths == other._paths if isinstance(other,
                                                         paths) else other

    def __repr__(self):
        return '[' + ', '.join(repr(x) for x in self._paths) + ']'

    def __str__(self):
        return self.__format__('')


class _sos_group(BaseTarget):
    '''A type that is similar to sos_targets but saves index of objects '''

    def __init__(self, indexes, labels=None, parent=None):
        super(_sos_group, self).__init__()
        self._indexes = list(indexes)
        if labels is not None:
            if isinstance(labels, str):
                self._labels = [labels] * len(indexes)
            else:
                self._labels = labels
                if len(self._indexes) != len(self._labels):
                    raise ValueError('Index and source have different length')
        elif parent is not None:
            self._labels = [parent._labels[x] for x in indexes]
        else:
            raise ValueError('Either labels or indexes should be specified')

    def add_last(self, n, parent):
        # add the last n elements of parent to group
        # this has to be called after the elements have been appended
        self._indexes.extend(
            range(len(parent._targets) - n, len(parent._targets)))
        self._labels.extend(parent._labels[len(parent._targets) -
                                           n:len(parent._targets)])
        return self

    def extend(self, grp, start, parent):
        self._indexes.extend([x + start for x in grp._indexes])
        self._labels.extend([parent._labels[x + start] for x in grp._indexes])
        self._dict.update(grp._dict)
        return self

    def __repr__(self):
        return f'_sos_group(indexes={self._indexes}, labels={self._labels})'

    def idx_to_targets(self, parent):
        ret = sos_targets([])
        ret._targets = [parent._targets[x] for x in self._indexes]
        ret._labels = self._labels
        ret._dict = self._dict
        return ret

    def __getstate__(self):
        return dict(
            indexes=self._indexes, labels=self._labels, properties=self._dict)

    def __setstate__(self, sdict):
        self._indexes = sdict['indexes']
        self._labels = sdict['labels']
        self._dict = sdict['properties']


class sos_targets(BaseTarget, Sequence, os.PathLike):
    '''A collection of targets.
    If verify_existence is True, an UnknownTarget exception
    will be thrown if target does not exist.
    '''
    # check if string contains wildcard character
    wildcard = re.compile(r'[*?\[]')

    def __init__(self,
                 *args,
                 group_by=None,
                 paired_with=None,
                 pattern=None,
                 group_with=None,
                 for_each=None,
                 _undetermined: Union[bool, str] = None,
                 _source='',
                 _verify_existence=False,
                 **kwargs):
        super(sos_targets, self).__init__()
        self._targets: List = []
        self._labels: List = []
        self._groups: List = []
        if isinstance(_undetermined, (bool, str)):
            self._undetermined = _undetermined
        else:
            self._undetermined = not bool(args)
        for arg in args:
            self.__append__(
                arg, default_source=_source, verify_existence=_verify_existence)
        for src, value in kwargs.items():
            self.__append__(
                value, source=src, verify_existence=_verify_existence)
        for t in self._targets:
            if isinstance(t, sos_targets):
                raise RuntimeError(
                    f"Nested sos_targets {t} were introduced by {args}")
            if not isinstance(t, BaseTarget):
                raise RuntimeError(f"Unrecognized target {t}")
        if _verify_existence:
            for target in self._targets:
                if not target.target_exists('any'):
                    raise UnknownTarget(target)
        if group_by:
            self._group(group_by)
        if paired_with:
            self._handle_paired_with(paired_with)
        if pattern:
            self._handle_extract_pattern(pattern)
        if group_with:
            self._handle_group_with(group_with)
        if for_each:
            self._handle_for_each(for_each)

    def set_traced(self):
        [x.set_traced() for x in self._targets]
        self.traced = True
        return self

    def is_external(self):
        if not self.valid():
            return False
        return all(x.is_external()
                   for x in self._targets
                   if isinstance(x, file_target))

    def unspecified(self):
        return not self._targets and self._undetermined is True

    def undetermined(self):
        return not self._targets and isinstance(self._undetermined, str)

    def touch(self):
        for x in self._targets:
            if isinstance(x, file_target):
                x.touch()

    def valid(self):
        return self._targets or self._undetermined is False

    def __append__(self,
                   arg,
                   source='',
                   default_source='',
                   verify_existence=False):
        src = source if source else default_source
        if isinstance(arg, paths):
            self._targets.extend([file_target(x) for x in arg._paths])
            self._labels.extend([src] * len(arg._paths))
            for g in self._groups:
                g.add_last(len(arg._paths), parent=self)
        elif isinstance(arg, path):
            self._targets.append(file_target(arg))
            self._labels.append(src)
            for g in self._groups:
                g.add_last(1, parent=self)
        elif isinstance(arg, str):
            if self.wildcard.search(arg):
                matched = sorted(glob.glob(os.path.expanduser(arg)))
                if matched:
                    self._targets.extend([file_target(x) for x in matched])
                    self._labels.extend([src] * len(matched))
                    for g in self._groups:
                        g.add_last(len(matched), parent=self)
                elif verify_existence:
                    raise UnknownTarget(arg)
                else:
                    env.logger.debug(f'Pattern {arg} does not match any file')
            else:
                self._targets.append(file_target(arg))
                self._labels.append(src)
                for g in self._groups:
                    g.add_last(1, parent=self)
        elif isinstance(arg, dict):
            for k, v in arg.items():
                if not isinstance(k, str):
                    raise ValueError(
                        f'Failed to create a sos_targets object with dictionary {arg}: source of sos_targets can only be a string: {k} of type {k.__class__.__name__} specified'
                    )
                self.__append__(v, source=k, verify_existence=verify_existence)
        elif isinstance(arg, sos_targets):
            # group handling if tricker here because arg can have
            # its own groups.
            if source:
                self.extend(arg, source=source)
            else:
                # use the source from sos_target, not from default
                self.extend(arg)
        elif isinstance(arg, BaseTarget):
            self._targets.append(arg)
            self._labels.append(src)
            for g in self._groups:
                g.add_last(1, parent=self)
        elif isinstance(arg, Iterable):
            # in case arg is a Generator, check its type will exhaust it
            for t in list(arg):
                self.__append__(t, source=src)
        elif arg is not None:
            raise RuntimeError(
                f'Unrecognized targets {arg} of type {arg.__class__.__name__}')

    def set_labels(self, source):
        if isinstance(source, str):
            self._labels = [source] * len(self._targets)
        elif len(source) == len(self._targets):
            self._labels = source
        else:
            raise ValueError(
                f'Invalid source {source} for sos_target with {len(self)} targets.'
            )

    labels = property(lambda self: self._labels, set_labels)

    targets = property(lambda self: self._targets)

    groups = property(
        lambda self: [x.idx_to_targets(self) for x in self._groups])

    def _get_group(self, index):
        return self._groups[index].idx_to_targets(self)

    #def targets(self):
    #    return [x.target_name() if isinstance(x, file_target) else x for x in self._targets]

    def later_than(self, other):
        '''if the current target is later than the other targets'''
        # if no input, output is naturally later
        file_rhs = [x for x in other.targets if isinstance(x, file_target)]
        if not file_rhs:
            return True
        # if no output, but input, we cannot skip
        file_lhs = [x for x in self._targets if isinstance(x, file_target)]
        if not file_lhs:
            return False
        # now we have both.
        return min(x.stat().st_mtime for x in file_lhs) > max(
            x.stat().st_mtime for x in file_rhs)

    def extend(self, another, source='', keep_groups=False):
        if isinstance(another, sos_targets):
            arg = another
        else:
            arg = sos_targets(another, _source=source)
        if arg.valid() and not self.valid():
            self._undetermined = False
        #
        n_old = len(self._targets)
        n_added = len(arg._targets)

        self._targets.extend(arg._targets)
        # if source is specified, override the default
        if source:
            self._labels.extend([source] * len(arg._targets))
        elif hasattr(arg, '_labels'):
            self._labels.extend(arg._labels)
        else:
            self._labels.extend([''] * len(arg._targets))
        # merge dictionaries
        self._dict.update(arg._dict)
        #
        if keep_groups:
            return self
        # it is possible to merge groups from multiple...
        if arg._groups:
            # if source is specified, it will override labels of all groups
            if not self._groups:
                self._groups = [
                    _sos_group(range(n_old), parent=self) for g in arg._groups
                ]
            if len(self._groups) == 1 and len(arg._groups) > 1:
                # 1 vs more, we duplicate itself
                self._groups = [
                    _sos_group(
                        self._groups[0]._indexes,
                        self._groups[0]._labels).set(**self._groups[0]._dict)
                    for ag in arg._groups
                ]
                for g, ag in zip(self._groups, arg._groups):
                    g.extend(ag, start=n_old, parent=self)
            elif len(self._groups) > 1 and len(arg._groups) == 1:
                for g in self._groups:
                    g.extend(arg._groups[0], start=n_old, parent=self)
            elif len(self._groups) == len(arg._groups):
                for g, ag in zip(self._groups, arg._groups):
                    g.extend(ag, start=n_old, parent=self)
            else:
                raise ValueError(
                    f'Cannot merge a sos_targets objects with {len(self._groups)} groups with another sos_targets object with {len(arg._groups)} groups.'
                )
        elif self._groups:
            # if the RHS has no _group but myself has groups...
            # source will be figured out during extend
            ag = _sos_group(range(n_added), labels=[''] * n_added)
            for g in self._groups:
                g.extend(ag, start=n_old, parent=self)
        return self

    def zap(self):
        for target in self._targets:
            if isinstance(target, file_target):
                target.zap()
            else:
                env.logger.debug(f'Ignore non-file target {target}')

    def __getstate__(self):
        return (self._targets, self._labels, self._undetermined, self._groups,
                self._dict)

    def __setstate__(self, state) -> None:
        if isinstance(state, tuple):
            if len(state) == 2:
                self._targets = state[0]
                self._labels = [''] * len(self._targets)
                self._undetermined = state[1]
                self._groups = []
                self._dict = {}
            elif len(state) == 3:
                self._targets = state[0]
                self._labels = state[1]
                self._undetermined = state[2]
                self._groups = []
                self._dict = {}
            elif len(state) == 4:
                self._targets = state[0]
                self._labels = state[1]
                self._undetermined = state[2]
                self._groups = state[3]
                self._dict = {}
            elif len(state) == 5:
                self._targets = state[0]
                self._labels = state[1]
                self._undetermined = state[2]
                self._groups = state[3]
                self._dict = state[4]
        else:
            # older version of sig file might only saved targets
            self._targets = state
            self._labels = [''] * len(self._targets)
            self._undetermined = False
            self._groups = []
            self._dict = {}

    def __len__(self):
        return len(self._targets)

    def select(self, i):
        # similar to [] but always returns a sos_targets object with appropriate source
        if isinstance(i, str):
            ret = sos_targets()
            ret._undetermined = self._undetermined
            ret._targets = [
                x for x, y in zip(self._targets, self._labels) if y == i
            ]
            index_map = {
                o_idx: n_idx for n_idx, o_idx in zip(
                    range(len(ret._targets)),
                    [x for x, y in enumerate(self._labels) if y == i])
            }
            ret._labels = [i] * len(ret._targets)
            ret._groups = []
            for grp in self._groups:
                ret._groups.append(
                    _sos_group([
                        index_map[x]
                        for x, y in zip(grp._indexes, grp._labels)
                        if y == i
                    ],
                               labels=i).set(**grp._dict))
            return ret
        elif isinstance(i, (tuple, list)):
            ret = sos_targets()
            ret._undetermined = self._undetermined
            ret._targets = [self._targets[x] for x in i]
            ret._labels = [self._labels[x] for x in i]
            ret._groups = []
            return ret
        elif callable(i):
            kept = [idx for idx, x in enumerate(self._targets) if i(x)]
            if len(kept) == len(self._targets):
                return self
            ret = sos_targets()
            ret._undetermined = self._undetermined
            ret._targets = [self._targets[x] for x in kept]
            ret._labels = [self._labels[x] for x in kept]
            ret._groups = []
            if not self._groups:
                return ret
            index_map = {
                o_idx: n_idx
                for n_idx, o_idx in zip(range(len(ret._targets)), kept)
            }
            kept = set(kept)
            for grp in self._groups:
                ret._groups.append(
                    _sos_group(
                        [index_map[x] for x in grp._indexes if x in kept], [
                            y for x, y in zip(grp._indexes, grp._labels)
                            if x in kept
                        ]).set(**grp._dict))
            return ret
        else:
            ret = sos_targets()
            ret._undetermined = self._undetermined
            ret._targets = [self._targets[i]] if isinstance(
                i, int) else self._targets[i]
            ret._labels = [self._labels[i]] if isinstance(
                i, int) else self._labels[i]
            ret._groups = []
            return ret

    def __getitem__(self, i):
        if isinstance(i, str):
            ret = sos_targets()
            ret._undetermined = self._undetermined
            ret._targets = [
                x for x, y in zip(self._targets, self._labels) if y == i
            ]
            index_map = {
                o_idx: n_idx for n_idx, o_idx in zip(
                    range(len(ret._targets)),
                    [x for x, y in enumerate(self._labels) if y == i])
            }
            ret._labels = [i] * len(ret._targets)
            ret._groups = []
            for grp in self._groups:
                ret._groups.append(
                    _sos_group([
                        index_map[x]
                        for x, y in zip(grp._indexes, grp._labels)
                        if y == i
                    ],
                               labels=i).set(**grp._dict))
            if not ret._targets:
                env.logger.warning(f'No target with label "{i}" is available.')
            return ret
        else:
            return self._targets[i]

    def target_signature(self):
        return tuple((x.target_signature(), y)
                     for x, y in zip(self._targets, self._labels))

    def validate(self, sig):
        return isinstance(sig, tuple) and len(sig) == len(
            self._targets) and all(
                x.validate(sig[0]) and src == sig[1]
                for x, src, sig in zip(self._targets, self._labels, sig))

    def target_exists(self, mode='any'):
        return all(x.target_exists(mode) for x in self._targets)

    def __getattr__(self, name):
        try:
            return self._dict[name]
        except Exception:
            if len(self._targets) == 1:
                try:
                    return getattr(self._targets[0], name)
                except Exception:
                    raise AttributeError(
                        f'{self.__class__.__name__} object or its first child has no attribute {name}'
                    )
            else:
                raise AttributeError(
                    f'{self.__class__.__name__} object of length {len(self)} has no attribute {name}'
                )

    def target_name(self):
        return f"sos_targets([{','.join(x.target_name() for x in self._targets)}],_labels=[{','.join(self._labels)}])"

    def _dedup(self):
        kept = []
        items = set()
        for i, t in enumerate(self._targets):
            if t not in items:
                kept.append(i)
                items.add(t)
        return self.remove_targets(self, kept=kept)

    def paired_with(self, name, properties):
        # can pair with sos_targets
        if not isinstance(properties,
                          sos_targets) and not is_basic_type(properties):
            env.logger.warning(
                f'Failed to paired_with with value "{properties}" as it contains unsupported data type'
            )
            return self
        if isinstance(properties, (bool, int, float, str, bytes)):
            for target in self._targets:
                target.set(name, properties)
        elif isinstance(properties, (list, tuple, sos_targets, paths)):
            if len(properties) != len(self._targets):
                raise ValueError(
                    f'Length of provided attributes ({len(properties)}) does not match length of sos_targets ({len(self._targets)})'
                )
            for target, property in zip(self._targets, properties):
                target.set(name, property)
        else:
            raise ValueError(
                f'Unacceptable properties {properties} for function paired_with'
            )
        return self

    def remove_targets(self, type, kept=None):
        '''Remove targets of certain type'''
        if kept is None:
            kept = [
                i for i, x in enumerate(self._targets)
                if not isinstance(x, type)
            ]
        if len(kept) == len(self._targets):
            return self
        self._targets = [self._targets[x] for x in kept]
        self._labels = [self._labels[x] for x in kept]
        if not self._groups:
            return self
        index_map = {
            o_idx: n_idx
            for n_idx, o_idx in zip(range(len(self._targets)), kept)
        }
        kept = set(kept)
        for idx, grp in enumerate(self._groups):
            self._groups[idx] = _sos_group(
                [index_map[x] for x in grp._indexes if x in kept],
                [y for x, y in zip(grp._indexes, grp._labels) if x in kept
                ]).set(**grp._dict)
        return self

    def resolve_remote(self):
        '''If target is of remote type, resolve it'''
        for idx, target in enumerate(self._targets):
            if isinstance(target, remote):
                resolved = target.resolve()
                if isinstance(resolved, str):
                    resolved = interpolate(resolved, env.sos_dict.dict())
                self._targets[idx] = file_target(resolved).set(**target._dict)
        return self

    def group_with(self, name, properties):
        if not self._groups:
            self._group(by='all')
        if not is_basic_type(properties):
            env.logger.warning(
                f'Failed to set {properties} as it is or contains unsupported data type'
            )
            return self
        if isinstance(properties, (bool, int, float, str, bytes)):
            for group in self._groups:
                group.set(name, properties)
        elif isinstance(properties, (list, tuple)):
            if len(properties) != len(self._groups):
                raise ValueError(
                    f'Length of provided properties ({len(properties)}) does not match number of groups ({len(self._groups)})'
                )
            for group, property in zip(self._groups, properties):
                group.set(name, property)
        else:
            raise ValueError(
                f'Unacceptable properties {properties} of type {properties.__class__.__name__} for function group_with'
            )
        return self

    def get(self, name, default=None):
        if name in self._dict:
            return self._dict[name]
        elif len(self._targets) == 1:
            return self._targets[0].get(name, default)
        else:
            return default

    def _add_groups(self, grps):
        self._groups = []

        for grp in grps:
            if not isinstance(grp, sos_targets):
                raise RuntimeError(
                    f'_output should be of type sos_targets: {grp} of type {grp.__class__.__name__} returned.'
                )
            start_idx = len(self._targets)
            grp_size = len(grp)
            self._targets.extend(grp._targets)
            self._labels.extend(grp._labels)
            self._groups.append(
                _sos_group(
                    range(start_idx, start_idx + grp_size),
                    labels=grp._labels).set(**grp._dict))
        # in theory the groups should not overlap but in rare cases when
        # output is for example dynamic, they could overlap.
        return self._dedup()

    def _remove_empty_groups(self):
        self._groups = [x for x in self._groups if len(x._indexes) > 0]
        return self

    def _duplicate_groups(self, n):
        n_grps = len(self._groups)
        for _ in range(n - 1):
            for grp in self._groups[:n_grps]:
                self._groups.append(
                    _sos_group(grp._indexes, grp._labels).set(**grp._dict))
        return self

    def _num_groups(self):
        return len(self._groups)

    def _group(self, by):
        if by is None:
            return self

        if self._groups:
            self._groups = []

        if by == 'single':
            self._groups = [
                _sos_group([x], parent=self) for x in range(len(self))
            ]
        elif by == 'all':
            # default option
            self._groups = [_sos_group(range(len(self)), self._labels)]
        elif isinstance(by, str) and (by.startswith('pairsource') or
                                      by.startswith('pairlabel')):
            labels = list(dict.fromkeys(self.labels))
            if len(labels) == 1:
                raise ValueError(f'Cannot pairlabel input with a single label.')
            if by == 'pairsource' or by == 'pairlabel':
                grp_size = 1
            else:
                try:
                    if by.startswith('pairsource'):
                        grp_size = int(by[10:])
                    else:
                        grp_size = int(by[9:])
                except Exception:
                    raise ValueError(f'Invalid pairsource option {by}')
            src_sizes = {s: self.labels.count(s) for s in labels}
            if max(src_sizes.values()) % grp_size != 0:
                raise ValueError(
                    f'Cannot use group size {grp_size} (option {by}) for source of size {src_sizes}'
                )
            n_groups = max(src_sizes.values()) // grp_size
            indexes = [[] for x in range(n_groups)]
            for s in labels:
                lookup = [
                    idx for idx, src in enumerate(self.labels) if src == s
                ]
                if src_sizes[s] > n_groups and src_sizes[s] % n_groups == 0:
                    gs = src_sizes[s] // n_groups
                    for i in range(n_groups):
                        # (0, 1, 2), (3, 4, 5), (6, 7, 8) ...
                        indexes[i].extend(lookup[i * gs:(i + 1) * gs])
                elif n_groups >= src_sizes[s] and n_groups % src_sizes[s] == 0:
                    for i in range(n_groups):
                        # (0 ), (0, ), (1, ), (1, ) ...
                        indexes[i].append(lookup[i //
                                                 (n_groups // src_sizes[s])])
                else:
                    raise ValueError(
                        f'Cannot use group size {grp_size} (by="{by}") for source of size {src_sizes}'
                    )
            self._groups = [
                _sos_group(indexes[x], parent=self) for x in range(n_groups)
            ]
        elif isinstance(by, str) and by.startswith('pairs'):
            if len(self) % 2 != 0:
                raise ValueError(
                    f'Paired by has to have even number of input files: {len(self)} provided'
                )
            if by == 'pairs':
                grp_size = 1
            else:
                try:
                    grp_size = int(by[5:])
                except Exception:
                    raise ValueError(f'Invalid pairs option {by}')
            if grp_size == 1:
                self._groups = [
                    _sos_group(x, parent=self) for x in zip(
                        range(0,
                              len(self) // 2), range(len(self) // 2, len(self)))
                ]
            else:
                if len(self) % grp_size != 0:
                    raise ValueError(
                        f'Paired by with group size {grp_size} is not possible with input of size {len(self)}'
                    )
                self._groups = [
                    _sos_group(
                        list(range(x[0], x[0] + grp_size)) +
                        list(range(x[1], x[1] + grp_size)),
                        parent=self) for x in zip(
                            range(0,
                                  len(self) // 2, grp_size),
                            range(len(self) // 2, len(self), grp_size))
                ]
        elif isinstance(by, str) and by.startswith('pairwise'):
            if by == 'pairwise':
                grp_size = 1
            else:
                try:
                    grp_size = int(by[8:])
                except Exception:
                    raise ValueError(f'Invalid pairs option {by}')
            if grp_size == 1:
                f1, f2 = tee(range(len(self)))
                next(f2, None)
                self._groups = [_sos_group(x, parent=self) for x in zip(f1, f2)]
            else:
                if len(self) % grp_size != 0:
                    raise ValueError(
                        f'Paired by with group size {grp_size} is not possible with input of size {len(self)}'
                    )
                f1, f2 = tee(range(len(self) // grp_size))
                next(f2, None)
                self._groups = [
                    _sos_group(
                        list(range(x[0] * grp_size, (x[0] + 1) * grp_size)) +
                        list(range(x[1] * grp_size, (x[1] + 1) * grp_size)),
                        parent=self) for x in zip(f1, f2)
                ]
        elif isinstance(by, str) and by.startswith('combinations'):
            if by == 'combinations':
                grp_size = 2
            else:
                try:
                    grp_size = int(by[12:])
                except Exception:
                    raise ValueError(f'Invalid pairs option {by}')
            self._groups = [
                _sos_group(x, parent=self)
                for x in combinations(range(len(self)), grp_size)
            ]
        elif by == 'source' or by == 'label':
            labels = list(dict.fromkeys(self.labels))
            self._groups = [
                _sos_group([i for i, x in enumerate(self._labels) if x == src],
                           parent=self) for src in labels
            ]
        elif isinstance(by, int) or (isinstance(by, str) and by.isdigit()):
            by = int(by)
            if len(self) % by != 0 and len(self) > by:
                env.logger.warning(
                    f'Number of samples ({len(self)}) is not a multiple of by ({by}). The last group would have less files than the other groups.'
                )
            if by < 1:
                raise ValueError(
                    'Value of paramter by should be a positive number.')
            self._groups = [
                _sos_group(range(i, min(i + by, len(self))), parent=self)
                for i in range(0, len(self), by)
            ]
        elif callable(by):
            try:
                self._groups = []
                idx = by(self)
                try:
                    idx = list(idx)
                except Exception:
                    raise ValueError(
                        f'Customized grouping method should return a list. {idx} of type {idx.__class__.__name__} is returned.'
                    )
                for grp in by(self):
                    if isinstance(grp, Sequence) and all(
                            isinstance(x, int) for x in grp):
                        if any(x < 0 or x >= len(self._targets) for x in grp):
                            raise ValueError(
                                f'Index out of range (< {len(self._targets)}): {grp}'
                            )
                        self._groups.append(_sos_group(grp, parent=self))
                    else:
                        index = []
                        for x in sos_targets(grp):
                            try:
                                index.append(self._targets.index(x))
                            except Exception:
                                raise ValueError(
                                    f'Returned target is not one of the targets. {x}'
                                )
                        self._groups.append(_sos_group(index, parent=self))
            except Exception as e:
                raise ValueError(
                    f'Failed to apply customized grouping method: {e}')
        else:
            raise ValueError(f'Unsupported by option ``{by}``!')
        return self

    def _handle_paired_with(self, paired_with):
        '''Handle input option paired_with'''
        if paired_with is None or not paired_with:
            var_name = []
            var_value = []
        elif isinstance(paired_with, str):
            var_name = ['_' + paired_with]
            if paired_with not in env.sos_dict:
                raise ValueError(f'Variable {paired_with} does not exist.')
            var_value = [env.sos_dict[paired_with]]
        elif isinstance(paired_with, dict):
            var_name = []
            var_value = []
            for k, v in paired_with.items():
                var_name.append(k)
                var_value.append(v)
        elif isinstance(paired_with, Iterable):
            try:
                var_name = ['_' + x for x in paired_with]
            except Exception:
                raise ValueError(
                    f'Invalud value for option paired_with {paired_with}')
            var_value = []
            for vn in var_name:
                if vn[1:] not in env.sos_dict:
                    raise ValueError(f'Variable {vn[1:]} does not exist.')
                var_value.append(env.sos_dict[vn[1:]])
        else:
            raise ValueError(
                f'Unacceptable value for parameter paired_with: {paired_with}')
        #
        for vn, vv in zip(var_name, var_value):
            # set paired with values to step_input
            self.paired_with(vn, vv)

    def _handle_group_with(self, group_with):
        '''Handle input option group_with'''
        if group_with is None or not group_with:
            var_name = []
            var_value = []
        elif isinstance(group_with, str):
            var_name = ['_' + group_with]
            if group_with not in env.sos_dict:
                raise ValueError(f'Variable {group_with} does not exist.')
            var_value = [env.sos_dict[group_with]]
        elif isinstance(group_with, dict):
            var_name = []
            var_value = []
            for k, v in group_with.items():
                var_name.append(k)
                var_value.append(v)
        elif isinstance(group_with, Iterable):
            try:
                var_name = ['_' + x for x in group_with]
            except Exception:
                raise ValueError(
                    f'Invalud value for option group_with {group_with}')
            var_value = []
            for vn in var_name:
                if vn[1:] not in env.sos_dict:
                    raise ValueError(f'Variable {vn[1:]} does not exist.')
                var_value.append(env.sos_dict[vn[1:]])
        else:
            raise ValueError(
                f'Unacceptable value for parameter group_with: {group_with}')
        #
        for vn, vv in zip(var_name, var_value):
            self.group_with(vn, vv)

    def _handle_extract_pattern(self, pattern):
        '''Handle input option pattern'''
        if pattern is None or not pattern:
            patterns = []
        elif isinstance(pattern, str):
            patterns = [pattern]
        elif isinstance(pattern, Iterable):
            patterns = pattern
        else:
            raise ValueError(
                f'Unacceptable value for parameter pattern: {pattern}')
        #
        for pattern in patterns:
            res = extract_pattern(pattern, self._targets)
            self.set(**res)
            # also make k, v pair with _input
            self._handle_paired_with({'_' + x: y for x, y in res.items()})

    def _handle_for_each(self, for_each):
        if for_each is None or not for_each:
            for_each = []
        elif isinstance(for_each, (str, dict)):
            for_each = [for_each]
        elif isinstance(for_each, Sequence):
            for_each = for_each
        else:
            raise ValueError(
                f'Unacceptable value for parameter for_each: {for_each}')
        #
        for fe_all in for_each:
            if isinstance(fe_all, dict):
                # in the format of {'name': value}
                fe_iter_names = []
                fe_values = []
                for k, v in fe_all.items():
                    if ',' in k:
                        names = [x.strip() for x in k.split(',')]
                        if isinstance(v, Iterable):
                            v = list(v)
                        if any(len(_v) != len(names) for _v in v):
                            raise ValueError(
                                f'Unable to unpack object {short_repr(v)} for variables {k} (of length {len(names)})'
                            )
                        fe_iter_names.extend(names)
                        fe_values.extend(list(zip(*v)))
                    else:
                        fe_iter_names.append(k)
                        fe_values.append(v)
            else:
                if ',' in fe_all:
                    fe_var_names = [x.strip() for x in fe_all.split(',')]
                    fe_iter_names = ['_' + x for x in fe_var_names]
                else:
                    fe_var_names = [fe_all]
                    fe_iter_names = ['_' + fe_all]
                # check iterator variable name
                for name in fe_iter_names:
                    if '.' in name:
                        raise ValueError(f'Invalid iterator variable {name}')
                # check variables
                fe_values = []
                for name in fe_var_names:
                    if name.split('.')[0] not in env.sos_dict:
                        raise ValueError(f'Variable {name} does not exist.')
                    if '.' in name:
                        fe_values.append(
                            getattr(env.sos_dict[name.split('.')[0]],
                                    name.split('.', 1)[-1]))
                    else:
                        fe_values.append(env.sos_dict[name])

            # get loop size
            loop_size = None
            for name, values in zip(fe_iter_names, fe_values):
                if not isinstance(values, Sequence):
                    try:
                        import pandas as pd
                        if not isinstance(values,
                                          (pd.DataFrame, pd.Series, pd.Index)):
                            raise ValueError(
                                f'Unacceptable for_each data type {values.__class__.__name__}'
                            )
                    except Exception as e:
                        raise ValueError(
                            f'Cannot iterate through variable {name}: {e}')
                if loop_size is None:
                    loop_size = len(values)
                elif loop_size != len(values):
                    raise ValueError(
                        f'Length of variable {name} (length {len(values)}) should match the length of other variables (length {loop_size}).'
                    )

            n_grps = self._num_groups()
            if n_grps == 0:
                self._group('all')
                n_grps = 1
            self._duplicate_groups(loop_size)
            #
            for vidx in range(loop_size):
                for idx in range(n_grps):
                    for var_name, values in zip(fe_iter_names, fe_values):
                        if isinstance(values, Sequence):
                            self._groups[n_grps * vidx + idx].set(
                                var_name, values[vidx])
                        elif isinstance(values, pd.DataFrame):
                            self._groups[n_grps * vidx + idx].set(
                                var_name, values.iloc[vidx])
                        elif isinstance(values, pd.Series):
                            self._groups[n_grps * vidx + idx].set(
                                var_name, values.iloc[vidx])
                        elif isinstance(values, pd.Index):
                            self._groups[n_grps * vidx + idx].set(
                                var_name, values[vidx])
                        else:
                            raise ValueError(
                                f'Failed to iterate through for_each variable {short_repr(values)}'
                            )

    def __hash__(self):
        return hash(repr(self))

    def __eq__(self, other):
        try:
            # allow compare to any object as long as it can be converted to sos_targets
            return self._targets == (
                other._targets if isinstance(other, sos_targets) else
                sos_targets(other)._targets)
        except Exception:
            return False

    def __add__(self, part):
        if len(self._targets) == 1:
            return self._targets[0].__add__(part)
        elif len(self._targets) == 0:
            raise ValueError(f"Cannot add {part} to empty target list")
        else:
            raise ValueError(
                f'Cannot add {part} to group of {len(self)} targets {self!r}')

    def __fspath__(self):
        if len(self._targets) == 1:
            return self._targets[0].__fspath__()
        elif len(self._targets) == 0:
            raise ValueError(
                f"Cannot treat an empty sos_targets as single target")
        else:
            raise ValueError(
                f'Cannot treat an sos_targets object {self} with more than one targets as a single target'
            )

    def __repr__(self):
        return ('[' + ', '.join(repr(x) for x in self._targets) +
                ']') if self.valid() else (
                    'Unspecified' if self.unspecified() else self._undetermined)

    def __short_repr__(self):
        grp_info = '' if self._num_groups(
        ) <= 1 else f' in {self._num_groups()} groups'
        if self.valid():
            if len(self._targets) <= 2:
                return ' '.join([x.target_name() for x in self._targets
                                ]) + grp_info
            else:
                return ' '.join([
                    x.target_name() for x in self._targets[:2]
                ]) + f'... ({len(self._targets)} items{grp_info})'
        else:
            return 'Unspecified' if self.unspecified() else self._undetermined

    def __stable_repr__(self):
        return repr(self)

    def __str__(self):
        return self.__format__('') if self.valid() else (
            'Unspecified' if self.unspecified() else self._undetermined)

    def __format__(self, format_spec):
        if not self.valid():
            return 'Unspecified' if self.unspecified() else self._undetermined
        if ',' in format_spec:
            fmt_spec = format_spec.replace(',', '')
            return ','.join(x.__format__(fmt_spec) for x in self._targets)
        else:
            return ' '.join(x.__format__(format_spec) for x in self._targets)

    def __deepcopy__(self, memo):
        ret = sos_targets()
        ret._targets = copy.deepcopy(self._targets)
        ret._groups = copy.deepcopy(self._groups)
        ret._labels = copy.deepcopy(self._labels)
        ret._dict = copy.deepcopy(self._dict)
        ret._undetermined = self._undetermined
        return ret

    def contains(self, target):
        if isinstance(target, str):
            return file_target(target) in self._targets
        else:
            return target in self._targets


class InMemorySignature:

    def __init__(self,
                 input_files: sos_targets,
                 output_files: sos_targets,
                 dependent_files: sos_targets,
                 signature_vars: set = set(),
                 sdict: dict = {},
                 shared_vars: list = []):
        '''Runtime information for specified output files
        '''
        self.content = None
        if not sdict:
            sdict = env.sos_dict
        if not input_files.valid():
            raise RuntimeError(
                'Input files of step signature cannot be undetermined.')
        if not dependent_files.valid():
            raise RuntimeError(
                'Dependent files of step signature cannot be undetermined.')

        self.input_files = input_files.remove_targets(type=sos_step)
        self.dependent_files = dependent_files.remove_targets(type=sos_step)
        self.output_files = output_files.remove_targets(type=sos_step)
        self.signature_vars = signature_vars
        self.shared_vars = shared_vars
        # signatures that exist before execution and might change during execution
        self.init_signature = {
            x: deepcopy(sdict[x]) for x in sorted(signature_vars) if
            x in sdict and not callable(sdict[x]) and pickleable(sdict[x], x)
        }

    def identify_local_args(self):
        # #1372
        # unlike global parameters that have been executed and padded,
        # local paramters needs to be in the signature so that steps can be
        # effectively skipped.
        if not self.signature_vars or not env.config['workflow_args']:
            return []

        params = {x: [] for x in sorted(self.signature_vars)}

        with_param = False
        for par in env.config['workflow_args']:
            if par.startswith('--'):
                parname = par.split('=')[0][2:] if '=' in par else par[2:]
                parname = parname.replace('-', '_')
                if parname in self.signature_vars:
                    params[parname].append(par)
                    with_param = parname
            elif par.startswith('-'):
                with_param = False
            elif with_param:
                params[with_param].append(par)
        return sum(params.values(), [])

    def write(self):
        if self.content is not None:
            return self.content
        if self.output_files.undetermined():
            self.output_files = env.sos_dict['_output']
            env.log_to_file(
                'TARGET',
                f'Set undetermined output files to {env.sos_dict["_output"]}')
        input_sig = {}
        for f in self.input_files:
            try:
                input_sig[str(f)] = f.target_signature()
            except Exception:
                env.logger.debug(
                    f'Failed to create signature: input target {f} does not exist'
                )
                return False
        output_sig = {}
        for f in self.output_files:
            try:
                output_sig[str(f)] = f.target_signature()
            except Exception:
                env.logger.debug(
                    f'Failed to create signature: output target {f} does not exist'
                )
                return False
        dependent_sig = {}
        for f in self.dependent_files:
            try:
                dependent_sig[str(f)] = f.target_signature()
            except Exception:
                env.logger.debug(
                    f'Failed to create signature: dependent target {f} does not exist'
                )
                return False
        init_context_sig = {
            var: objectMD5(self.init_signature[var])
            for var in self.init_signature
            if pickleable(self.init_signature[var], var)
        }
        if self.shared_vars:
            end_context = {
                var: env.sos_dict[var]
                for var in self.shared_vars
                if var in env.sos_dict and pickleable(env.sos_dict[var], var)
            }
        else:
            end_context = {}

        self.content = {
            'input': input_sig,
            'input_obj': self.input_files,
            'output': output_sig,
            'output_obj': self.output_files,
            'depends': dependent_sig,
            'depends_obj': self.dependent_files,
            'init_context_sig': init_context_sig,
            'end_context': end_context,
            'local_args': self.identify_local_args(),
        }
        return self.content

    def validate(self, signature):
        '''Check if ofiles and ifiles match signatures recorded in md5file'''
        if not signature:
            return 'Empty signature'
        sig_files = self.input_files._targets + self.output_files._targets + \
            self.dependent_files._targets
        for x in sig_files:
            if not x.target_exists('any'):
                return f'Missing target {x}'
        #

        files_checked = {x.target_name(): False for x in sig_files}
        res = {'input': [], 'output': [], 'depends': [], 'vars': {}}
        cur_type = 'input'
        # old signature
        if 'init_context' in signature:
            for key, value in signature['init_context'].items():
                if key not in env.sos_dict:
                    return f'Variable {key} not in running environment'
                try:
                    if env.sos_dict[key] != value:
                        return f'Context variable {key} value mismatch: {short_repr(value)} saved, {short_repr(env.sos_dict[key])} current'
                except Exception as e:
                    env.logger.debug(
                        f"Variable {key} of type {type(value).__name__} cannot be compared: {e}"
                    )
        elif 'init_context_sig' in signature:
            for key, value in signature['init_context_sig'].items():
                if key not in env.sos_dict:
                    return f'Variable {key} not in running environment'
                try:
                    if objectMD5(env.sos_dict[key]) != value:
                        return f'ID of context variable {key} ({objectMD5(env.sos_dict[key])}) mismatch: {short_repr(env.sos_dict[key])} does not match id {value}'
                except Exception as e:
                    env.logger.debug(f"Variable {key} cannot be compared: {e}")

        if 'local_args' in signature:
            if signature['local_args'] != self.identify_local_args():
                return f'Local parameters differ'

        res['vars'].update(signature['end_context'])
        #
        for cur_type in ['input', 'output', 'depends']:
            for f, m in signature[cur_type].items():
                try:
                    if '(' in f and ')' in f:
                        # this part is hard, because this can be a customized target.
                        target_type = f.split('(')[0]
                        target_class = None
                        if target_type in globals():
                            target_class = eval(target_type)
                        else:
                            # check registry
                            for entrypoint in pkg_resources.iter_entry_points(
                                    group='sos_targets'):
                                if entrypoint.name.strip() == target_type:
                                    target_class = entrypoint.load()
                                    break
                        if target_class is None:
                            raise ValueError(
                                f'Failed to identify target class {target_type}'
                            )
                        # parameter of class?
                        freal = eval(f, {target_type: target_class})
                    else:
                        freal = file_target(f)
                    if not freal.validate(m):
                        return f'Target {f} does not exist or does not match saved signature {m}'
                    res[cur_type].append(freal.target_name(
                    ) if isinstance(freal, file_target) else freal)
                    files_checked[freal.target_name()] = True
                except Exception as e:
                    env.logger.debug(f'Wrong md5 in signature: {e}')
        #
        if not all(files_checked.values()):
            return f'No MD5 signature for {", ".join(x for x,y in files_checked.items() if not y)}'
        if 'input_obj' in signature:
            # for new style signature, the entire objects are kept
            res['input'] = signature['input_obj']
            res['depends'] = signature['depends_obj']
            res['output'] = signature['output_obj']
        return res


class RuntimeInfo(InMemorySignature):
    '''Record run time information related to a number of output files. Right now only the
    .exe_info files are used.
    '''

    def __init__(self,
                 step_md5: str,
                 input_files: sos_targets,
                 output_files: sos_targets,
                 dependent_files: sos_targets,
                 signature_vars: set = set(),
                 sdict: dict = {},
                 shared_vars: list = []):
        '''Runtime information for specified output files
        '''
        if 'sos_run' in signature_vars:
            # if a step has nested workflow, we cannot save signature
            # because we do not know the exact content of the nested workflow.
            self.sig_id = ''
            return

        if not sdict:
            sdict = env.sos_dict
        self.step_md5 = step_md5
        super(RuntimeInfo, self).__init__(
            input_files,
            output_files,
            dependent_files,
            signature_vars,
            shared_vars=shared_vars)

        self.sig_id = textMD5(
            f'{self.step_md5} {self.input_files} {self.output_files} {self.dependent_files} {stable_repr(self.init_signature)}{sdict["_index"] if self.output_files.undetermined() else ""}'
        )

    def __getstate__(self):
        if not self.sig_id:
            return {}
        return {
            'step_md5': self.step_md5,
            'input_files': self.input_files,
            'output_files': self.output_files,
            'dependent_files': self.dependent_files,
            'signature_vars': self.signature_vars,
            'init_signature': self.init_signature,
            'sig_id': self.sig_id
        }

    def __setstate__(self, sdict: Dict[str, Any]):
        if not sdict:
            self.sig_id = ''
            return
        self.step_md5 = sdict['step_md5']
        self.input_files = sdict['input_files']
        self.output_files = sdict['output_files']
        self.dependent_files = sdict['dependent_files']
        self.signature_vars = sdict['signature_vars']
        self.init_signature = sdict['init_signature']
        self.sig_id = sdict['sig_id']

    def lock(self):
        if not self.sig_id:
            return
        # we will need to lock on a file that we do not really write to
        # otherwise the lock will be broken when we write to it.
        self._lock = fasteners.InterProcessLock(
            os.path.join(env.temp_dir, self.sig_id + '.lock'))
        if not self._lock.acquire(blocking=False):
            self._lock = None
            raise UnavailableLock((self.input_files, self.output_files,
                                   os.path.join(env.temp_dir,
                                                self.sig_id + '.lock')))
        else:
            env.log_to_file(
                'TARGET',
                f'Lock acquired for output files {short_repr(self.output_files)}'
            )

    def release(self, quiet=False):
        if not self.sig_id:
            return
        if not hasattr(self, '_lock') or self._lock is None:
            env.logger.warning(
                f'Releasing an non-existent or released lock for {self.sig_id}.'
            )
            return
        if self._lock:
            try:
                self._lock.release()
                env.log_to_file(
                    'TARGET',
                    f'Lock released for output files {short_repr(self.output_files)}'
                )
            except Exception as e:
                if not quiet:
                    env.logger.warning(
                        f'Unable to release lock for output files {self.output_files}: {e}'
                    )
            finally:
                self._lock = None

    def set_output(self, files: sos_targets):
        if not self.sig_id:
            return
        # add signature file if input and output files are dynamic
        if 'TARGET' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                'SOS_DEBUG']:
            env.log_to_file('TARGET', f'Set output of signature to {files}')
        self.output_files = files

    def write(self):
        '''Write signature file with signature of script, input, output and dependent files.
        Because local input and output files can only be determined after the execution
        of workflow. They are not part of the construction.
        '''
        if not self.sig_id:
            return False
        if not self.output_files.valid():
            raise ValueError(
                f'Cannot write signature with undetermined output {self.output_files}'
            )
        else:
            if 'TARGET' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                    'SOS_DEBUG']:
                env.log_to_file(
                    'TARGET',
                    f'write signature {self.sig_id} with output {self.output_files}'
                )
        ret = super(RuntimeInfo, self).write()
        if ret is False:
            env.logger.debug(f'Failed to write signature {self.sig_id}')
            return ret
        send_message_to_controller(['step_sig', self.sig_id, ret])
        send_message_to_controller([
            'workflow_sig', 'tracked_files', self.sig_id,
            repr({
                'input_files': [
                    str(f.resolve())
                    for f in self.input_files
                    if isinstance(f, file_target)
                ],
                'dependent_files': [
                    str(f.resolve())
                    for f in self.dependent_files
                    if isinstance(f, file_target)
                ],
                'output_files': [
                    str(f.resolve())
                    for f in self.output_files
                    if isinstance(f, file_target)
                ]
            })
        ])
        return True

    def validate(self):
        '''Check if ofiles and ifiles match signatures recorded in md5file'''
        if not self.sig_id:
            return f'no signature for steps with nested workflow'
        if 'TARGET' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                'SOS_DEBUG']:
            env.log_to_file('TARGET', f'Validating {self.sig_id}')
        #
        # file not exist?
        sig_files = self.input_files._targets + self.output_files._targets + \
            self.dependent_files._targets
        for x in sig_files:
            if not x.target_exists('any'):
                return f'Missing target {x}'
        #
        sig = request_answer_from_controller(['step_sig', 'get', self.sig_id])
        if not sig:
            return f"No signature found for {self.sig_id}"
        return super(RuntimeInfo, self).validate(sig)
