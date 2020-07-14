#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import ast
import argparse
import base64
import copy
import getpass
import logging
import math
import os
import pickle
import psutil
import re
import sys
import socket
import tempfile
import threading
import time
import traceback
import types
import urllib
from zmq.log.handlers import PUBHandler

import urllib.parse
import urllib.request
from collections.abc import Sequence, Mapping, Set, KeysView
from collections import defaultdict
from html.parser import HTMLParser
from io import FileIO, StringIO, BytesIO
from typing import Optional, List, Dict

import fasteners
import yaml
from tqdm import tqdm as ProgressBar

__all__ = ['get_output']

COLOR_CODE = {
    'ENDC': 0,  # RESET COLOR
    'BOLD': 1,
    'UNDERLINE': 4,
    'BLINK': 5,
    'INVERT': 7,
    'CONCEALD': 8,
    'STRIKE': 9,
    'GREY30': 90,
    'GREY40': 2,
    'GREY65': 37,
    'GREY70': 97,
    'GREY20_BG': 40,
    'GREY33_BG': 100,
    'GREY80_BG': 47,
    'GREY93_BG': 107,
    'DARK_RED': 31,
    'RED': 91,
    'RED_BG': 41,
    'LIGHT_RED_BG': 101,
    'DARK_YELLOW': 33,
    'YELLOW': 93,
    'YELLOW_BG': 43,
    'LIGHT_YELLOW_BG': 103,
    'DARK_BLUE': 34,
    'BLUE': 94,
    'BLUE_BG': 44,
    'LIGHT_BLUE_BG': 104,
    'DARK_MAGENTA': 35,
    'PURPLE': 95,
    'MAGENTA_BG': 45,
    'LIGHT_PURPLE_BG': 105,
    'DARK_CYAN': 36,
    'AUQA': 96,
    'CYAN_BG': 46,
    'LIGHT_AUQA_BG': 106,
    'DARK_GREEN': 32,
    'GREEN': 92,
    'GREEN_BG': 42,
    'LIGHT_GREEN_BG': 102,
    'BLACK': 30,
}


def colorstr(astr: str, color: Optional[str] = None) -> str:
    color_code = 0 if color is None else COLOR_CODE[color]
    if sys.platform == 'win32':
        return astr
    else:
        return f'\033[{color_code}m{astr}\033[0m'


def emphasize(msg: str, color: Optional[str] = None):
    level_color = 0 if color is None else COLOR_CODE[color]
    # display text within `` and `` in green
    if sys.platform == 'win32':
        return str(msg).replace('``', '')
    elif level_color == 0:
        return re.sub(r'``([^`]*)``', '\033[32m\\1\033[0m', str(msg))
    else:
        return re.sub(r'``([^`]*)``',
                      f'\033[0m\033[32m\\1\033[0m\033[{level_color}m', str(msg))


class ColoredFormatter(logging.Formatter):
    ''' A logging formatter that uses color to differntiate logging messages
    and emphasize texts. Texts that would be empahsized are quoted with
    double backslashes (`` ``).
    '''

    def __init__(self, msg: str):
        logging.Formatter.__init__(self, msg)
        #
        # color for different logging levels. The current terminal color
        # is used for INFO
        self.LEVEL_COLOR = {
            'DEBUG': 'DARK_CYAN',
            'WARNING': 'PURPLE',
            'ERROR': 'RED',
            'CRITICAL': 'RED_BG',
        }

    def format(self, record):
        level_name = record.levelname
        if level_name in self.LEVEL_COLOR:
            level_color = self.LEVEL_COLOR[level_name]
            record.color_levelname = colorstr(level_name, level_color)
            record.color_name = colorstr(record.name, level_color)
            record.color_msg = colorstr(
                emphasize(record.msg, level_color), level_color)
        else:
            # for INFO, use default color
            record.color_levelname = record.levelname
            record.color_msg = emphasize(record.msg)
        # for testing certain error message
        # if 'runtime' in record.color_msg:
        #     print(get_traceback())
        return logging.Formatter.format(self, record)


def short_repr(obj, noneAsNA=False):
    '''Return a short representation of obj for clarity.'''
    if obj is None:
        return 'unspecified' if noneAsNA else 'None'
    elif isinstance(obj, str) and len(obj) > 80:
        return '{}...{}'.format(obj[:60].replace('\n', '\\n'),
                                obj[-20:].replace('\n', '\\n'))
    elif isinstance(obj, (str, int, float, bool)):
        return repr(obj)
    elif hasattr(obj, '__short_repr__'):
        return obj.__short_repr__()
    elif isinstance(obj, Sequence):  # should be a list or tuple
        if len(obj) == 0:
            return '[]'
        elif len(obj) == 1:
            return f'{short_repr(obj[0])}'
        elif len(obj) == 2:
            return f'{short_repr(obj[0])}, {short_repr(obj[1])}'
        else:
            return f'{short_repr(obj[0])}, {short_repr(obj[1])}, ... ({len(obj)} items)'
    elif isinstance(obj, dict):
        if not obj:
            return ''
        elif len(obj) == 1:
            first_key = list(obj.keys())[0]
            return f'{short_repr(first_key)!r}:{short_repr(obj[first_key])!r}'
        else:
            first_key = list(obj.keys())[0]
            return f'{short_repr(first_key)}:{short_repr(obj[first_key])}, ... ({len(obj)} items)'
    elif isinstance(obj, KeysView):
        if not obj:
            return ''
        elif len(obj) == 1:
            return short_repr(next(iter(obj)))
        else:
            return f'{short_repr(next(iter(obj)))}, ... ({len(obj)} items)'
    # elif hasattr(obj, 'target_name'):
    #    return obj.target_name()
    else:
        ret = str(obj)
        if len(ret) > 40:
            return f'{repr(obj)[:35]}...'
        else:
            return ret


#
# SoS Workflow dictionary
#


class WorkflowDict(object):
    """A dictionary object that keeps all SoS workflow objects.

    IMPORTANT:

    Python does not allow the passing of a derived class of dict as globals
    to eval or exec. Doing so will result in strange behavior such as __builtins__
    not found. We then have to embed a real dictionary in WorkflowDict instead of
    deriving a dict from it.
    """

    def __init__(self, *args, **kwargs):
        self._dict = dict(*args, **kwargs)

    def dict(self):
        return self._dict

    def clear(self):
        self._dict.clear()

    def set(self, key, value):
        '''A short cut to set value to key without triggering any logging
        or warning message.'''
        if hasattr(value, 'labels'):
            if 'VARIABLE' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                    'SOS_DEBUG']:
                env.log_to_file(
                    'VARIABLE',
                    f"Set {key} to {short_repr(value)} with labels {short_repr(value.labels)}"
                )
        else:
            if 'VARIABLE' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                    'SOS_DEBUG']:
                env.log_to_file(
                    'VARIABLE',
                    f"Set {key} to {short_repr(value)} of type {value.__class__.__name__}"
                )
        self._dict[key] = value
        # if self._change_all_cap_vars is not None and key.isupper():
        #    self._check_readonly(key, value)

    def quick_update(self, obj):
        '''Update without sanity check etc. For fast internal update'''
        self._dict.update(obj)

    def update(self, obj):
        '''Redefine update to trigger logging message'''
        self._dict.update(obj)
        for k, v in obj.items():
            # if k.isupper():
            #    self._check_readonly(k, v)
            if env.verbosity > 2:
                self._log(k, v)

    def __contains__(self, key):
        return key in self._dict

    def __getattr__(self, attr):
        # for attributes that cannot be found, default to dictionary attribute
        # (e.g. keys, pop, get...)
        return getattr(self._dict, attr)

    def __getitem__(self, key):
        return self._dict[key]

    def __setitem__(self, key, value):
        '''Set value to key, trigger logging and warning messages if needed'''
        if env.verbosity > 2:
            self._log(key, value)
        if env.config['run_mode'] == 'prepare':
            self._warn(key, value)
        if key in ('input', 'output', 'depends', '_input', '_output',
                   '_depends', '_runtime'):
            raise ValueError(f'Variable {key} can only be set by SoS')
        self.set(key, value)

    def _log(self, key, value):
        if 'VARIABLE' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                'SOS_DEBUG']:
            env.log_to_file('VARIABLE',
                            f'Set ``{key}`` = ``{short_repr(value)}``')

    def _warn(self, key, value):
        if key.startswith('_') and not key.startswith('__') and key not in (
                '_input', '_output', '_step', '_index', '_depends', '_runtime'):
            env.logger.warning(
                f'{key}: Variables with leading underscore is reserved for SoS temporary variables.'
            )

    def clone_selected_vars(self, selected=None):
        return {
            x: copy.deepcopy(y)
            for x, y in self._dict.items()
            if (not selected or x in selected) and pickleable(y, x)
        }


#
# Runtime environment
#


class RuntimeEnvironments(object):
    '''A singleton object that provides runtime environment for SoS.
    Atributes of this object include:

    logger:
        a logging object

    verbosity:
        a verbosity level object that sets the verbosity level of the logger

    logfile:
        name of logfile for the logger. default to no logfile.

    '''
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(RuntimeEnvironments, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # sockets that will be initialized by the controller code
        self.zmq_context = None
        self.master_push_socket = None
        self.master_request_socket = None

        self._sub_idx = 0
        self._sub_envs = [{}]

        # this function is used by tests to reset environments
        # after finishing an test
        self.reset()

    def request_new(self):
        old_idx = self._sub_idx
        # if we can find an old empty env, use it
        for idx, subenv in enumerate(self._sub_envs):
            if idx != old_idx and not subenv:
                self.switch(idx)
                return idx, old_idx
        # otherwise allocate a new one
        self.switch(len(self._sub_envs))
        return self._sub_idx, old_idx

    def restore_to_old(self, new_idx, old_idx):
        self.switch(old_idx)
        env._sub_envs[new_idx].clear()

    def switch(self, idx):
        # save old env
        if idx == self._sub_idx:
            return
        self._sub_envs[self._sub_idx]['sos_dict'] = self.sos_dict
        self._sub_envs[self._sub_idx]['config'] = copy.deepcopy(env.config)
        self._sub_envs[self._sub_idx]['socket'] = env.__socket__ if hasattr(
            env, '__socket__') else None
        if len(self._sub_envs) <= idx:
            self._sub_envs.append({
                'sos_dict': WorkflowDict(),
                'config': copy.deepcopy(env.config),
                'socket': env.__socket__ if hasattr(env, '__socket__') else None
            })
        if not self._sub_envs[idx]:
            self._sub_envs[idx] = {
                'sos_dict': WorkflowDict(),
                'config': copy.deepcopy(env.config),
                'socket': env.__socket__ if hasattr(env, '__socket__') else None
            }
        self.sos_dict = self._sub_envs[idx]['sos_dict']
        env.config = self._sub_envs[idx]['config']
        env.__socket__ = self._sub_envs[idx]['socket']
        self._sub_idx = idx
        # env.logger.error(f"{os.getpid()} switch to {idx} {env.sos_dict.get('num', 'unknown')}")

    _exec_dir = None
    _temp_dir = os.path.join(tempfile.gettempdir(), getpass.getuser(), '.sos')

    def log_to_file(self, topic, msg=''):
        # if only one parameter is given, assuming ALL topic and topic is the message
        if not msg:
            self.logger.debug(topic)
            return
        if topic not in self.config['SOS_DEBUG'] and 'ALL' not in self.config[
                'SOS_DEBUG']:
            return
        self.logger.debug(topic + ' - ' + str(msg))

    def reset(self):
        # logger
        self._logger = None
        self._verbosity: int = 2
        self._logging_socket = None
        self._set_logger()

        #
        # run mode, this mode controls how SoS actions behave
        #
        self.config = defaultdict(str)
        self.config.update({
            'config_file': None,
            'output_dag': None,
            'output_report': None,
            'wait_for_task': None,
            'default_queue': '',
            'worker_procs': ['2'],
            'max_running_jobs': None,
            'sig_mode': 'default',
            'run_mode': 'run',
            'verbosity': 1,
            # determined later
            'master_id': '',
            'SOS_DEBUG': set(),
        })
        if 'SOS_DEBUG' in os.environ:
            self.config['SOS_DEBUG'] = set([
                x for x in os.environ['SOS_DEBUG'].split(',')
                if '.' not in x and x != '-'
            ])
        #
        # global dictionaries used by SoS during the
        # execution of SoS workflows
        self.sos_dict = WorkflowDict()
        # parameters of the workflow, which will be handled differently
        self.parameter_vars = set()
        #
        # this directory will be used by a lot of processes
        self.exec_dir = os.getcwd()

        os.makedirs(
            os.path.join(os.path.expanduser('~'), '.sos', 'tasks'),
            exist_ok=True)

    #
    # attribute logger
    #

    def _set_logger(self, unused=None):
        # create a logger, we current use the regular logger but we should
        # switch to multiprocessing.get_logger if we notice trouble in, for example,
        # logging from multiple processes.
        self._logger = logging.getLogger()
        # clear previous handler
        while self._logger.hasHandlers():
            self._logger.removeHandler(self._logger.handlers[0])
        levels = {
            0: logging.ERROR,
            1: logging.WARNING,
            2: logging.INFO,
            3: logging.DEBUG,
            4: logging.DEBUG,
            None: logging.INFO
        }
        # stop setting root logger always to DEBUG. If SOS_DEBUG is defined as PROFILE, we profile
        # in non-debug mode. Otherwise debugging leval will be set to DEBUG
        debug_mode = 'SOS_DEBUG' in os.environ and os.environ[
            'SOS_DEBUG'] and os.environ['SOS_DEBUG'] != 'PROFILE'
        self._logger.setLevel(
            logging.DEBUG if debug_mode else levels[self._verbosity])

        if self._logging_socket:
            socket_handler = PUBHandler(self._logging_socket)
            # debug informaiton and time is always written to the log file
            socket_handler.setLevel(levels[self._verbosity])

            # ch.setFormatter(logging.Formatter(
            #    '%(asctime)s: %(levelname)s: %(message)s'))
            self._logger.addHandler(socket_handler)
            # also log to file for debugging purposes
            # ch = logging.FileHandler(os.path.join(os.path.expanduser('~'), 'sos_debug.log'), mode='a')
            # debug informaiton and time is always written to the log file
            # ch.setLevel(logging.TRACE)
            # ch.setFormatter(logging.Formatter(
            #    '%(asctime)s: %(levelname)s: %(message)s'))
            # self._logger.addHandler(ch)
        else:
            # output to standard output
            cout = logging.StreamHandler()
            cout.setLevel(levels[self._verbosity])
            cout.setFormatter(
                ColoredFormatter('%(color_levelname)s: %(color_msg)s'))
            self._logger.addHandler(cout)

        if debug_mode:
            logfile_info = [
                x for x in os.environ['SOS_DEBUG'].split(',')
                if '.' in x or x == '-'
            ]
            if logfile_info:
                if logfile_info[0] == '-':
                    logfile = logging.StreamHandler()
                    formatter = ColoredFormatter(
                        '%(color_levelname)s: %(color_msg)s')
                else:
                    logfile = logging.FileHandler(
                        os.path.expanduser(logfile_info[0]), mode='a')
                    formatter = logging.Formatter(
                        '%(asctime)s - %(levelname)s - %(message)s')
            else:
                logfile = logging.FileHandler(
                    os.path.join(
                        os.path.expanduser('~'), '.sos', 'sos_debug.log'),
                    mode='a')
                formatter = logging.Formatter(
                    '%(asctime)s - %(levelname)s - %(message)s')
            logfile.setFormatter(formatter)
            logfile.setLevel(logging.DEBUG)
            self._logger.addHandler(logfile)

    #
    # attribute exec_dir

    def _set_exec_dir(self, edir):
        if not os.path.isdir(edir):
            raise RuntimeError(f'Exec dir {edir} does not exist.')
        os.makedirs(os.path.join(edir, '.sos'), exist_ok=True)
        self._exec_dir = edir

    def _get_exec_dir(self):
        if self._exec_dir is None:
            raise RuntimeError('Exec dir is not set')
        os.makedirs(os.path.join(self._exec_dir, '.sos'), exist_ok=True)
        return self._exec_dir

    exec_dir = property(_get_exec_dir, _set_exec_dir)

    # attribute temp_dir
    def _get_temp_dir(self):
        if not os.path.isdir(self._temp_dir):
            os.makedirs(self._temp_dir, exist_ok=True)
        return self._temp_dir

    temp_dir = property(_get_temp_dir)

    #
    # attribute logger
    #
    @property
    def logger(self):
        return self._logger

    #
    # attribute verbosity
    #

    def _set_verbosity(self, v):
        if v in [0, 1, 2, 3, 4]:
            self._verbosity = v
            # reset logger to appropriate logging level
            self._set_logger()

    #
    verbosity = property(lambda self: self._verbosity, _set_verbosity)

    #
    # attribute logfile
    #

    def set_socket_logger(self, socket):
        self._logging_socket = socket
        # reset logger to include log file
        self._set_logger()


# set up environment variable and a default logger
env = RuntimeEnvironments()


def get_logger():
    return env.logger


#
# String formatting
#
class _DeHTMLParser(HTMLParser):
    '''This parser analyzes input text, removes HTML tags such as
    <p>, <br>, <ul>, <li> etc and returns properly formatted texts.
    '''

    def __init__(self):
        HTMLParser.__init__(self)
        self.__text = []

    def handle_data(self, data):
        text = data.strip()
        if len(text) > 0:
            text = re.sub('[ \t\r\n]+', ' ', text)
            self.__text.append(text + ' ')

    def handle_starttag(self, tag, attrs):
        if tag == 'p':
            self.__text.append('\n\n\n\n')
        elif tag == 'br':
            self.__text.append('\n\n')
        elif tag == 'ul':
            self.__text.append('')
        elif tag == 'li':
            self.__text.append('\n\n  * ')

    def handle_endtag(self, tag):
        if tag == 'ul':
            self.__text.append('\n\n')
        if tag == 'li':
            self.__text.append('\n\n')

    def handle_startendtag(self, tag, attrs):
        if tag == 'br':
            self.__text.append('\n\n')

    def text(self):
        return ''.join(self.__text).strip()


def dehtml(text):
    '''Remove HTML tag in input text and format the texts
    accordingly. '''
    try:
        parser = _DeHTMLParser()
        parser.feed(text)
        parser.close()
        return parser.text()
    except Exception as e:
        env.logger.warning(f'Failed to dehtml text: {e}')
        return text


# exception classes


class Error(Exception):
    '''Base class for SoS_ScriptParser exceptions.'''

    def _get_message(self) -> str:
        '''Getter for 'message'; needed only to override deprecation in
        BaseException.'''
        return self.__message

    def _set_message(self, value: str) -> None:
        '''Setter for 'message'; needed only to override deprecation in
        BaseException.'''
        self.__message = value

    # BaseException.message has been deprecated since Python 2.6.  To prevent
    # DeprecationWarning from popping up over this pre-existing attribute, use
    # a new property that takes lookup precedence.
    message = property(_get_message, _set_message)

    def __init__(self, msg: str = '') -> None:
        self.message: str = msg
        Exception.__init__(self, msg)

    def __repr__(self) -> str:
        return self.message

    __str__ = __repr__


class StopInputGroup(Error):
    '''Abort a step and continue'''

    def __init__(self, msg, keep_output=False):
        self.keep_output = keep_output
        Error.__init__(self, msg)


class TerminateExecution(Error):
    '''Abort a step and continue'''

    def __init__(self, msg):
        Error.__init__(self, msg)


class ProcessKilled(Error):
    '''Process is killed'''

    def __init__(self, msg=''):
        Error.__init__(self, msg)


def get_traceback():
    output = StringIO()
    exc_type, exc_value, exc_traceback = sys.exc_info()
    # print "*** print_tb:"
    traceback.print_tb(exc_traceback, limit=1, file=output)
    # print "*** print_exception:"
    try:
        traceback.print_exception(
            exc_type, exc_value, exc_traceback, limit=5, file=output)
    except Exception:
        # the above function call can fail under Python 3.4 for some
        # exception but we do not really care if that happens
        pass
    result = output.getvalue()
    output.close()
    return result
    # print "*** print_exc:"
    # traceback.print_exc()
    # print "*** format_exc, first and last line:"
    # formatted_lines = traceback.format_exc().splitlines()
    # print formatted_lines[0]
    # print formatted_lines[-1]
    # print "*** format_exception:"
    # print repr(traceback.format_exception(exc_type, exc_value,
    #                                      exc_traceback))
    # print "*** extract_tb:"
    # print repr(traceback.extract_tb(exc_traceback))
    # print "*** format_tb:"
    # print repr(traceback.format_tb(exc_traceback))
    # print "*** tb_lineno:", exc_traceback.tb_lineno


def pickleable(obj, name):
    if isinstance(obj, (str, bool, int, float, complex, bytes)):
        return True
    if isinstance(obj, (types.ModuleType, WorkflowDict)):
        return False
    if callable(obj):
        return False
    try:
        pickle.dumps(obj)
        return True
    except Exception as e:
        env.logger.debug(
            f'Object {name} with value {short_repr(obj)} is not passed because it is not pickleable: {e}'
        )
        return False


class ProgressFileObj(FileIO):
    '''A wrapper of a file object that update a progress bar
    during file read.
    '''

    def __init__(self, prog, *args, **kwargs):
        FileIO.__init__(self, *args, **kwargs)
        self.prog = prog

    def read(self, n, *args):
        self.prog.update(n)
        return FileIO.read(self, n, *args)


def stable_repr(obj):
    if isinstance(obj, str):
        return repr(obj)
    if hasattr(obj, '__stable_repr__'):
        return obj.__stable_repr__()
    if isinstance(obj, Mapping):
        items = [stable_repr(k) + ':' + stable_repr(obj[k]) for k in obj.keys()]
        return '{' + ', '.join(sorted(items)) + '}'
    if isinstance(obj, Set):
        items = [stable_repr(x) for x in obj]
        return '{' + ', '.join(sorted(items)) + '}'
    if isinstance(obj, Sequence):
        return '[' + ', '.join(stable_repr(k) for k in obj) + ']'
    return repr(obj)


#
# A utility function that returns output of a command


def get_output(cmd, show_command=False, prompt='$ '):
    import subprocess
    try:
        output = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, shell=True).decode()
    except subprocess.CalledProcessError as e:
        if e.output.decode():
            env.logger.error(e.output.decode())
        raise RuntimeError(e)
    if show_command:
        return f'{prompt}{cmd}\n{output}'
    else:
        return output


#
# search a path and locate script and other files
#


def locate_script(filename, start=''):
    #
    attemp = os.path.abspath(os.path.expanduser(filename))
    if os.path.isfile(attemp):
        return ('', attemp)
    #
    token = urllib.parse.urlparse(filename)
    # if no scheme or netloc, the URL is not acceptable
    if all([
            getattr(token, qualifying_attr)
            for qualifying_attr in ('scheme', 'netloc')
    ]):
        try:
            local_filename, _ = urllib.request.urlretrieve(filename)
            with open(local_filename) as script:
                content = script.read()
            #
            return (content, filename)
        except Exception as e:
            env.logger.error(str(e))
            raise ValueError(f'Failed to open {filename}')
    #
    # a search path
    pathes = [start]
    sos_config_file = os.path.join(
        os.path.expanduser('~'), '.sos', 'config.yml')
    if os.path.isfile(sos_config_file):
        try:
            with open(sos_config_file) as config:
                cfg = yaml.safe_load(config)
        except Exception:
            raise RuntimeError(
                f'Failed to parse global sos config file {sos_config_file}, is it in JSON format?'
            )
        #
        pathes.extend(cfg.get('sos_path', []))
    #
    for path in pathes:
        if not path:
            continue
        attemp = os.path.join(
            os.path.expanduser(path), os.path.expanduser(filename))
        if os.path.isfile(attemp):
            return ('', attemp)
        # is it an URL?
        token = urllib.parse.urlparse(path)
        # if no scheme or netloc, the URL is not acceptable
        if all([
                getattr(token, qualifying_attr)
                for qualifying_attr in ('scheme', 'netloc')
        ]):
            url = path + ('' if path.endswith('/') else '/') + filename
            try:
                local_filename, _ = urllib.request.urlretrieve(url)
                with open(local_filename) as script:
                    content = script.read()
                return content, url
            except Exception:
                pass
    #
    raise ValueError(f'Failed to locate {filename}')


def valid_expr_till(text):
    pos = len(text)
    while pos > 0:
        try:
            if not text[:pos].lstrip():
                return 0
            # so we trying to find out the valid expression before : and !
            # but somehow a : r is a valid expression until we put it inside ().
            ast.parse('(' + text[:pos].lstrip() + ')')
            if pos == len(text) or text[pos] == '!' or text[pos] == ':':
                return pos
            else:
                return 0
        except Exception:
            pos -= 1
    return 0


def check_last_piece(text):
    pos = 0
    while True:
        spos = text.find('}', pos)
        if spos == -1:
            return True
        elif spos == len(text) - 1 or text[spos + 1] != '}':
            raise SyntaxError("f-string: single '}' is not allowed")
        elif spos == len(text) - 2:
            # }} as the last
            return True
        else:
            pos = spos + 2


def split_fstring(text):
    # now that we have the correct sigil
    # first, we need to replace all { as {{ and } as }}
    pieces = []
    pos = 0
    while True:
        dpos = text.find('{{', pos)
        spos = text.find('{', pos)
        if spos == -1:
            # no more {
            check_last_piece(text)
            pieces.append(text)
            break
        if spos == dpos:  # no { before {{
            pos = dpos + 2
            continue
        # now we have a valid spos
        pieces.append(text[:spos])
        # skip '{'
        rhs_pieces = text[spos + 1:].split('}')
        if len(rhs_pieces) == 1:
            raise SyntaxError(
                f'Invalid f-string {text}: missing right sigil at {text[pos:pos+20]}'
            )
        # rhs = 'whatever }' :r} something else {}
        #
        # we need to include } in expression
        for n in range(1, len(rhs_pieces) + 1):
            if valid_expr_till('}'.join(rhs_pieces[:n])) > 0:
                pieces.append('}'.join(rhs_pieces[:n]))
                text = '}'.join(rhs_pieces[n:])
                break
            # the last one, still not valid
            if n == len(rhs_pieces):
                raise SyntaxError(
                    f'Invalid f-string "{text}": invalid or empty expression')
    return pieces


def as_fstring(text):
    """expansion with python f-string, usually ok, but with the case
       of ' inside expressions, adding repr will add backslash to it
       and cause trouble.
    """
    for quote in ('"""', "'''"):
        # although script-format always ends with \n, direct use of this function might
        # have string ending with " or '
        if quote not in text and not text.endswith(quote[0]):
            return 'fr' + quote + text + quote

    # now, we need to look into the structure of f-string
    pieces = split_fstring(text)
    # if all expressions do not have single quote, we can
    # safely repr the entire string
    if not any("'" in piece for piece in pieces[1::2]):
        return 'f' + repr(text)
    #
    # unfortunately, this thing has both single and double triple quotes
    # because we cannot use backslash inside expressions in f-string, we
    # have to use format string now.
    args = []
    for idx in range(len(pieces))[1::2]:
        pos = valid_expr_till(pieces[idx])
        if pos == 0:
            raise SyntaxError(f'invalid expression in {pieces[idx]}')
        args.append(pieces[idx][:pos])
        pieces[idx] = '{' + str(idx // 2) + pieces[idx][pos:] + '}'
    return repr(''.join(pieces)) + '.format(' + ', '.join(args) + ')'


def natural_keys(text):
    '''
    alist.sort(key=natural_keys) sorts in human order
    http://nedbatchelder.com/blog/200712/human_sorting.html
    (See Toothy's implementation in the comments)
    '''
    return [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', text)]


def transcribe(text, cmd=None):
    if cmd is not None:
        text = '{}:\n{}'.format(cmd,
                                '    ' + text.replace('\n', '\n    ') + '\n')
    with fasteners.InterProcessLock(
            os.path.join(env.temp_dir, 'transcript.lck')):
        with open(os.path.join(env.exec_dir, '.sos', 'transcript.txt'),
                  'a') as trans:
            trans.write(text)


def dict_merge(dct, merge_dct):
    """ Recursive dict merge. Inspired by :meth:``dict.update()``, instead of
    updating only top-level keys, dict_merge recurses down into dicts nested
    to an arbitrary depth, updating keys. The ``merge_dct`` is merged into
    ``dct``.
    :param dct: dict onto which the merge is executed
    :param merge_dct: dct merged into dct
    :return: None
    """
    for k, v in merge_dct.items():
        if (k in dct and isinstance(dct[k], dict) and isinstance(v, dict)):
            dict_merge(dct[k], v)
        else:
            dct[k] = v


# display file size in K, M, G etc automatically. Code copied from
# http://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size
# for its compact size


def pretty_size(n,
                pow=0,
                b=1024,
                u='B',
                pre=[''] + [p + 'i' for p in 'KMGTPEZY']):
    pow, n = min(int(math.log(max(n * b**pow, 1), b)), len(pre) - 1), n * b**pow
    return "%%.%if %%s%%s" % abs(pow %
                                 (-pow - 1)) % (n / b**float(pow), pre[pow], u)


def expand_size(size):
    if isinstance(size, int):
        return size
    m = re.match(r'\s*([+-]?)([\.\d]*)\s*(\S+)\s*', size)
    if not m:
        raise ValueError(f'Invalid size specified: {size}')
    sign, num, unit = m.groups()
    sign = -1 if sign == '-' else 1
    if not unit:
        return sign * int(num)
    if not num:
        num = 1
    s = {x + 'I': 1024**(idx + 1) for idx, x in enumerate('KMGTPEZY')}
    s.update({x: 1000**(idx + 1) for idx, x in enumerate('KMGTPEZY')})
    unit = unit[:-1].upper() if unit[-1].upper().endswith('B') else unit.upper()
    if unit not in s:
        raise ValueError(f'Invalid size specified: {size}')
    return sign * int(float(num) * s[unit])


def find_symbolic_links(item):
    item = os.path.expanduser(item)
    if os.path.islink(item):
        if not os.path.exists(item):
            env.logger.warning(f'Non-existent symbolic link {item}')
        return {item: os.path.realpath(item)}
    elif os.path.isfile(item):
        return {}
    else:
        result = {}
        for x in os.listdir(item):
            result.update(find_symbolic_links(os.path.join(item, x)))
        return result


class ActivityNotifier(threading.Thread):

    def __init__(self, msg, delay=5):
        threading.Thread.__init__(self)
        self.msg = msg
        self.start_time = time.time()
        self.delay = delay
        self.event = threading.Event()
        self.start()

    def run(self):
        prog = None
        while True:
            self.event.wait(self.delay)
            if self.event.is_set():
                if prog:
                    prog.close()
                break
            if not prog:
                print(self.msg)
                prog = ProgressBar(
                    desc='', position=0, bar_format='{desc}', total=100000000)
            second_elapsed = time.time() - self.start_time
            prog.set_description('Elapsed time {}{}'.format(
                '' if second_elapsed < 86400 else
                f'{int(second_elapsed/86400)} day{"s" if second_elapsed > 172800 else ""} ',
                time.strftime('%H:%M:%S', time.gmtime(second_elapsed))))
            prog.update(1)

    def stop(self):
        self.event.set()


class DelayedAction:
    '''Call the passed function with param after a few seconds. It is most often
    used to display certain message only if an action takes a long time.

        action = delayedAction(env.logger.info, 'This might take a while', 5)
        some_action_that_might_take_a_while
        del action

    if the action finishes very quick, the message will not be displayed.
    '''

    def __init__(self, func, param, delay=5):
        self.timer = threading.Timer(delay, func, (param,))
        self.timer.start()

    def __del__(self):
        self.timer.cancel()


class ArgumentError(Error):
    """Raised when an invalid argument is passed."""

    def __init__(self, msg):
        Error.__init__(self, msg)
        self.args = (msg,)


def _parse_error(msg):
    '''This function will replace error() function in argparse module so that SoS
    can hijack errors raised from it.'''
    raise ArgumentError(msg)


def sos_get_param(key, defvalue):
    '''Parse command line arguments and set values to parameters section.
    NOTE: parmeters will not be handled if it is already defined in
    the environment. This makes the parameters variable.
    '''
    env.parameter_vars.add(key)
    # if no argument is provided
    if not env.config['workflow_args'] and not env.config['workflow_vars']:
        if isinstance(defvalue, type):
            raise ArgumentError(
                f'Argument {key} of type {defvalue.__name__} is required')
        else:
            return defvalue
    #
    if env.config['workflow_vars']:
        if key in env.config['workflow_vars']:
            return env.config['workflow_vars'][key]

    parser = argparse.ArgumentParser(allow_abbrev=False)
    # thre is a possibility that users specify --cut-off instead of --cut_off for parameter
    # cut_off. It owuld be nice to allow both.
    #
    # Argparse would produce cut_off for both definition of --cut-off and --cut_off, however
    # you can only use the matching input...
    from .targets import path, paths, sos_targets, file_target
    ret_type = None

    if isinstance(defvalue, type) or defvalue is None:
        if defvalue == bool:
            feature_parser = parser.add_mutually_exclusive_group(required=True)
            feature_parser.add_argument(
                f'--{key}', dest=key, action='store_true')
            feature_parser.add_argument(
                f'--no-{key}', dest=key, action='store_false')
            if '_' in key:
                feature_parser.add_argument(
                    f'--{key.replace("_", "-")}', dest=key, action='store_true')
                feature_parser.add_argument(
                    f'--no-{key.replace("_", "-")}',
                    dest=key,
                    action='store_false')
        else:
            if defvalue is None:
                defvalue = str
            if defvalue in (sos_targets, paths):
                ret_type = defvalue
            # if only a type is specified, it is a required document of required type
            if '_' in key:
                feature_parser = parser.add_mutually_exclusive_group(
                    required=True)
                feature_parser.add_argument(
                    f'--{key}',
                    dest=key,
                    type=str if hasattr(defvalue, '__iter__') and
                    defvalue not in (file_target, path) else defvalue,
                    help='',
                    nargs='+' if defvalue not in (str, file_target) and
                    hasattr(defvalue, '__iter__') else '?')
                feature_parser.add_argument(
                    f'--{key.replace("_", "-")}',
                    dest=key,
                    type=str if hasattr(defvalue, '__iter__') and
                    defvalue not in (file_target, path) else defvalue,
                    help='',
                    nargs='+' if defvalue not in (str, file_target) and
                    hasattr(defvalue, '__iter__') else '?')
            else:
                parser.add_argument(
                    f'--{key}',
                    dest=key,
                    type=str if hasattr(defvalue, '__iter__') and
                    defvalue not in (file_target, path) else defvalue,
                    help='',
                    required=True,
                    nargs='+' if defvalue not in (str, file_target, path) and
                    hasattr(defvalue, '__iter__') else '?')
    else:
        if isinstance(defvalue, bool):
            feature_parser = parser.add_mutually_exclusive_group(required=False)
            feature_parser.add_argument(
                f'--{key}', dest=key, action='store_true')
            feature_parser.add_argument(
                f'--no-{key}', dest=key, action='store_false')
            if '_' in key:
                feature_parser.add_argument(
                    f'--{key.replace("_", "-")}', dest=key, action='store_true')
                feature_parser.add_argument(
                    f'--no-{key.replace("_", "-")}',
                    dest=key,
                    action='store_false')
            feature_parser.set_defaults(**{key: defvalue})
        else:
            if isinstance(defvalue, (file_target, path)):
                deftype = type(defvalue)
            elif isinstance(defvalue, paths):
                deftype = path
                ret_type = type(defvalue)
            elif isinstance(defvalue, sos_targets):
                deftype = file_target
                ret_type = type(defvalue)
            elif isinstance(defvalue, str):
                deftype = str
            elif isinstance(defvalue, Sequence):
                if len(defvalue) > 0:
                    deftype = type(defvalue[0])
                else:
                    deftype = str
            else:
                deftype = type(defvalue)

            if '_' in key:
                feature_parser = parser.add_mutually_exclusive_group(
                    required=False)
                feature_parser.add_argument(
                    f'--{key}',
                    dest=key,
                    type=deftype,
                    nargs='*' if isinstance(defvalue, Sequence) and
                    not isinstance(defvalue, str) else '?',
                    default=defvalue)
                feature_parser.add_argument(
                    f'--{key.replace("_", "-")}',
                    dest=key,
                    type=deftype,
                    nargs='*' if isinstance(defvalue, Sequence) and
                    not isinstance(defvalue, str) else '?',
                    default=defvalue)
            else:
                parser.add_argument(
                    f'--{key}',
                    dest=key,
                    type=deftype,
                    nargs='*' if isinstance(defvalue, Sequence) and
                    not isinstance(defvalue, str) else '?',
                    default=defvalue)
    #
    parser.error = _parse_error
    parsed, _ = parser.parse_known_args(env.config['workflow_args'])
    return ret_type(vars(parsed)[key]) if ret_type else vars(parsed)[key]


# def is_locked(lockfile):
#    lock = fasteners.InterProcessLock(lockfile)
#    gotten = lock.acquire(blocking=False)
#    if gotten:
#        lock.release()
#        return False
#    else:
#        return True


class TimeoutInterProcessLock(fasteners.InterProcessLock):
    #
    # #871
    #
    # For some unknown reason, sos could hang after tasks fail to obtain a lock.
    # The problem should **NOT** be able to fix by simply removing the lock file
    # because the new lock file should still be marked as "locked" if the process
    # that clocks the lock is still valid. However, we have observed cases that
    # no process could be found to be locking the file, yet the lock appears to
    # be occupied, and yet removing the lock file **will not** automatically
    # unlock the waiting process, yet rerunning sos after removing the lock
    # file would work.
    #
    # This is really strange but we are **temporarily** replacing fasteners.InterProcessLock
    # with this TimeoutInterProcessLock which would try to remove the lock file after
    # timeout (default to 5) seconds, and try to obtain a lock after the removal of the
    # lock file.
    #
    def __init__(self, path, timeout=5, sleep_func=time.sleep, logger=None):
        super(TimeoutInterProcessLock, self).__init__(
            path, sleep_func=sleep_func, logger=logger)
        self.timeout = timeout

    def __enter__(self):
        start_time = time.time()
        msg = False
        while True:
            gotten = self.acquire(blocking=False)
            if gotten:
                return self
            self.sleep_func(0.01)
            if time.time() - start_time > self.timeout:
                if os.path.exists(self.path):
                    try:
                        os.remove(self.path)
                    except Exception:
                        pass
                if not msg:
                    env.logger.warning(
                        f'Failed to obtain lock {self.path} after {self.timeout} seconds, perhaps you will have to remove the lock file manually.'
                    )
                    msg = True


config_cache: dict = {}


def load_config_files(filename=None):
    # user-specified configuration file.
    if filename is None and 'config_file' in env.config:
        filename = env.config['config_file']

    if filename is not None and not os.path.isfile(
            os.path.expanduser(filename)):
        env.logger.warning(f'Ignoring missing configuration file {filename}')
        filename = None

    filemtime = None if filename is None else os.path.getmtime(
        os.path.expanduser(filename))

    from .targets import textMD5
    extra_cfg = None if 'extra_config' not in env.config else textMD5(
        str(env.config['extra_config']))

    if (filename, filemtime, extra_cfg) in config_cache:
        env.sos_dict.set('CONFIG',
                         config_cache[(filename, filemtime, extra_cfg)])
        return config_cache[(filename, filemtime, extra_cfg)]

    cfg = {}
    # site configuration file
    sos_config_file = os.path.join(
        os.path.split(__file__)[0], 'site_config.yml')
    if os.path.isfile(sos_config_file):
        try:
            with open(sos_config_file) as config:
                cfg = yaml.safe_load(config)
        except Exception as e:
            raise RuntimeError(
                f'Failed to parse global sos hosts file {sos_config_file}, is it in YAML/JSON format? ({e})'
            )

    # global site file
    sos_config_file = os.path.join(os.path.expanduser('~'), '.sos', 'hosts.yml')
    if os.path.isfile(sos_config_file):
        try:
            with open(sos_config_file) as config:
                gd = yaml.safe_load(config)
            dict_merge(cfg, gd)
        except Exception as e:
            print(get_traceback())
            env.logger.warning(
                f'Failed to parse global sos hosts file {sos_config_file}: {e}')
    # global config file
    sos_config_file = os.path.join(
        os.path.expanduser('~'), '.sos', 'config.yml')
    if os.path.isfile(sos_config_file):
        try:
            with open(sos_config_file) as config:
                gd = yaml.safe_load(config)
            dict_merge(cfg, gd)
        except Exception as e:
            env.logger.warning(
                f'Failed to parse global sos config file {sos_config_file}: {e}'
            )

    if filename is not None:
        if not os.path.isfile(os.path.expanduser(filename)):
            raise RuntimeError(f'Config file {filename} not found')
        try:
            with open(os.path.expanduser(filename)) as config:
                gd = yaml.safe_load(config)
            dict_merge(cfg, gd)
        except Exception as e:
            env.logger.warning(f'Failed to parse config file {filename}: {e}')

    if 'user_name' not in cfg:
        cfg['user_name'] = getpass.getuser().lower()

    if 'extra_config' in env.config and isinstance(env.config['extra_config'],
                                                   dict):
        cfg.update(env.config['extra_config'])

    # handle keyword "based_on", which should fill the dictionary with others.
    def process_based_on(cfg, item):
        if 'based_on' in item:
            if not isinstance(item['based_on'],
                              (str, list)) or not item['based_on']:
                raise ValueError(
                    f'A string is expected for key based_on. {item["based_on"]} obtained'
                )

            referred_keys = [item['based_on']] if isinstance(
                item['based_on'], str) else item['based_on']
            item.pop('based_on')
            for rkey in referred_keys:
                # find item...
                val = cfg
                for key in rkey.split('.'):
                    if not isinstance(val, dict):
                        raise ValueError(f'Based on key {item} not found')
                    if key not in val:
                        raise ValueError(
                            f'Based on key {key} not found in config')
                    else:
                        val = val[key]
                #
                if not isinstance(val, dict):
                    raise ValueError('Based on item must be a dictionary')
                if 'based_on' in val:
                    val = process_based_on(cfg, val)
                # ok, we have got a dictionary, let us use it to replace item
                for k, v in val.items():
                    if k not in item:
                        item[k] = v
            return item
        else:
            for k, v in item.items():
                if isinstance(v, dict):
                    # v should be processed in place
                    process_based_on(cfg, v)
            return item

    #
    for v in cfg.values():
        if isinstance(v, dict):
            process_based_on(cfg, v)

    config_cache[(filename, filemtime, extra_cfg)] = cfg
    env.sos_dict.set('CONFIG', cfg)
    return cfg


def format_duration(time_diff_secs, short=True):
    secs = int(time_diff_secs)
    rec = [(secs // (3600 * 24), 'day'), (secs % (3600 * 24) // 3600, 'hr'),
           (secs % 3600 // 60, 'min'), (secs % 60, 'sec')]
    txt = [f'{x} {y}' for x, y in rec if x > 0]
    return (txt[0] if short else ' '.join(txt)) if txt else '0s'


def format_HHMMSS(v):
    if isinstance(v, int):
        h, m, s = v // 3600, v % 3600 // 60, v % 60
    elif isinstance(v, str):
        # the time can be spacified as age.
        try:
            return format_HHMMSS(expand_time(v))
        except Exception as e:
            raise ValueError(
                f'walltime should be specified as a integer with unit s (default), h, m, d or string in the format of HH:MM:SS. "{v}" specified ({e})'
            )
    else:
        raise ValueError(
            f'walltime should be specified as a integer with unit s (default), h, m, d or string in the format of HH:MM:SS. "{v}" of type {v.__class__.__name__} specified'
        )
    return f'{h:02d}:{m:02d}:{s:02d}'


def expand_time(v, default_unit='s') -> int:
    # expand walltime from '00:00:00' format to second
    if isinstance(v, str):
        try:
            sign = {'+': 1, '-': -1}[v[1]]
            v = v[1:]
        except Exception:
            # if there is no sign, assume +
            sign = 1

        # format HHMMSS
        if ':' in v:
            try:
                h, m, s = map(int, v.split(':'))
                return sign * (h * 60 * 60 + m * 60 + s)
            except Exception as e:
                raise ValueError(
                    f'Input of option walltime should be an integer with unit s (default), h, m, d or a string in the format of HH:MM:SS. {v} specified: {e}'
                )
        #
        try:
            unit = {'s': 1, 'm': 60, 'h': 3600, 'd': 3600 * 24}[v[-1]]
            v = v[:-1]
        except Exception:
            unit = {'s': 1, 'm': 60, 'h': 3600, 'd': 3600 * 24}[default_unit]
        #
        try:
            return int(sign * unit * float(v))
        except Exception:
            raise ValueError(
                f'Unacceptable time for parameter age, expecting [+/-] num [s|m|h|d] or HH:MM:SS (e.g. +5h): {v} provided'
            )
    elif isinstance(v, int):
        return v
    else:
        raise ValueError(
            f'Input of option walltime should be an integer with unit s (default), h, m, d or a string in the format of HH:MM:SS. {v} specified.'
        )


def tail_of_file(filename, n, ansi2html=False):
    """Reads a n lines from f with an offset of offset lines. """
    avg_line_length = 74
    to_read = n

    with open(filename) as f:
        while 1:
            try:
                f.seek(-(avg_line_length * to_read), 2)
            except IOError:
                # woops.  apparently file is smaller than what we want
                # to step back, go to the beginning instead
                f.seek(0)
            pos = f.tell()
            lines = f.read().splitlines()
            if len(lines) >= to_read or pos == 0:
                if ansi2html:
                    return convertAnsi2html('\n'.join(lines[-to_read:]))
                return '\n'.join(lines[-to_read:]) + '\n'
            avg_line_length *= 1.3


def sample_lines(lines, n):
    '''Draw a sample of n lines from filename, largely evenly.'''
    if len(lines) <= n:
        return ''.join(lines)
    else:
        m = len(lines)
        return ''.join([lines[x * m // n + m // (2 * n)] for x in range(n)])


def linecount_of_file(filename):
    f = open(filename, 'rb')
    lines = 0
    buf_size = 1024 * 1024
    read_f = f.raw.read

    buf = read_f(buf_size)
    while buf:
        lines += buf.count(b'\n')
        buf = read_f(buf_size)

    return lines


def isPrimitive(obj):
    # test if object is of primitive types (string, number, sequence etc
    # http://stackoverflow.com/questions/6391694/check-if-a-variables-type-is-primitive
    return not hasattr(obj, '__dict__')


def save_var(name, var):
    if isinstance(var, (bool, int, float, complex, str, bytes)) or var is None:
        return f'{name}={repr(var)}\n'
    if isinstance(var, (type, types.ModuleType, WorkflowDict, Exception)):
        return ''
    try:
        # for more complex type, we use pickle + base64
        return f'{name}:={base64.b64encode(pickle.dumps(var))}\n'
    except Exception:
        return ''


def load_var(line):
    from .targets import remote
    assert remote
    key, value = line.split('=', 1)
    if key.endswith(':'):
        return key[:-1], pickle.loads(base64.b64decode(eval(value.strip())))
    else:
        try:
            return key, eval(value.strip())
        except Exception:
            # use SoS_eval instead of eval because vars can contain sos objects such as R_library
            from .eval import SoS_eval
            return key, SoS_eval(value.strip())


def version_info(module: str):
    # return the version of Python module
    try:
        code = ("import %s; version=str(%s.__version__)" % (module, module))
        ns_g: Dict = {}
        ns_l: Dict = {}
        exec(compile(code, "<string>", "exec"), ns_g, ns_l)
        return ns_l["version"]
    except Exception:
        import pkg_resources
        try:
            return pkg_resources.require(module)[0].version
        except Exception:
            return 'na'


def loaded_modules(namespace=None):
    if not namespace:
        return []
    res = {}
    for value in namespace.values():
        if isinstance(value, types.ModuleType):
            res[value.__name__] = version_info(value.__name__)
    return [(x, y) for x, y in res.items() if y != 'na']


def convertAnsi2html(txt):
    # 94 is blue, debug
    # 32 is darkgreen, emphasize
    # 95 is purple, warning
    # 91 is red, error
    # 36 is cray, trace
    return txt.replace('\033[94m', '<font color="">'). \
        replace('\033[32m', '<font color="DarkGreen">'). \
        replace('\033[36m', '<font color="cyan">'). \
        replace('\033[95m', '<font color="purple">'). \
        replace('\033[91m', '<font color="red">'). \
        replace('\033[0m', '</font>'). \
        replace('\n', '<br>')


# log to file for debugging purpose only


def pexpect_run(cmd, shell=False, win_width=None, stdout_socket=None):

    def send_output(output):
        if stdout_socket:
            stdout_socket.send_multipart([
                b'PRINT',
                env.config.get('slave_id', '').encode(),
                output.encode()
            ])
        else:
            sys.stdout.write(output)

    if sys.platform == 'win32':
        import pexpect
        import pexpect.popen_spawn as ps
        child = ps.PopenSpawn(cmd)
        while True:
            try:
                child.expect('\n')
                if env.verbosity > 0:
                    send_output(child.before.decode() + '\n')
            except pexpect.EOF:
                break
        return child.wait()
    else:
        import pexpect
        import subprocess
        if win_width:
            os.environ['COLUMNS'] = str(win_width)
        else:
            os.environ['COLUMNS'] = '80'
        try:
            if isinstance(cmd, str):
                if shell:
                    child = pexpect.spawn(
                        '/bin/bash', ['-c', cmd], timeout=None)
                else:
                    child = pexpect.spawn(cmd, timeout=None)
            else:
                if shell:
                    child = pexpect.spawn(
                        '/bin/bash', ['-c', subprocess.list2cmdline(cmd)],
                        timeout=None)
                else:
                    child = pexpect.spawn(
                        subprocess.list2cmdline(cmd), timeout=None)
            while True:
                try:
                    child.expect('\r\n')
                    if env.verbosity > 0:
                        send_output(child.before.decode() + '\n')
                except pexpect.EOF:
                    break
            child.wait()
            child.close()
            return child.exitstatus
        except Exception as e:
            sys.stderr.write(str(e))
            return 1


def format_par(name, par):
    from .targets import path, file_target
    try:
        name = name.replace("_", "-")
        val = eval(par)
        # specify type
        if isinstance(val, type) or val is None:
            if val == bool:
                return f'--[no-]{name} (required)'
            elif val is None:
                return f'--{name} VAL (required)'
            elif val not in (str, file_target, path) and hasattr(
                    val, '__iter__'):
                return f'--{name} VAL VAL ... (as {val.__class__.__name__}, required)'
            else:
                return f'--{name} VAL (as {val.__name__}, required)'
        else:
            if val is True or val is False:
                return f'--[no-]{name} (default to {val})'
            elif isinstance(val, Sequence) and not isinstance(val, str):
                return f'--{name} {" ".join(str(x) for x in val)} (as {val.__class__.__name__})'
            elif isinstance(val, str):
                return f'--{name} {val if val.isalnum() else repr(val)}'
            else:
                return f'--{name} {val} (as {val.__class__.__name__})'
    except Exception:
        return f'--{name} {par}'


def b64_of(filename: str):
    with open(filename, 'rb') as content:
        data = content.read()
    return base64.b64encode(data).decode('ascii')


def dot_to_gif(filename: str, warn=None):
    import glob
    import tempfile
    from graphviz import Source
    from PIL import Image, ImageDraw, ImageFont
    with open(filename) as dot, tempfile.TemporaryDirectory() as tempDirectory:
        content = dot.read()
        # find out subworkflows
        subworkflows = [
            line.split()[2].strip('"')
            for line in content.splitlines()
            if line.startswith('strict digraph')
        ]
        # find out unique subgraphs
        unique_subworkflows = list(dict.fromkeys(subworkflows))
        #
        src = Source(content)
        src.format = 'png'
        outfile = src.render(filename='sosDot', directory=tempDirectory)
        # dot command can generate more than outfiles returned by the render function
        pngFiles = glob.glob(os.path.join(tempDirectory, f'sosDot*.png'))
        if len(pngFiles) == 1:
            return b64_of(outfile)
        # create a gif files from multiple png files
        pngFiles.sort(
            key=lambda x: int(os.path.basename(x)[:-3].split('.')[1] or 0))
        # find maximum size for all graphs corresponding to their subgraphs
        maxWidth = 0
        wf_maxHeight = {}
        images = {}
        for subworkflow in unique_subworkflows:
            wf_images = {
                png: Image.open(png)
                for png, wf in zip(pngFiles, subworkflows)
                if wf == subworkflow
            }
            maxWidth = max(maxWidth,
                           max([x.size[0] for x in wf_images.values()]))
            wf_maxHeight[subworkflow] = max(
                [x.size[1] for x in wf_images.values()]) + 20
            images.update(wf_images)
        # now, we stack workflows as follows
        #    G1
        #    G2
        #    G3
        # and allow G1, G2, G3 to expand...
        maxWidth += 150
        totalHeight = sum(wf_maxHeight.values())
        lastGraph = {}
        newImages = {}
        try:
            font = ImageFont.truetype('/Library/Fonts/Arial.ttf', 8)
        except Exception:
            try:
                font = ImageFont.truetype('arial.ttf', 8)
            except Exception:
                font = None

        for subworkflow, pngFile in zip(subworkflows, pngFiles):
            image = images[pngFile]
            lastGraph[subworkflow] = image
            # we need to stitch figures together
            try:
                newImg = Image.new(
                    "RGB", (maxWidth, totalHeight), color=0xFFFFFF)
                y_loc = 0
                for wf in unique_subworkflows:
                    # if current, use the new one
                    if wf == subworkflow:
                        img = image
                    elif wf in lastGraph:
                        img = lastGraph[wf]
                    else:
                        continue
                    newImg.paste(img,
                                 ((maxWidth - img.size[0]) // 2, y_loc + 20))
                    draw = ImageDraw.Draw(newImg)
                    # font = ImageFont.truetype("sans-serif.ttf", 8)
                    # , font=font)
                    draw.text((5, y_loc + 5), wf, (0, 0, 0), font=font)
                    y_loc += wf_maxHeight[wf]
            except Exception as e:
                if warn:
                    warn(f'Failed to resize gif file: {e}')
                return b64_of(pngFiles[-1])
            newImages[pngFile] = newImg

        # create a gif file from images
        try:
            with BytesIO() as output:
                newImages[pngFiles[0]].save(
                    output,
                    save_all=True,
                    format='GIF',
                    append_images=[newImages[x] for x in pngFiles[1:]],
                    duration=400,
                    loop=0)
                return base64.b64encode(output.getvalue()).decode('ascii')
        except Exception as e:
            if warn:
                warn(f'Failed to generate gif animation: {e}')
        # if things go wrong
        return b64_of(pngFiles[-1])


def separate_options(options: str) -> List[str]:
    pieces = options.split(',')
    idx = 0
    while True:
        try:
            # test current group
            compile(
                pieces[idx].strip(),
                filename='<string>',
                mode='exec' if '=' in pieces[idx] else 'eval')
            # if it is ok, go next
            idx += 1
            if idx == len(pieces):
                break
        except Exception:
            # error happens merge the next piece
            if idx < len(pieces) - 1:
                pieces[idx] += ',' + pieces[idx + 1]
                # error happens merge the next piece
                pieces.pop(idx + 1)
            else:
                # if no next group, expand previously correct one
                if idx == 0:
                    raise ValueError('Invalid section option')
                # break myself again
                pieces = pieces[: idx] + \
                    pieces[idx].split(',') + pieces[idx + 1:]
                # go back
                idx -= 1
                pieces[idx] += '\n' + pieces[idx + 1]
                pieces.pop(idx + 1)
    return pieces


def get_localhost_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


#
# Handling -j option on cluster
#


def get_nodelist():
    """Return a list of hosts depending on the environment"""
    # If on a cluster, return some SoS acceptable format of node:nproc.
    if "SLURM_NODELIST" in os.environ:
        # Use scontrol utility to get the hosts list
        import subprocess
        hostsstr = subprocess.check_output(
            ["scontrol", "show", "hostnames",
             os.environ['SLURM_NODELIST']]).decode()
        # Split using endline
        # Split using endlinea, numer of cpus can be in the format of 4(x2)
        nprocs = os.environ["SLURM_JOB_CPUS_PER_NODE"].split('(')[0]
        args = [
            f'{x}:{nprocs}' for x in hostsstr.split(os.linesep) if x.strip()
        ]
        env.log_to_file('WORKER', f'Using "-j {args}" on a SLURM cluster.')
        return args
    elif "PBS_ENVIRONMENT" in os.environ:
        with open(os.environ["PBS_NODEFILE"], 'r') as hosts:
            hostlist = hosts.read().split()
            from collections import Counter, OrderedDict
            counts = Counter(hostlist)
            args = [
                f'{x}:{counts[x]}'
                for x in list(OrderedDict.fromkeys(hostlist))
            ]
        env.log_to_file('WORKER', f'Using "-j {args}" on a PBS cluster.')
        return args
    elif "PE_HOSTFILE" in os.environ:
        with open(os.environ["PE_HOSTFILE"], 'r') as hosts:
            args = [':'.join(host.split()) for host in hosts]
        env.log_to_file('WORKER', f'Using "-j {args}" on a SGE cluster.')
        return args
    elif "LSB_MCPU_HOSTS" in os.environ:
        # IBM LSF https://www.ibm.com/support/knowledgecenter/en/SSETD4_9.1.3/lsf_config_ref/lsf_envars_ref.html
        # LSB_MCPU_HOSTS="hostA 3 hostB 3"
        hostlist = os.environ["LSB_MCPU_HOSTS"].strip().split()
        args = [
            f'{hostlist[2*i]}:{hostlist[2*i+1]}'
            for i in range(len(hostlist) // 2)
        ]
        env.log_to_file('WORKER', f'Using "-j {args}" on a IBM LSF cluster.')
        return args


def under_cluster():
    return any(
        x in os.environ for x in ("SLURM_NODELIST", "PBS_ENVIRONMENT",
                                  "PE_HOSTFILE", "LSB_MCPU_HOSTS"))


def get_open_files_and_connections(pid):
    proc = psutil.Process(pid)
    return {'open_files': proc.open_files(), 'connections': proc.connections()}
