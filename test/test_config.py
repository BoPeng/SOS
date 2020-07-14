#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import getpass
import subprocess
import pytest

from sos import execute_workflow
from sos._version import __version__
from sos.utils import env, load_config_files
from sos.eval import get_config

# if the test is imported under sos/test, test interacive executor

test_cfg = '''
    cut: 0.5
    cut1:
    - 0.5
    - 2
    - 3
    cut2: a3
    cut3:
    - a
    - b
    - c
    cut4:
    A: 123
    me: '{user_name}@my'
'''


def test_command_line():
    '''Test command line arguments'''
    assert subprocess.call(
        'sos config -h',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    assert subprocess.call(
        'sos config -g --get',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    assert subprocess.call(
        'sos config',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    assert subprocess.call(
        'sos config --get',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    assert subprocess.call(
        'sos config -g --set a 5',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    assert subprocess.call(
        'sos config --get a',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    assert subprocess.call(
        'sos config -g --unset a',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0


def test_config_set(config_factory):
    '''Test interpolation of config'''
    myconfig = config_factory(test_cfg)

    assert subprocess.call(
        f'sos config --set cut 0.5 -c {myconfig}',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    load_config_files(myconfig)
    assert env.sos_dict['CONFIG']['cut'] == 0.5
    #
    assert subprocess.call(
        f'sos config --set cut1 0.5 2 3 -c {myconfig}',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    load_config_files(myconfig)
    assert env.sos_dict['CONFIG']['cut1'] == [0.5, 2, 3]
    #
    assert subprocess.call(
        f'sos config --set cut2 a3 -c {myconfig}',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    load_config_files(myconfig)
    assert env.sos_dict['CONFIG']['cut2'] == 'a3'
    #
    assert subprocess.call(
        f'sos config --set cut3 a b c -c {myconfig}',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    load_config_files(myconfig)
    assert env.sos_dict['CONFIG']['cut3'] == ['a', 'b', 'c']
    #
    assert subprocess.call(
        f'''sos config --set cut4 "{{'A': 123}}" -c {myconfig}''',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    load_config_files(myconfig)
    assert env.sos_dict['CONFIG']['cut4'] == {'A': 123}


def test_interpolate(config_factory):
    '''Test interpolation of config'''
    myconfig = config_factory(test_cfg)
    assert subprocess.call(
        f'''sos config --set me '{{user_name}}@my' -c {myconfig}''',
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        shell=True) == 0
    load_config_files(myconfig)
    assert get_config('me') == f'{getpass.getuser().lower()}@my'


def test_global_vars(config_factory):
    '''Test SoS defined variables'''
    execute_workflow("[0]", options={'mode': 'dryrun'})

    assert env.sos_dict['SOS_VERSION'] == __version__
    assert isinstance(env.sos_dict['CONFIG'], dict)

    cfg = config_factory({'my_config': 5})

    execute_workflow("[0]", options={'config_file': cfg})
    assert env.sos_dict['CONFIG']['my_config'] == 5


def test_get_config(config_factory):
    myconfig = config_factory({
        'val': 5,
        'A': {
            'B.C': '33',
            'B.C1': {
                'D': '34'
            },
            'D': '45'
        },
        'E': {
            'F': {
                'val': 6,
                'val1': 10,
                'G': '{val + val1}'
            },
            'H': '{val}'
        },
        'O': 'A{nonexisting}'
    })
    load_config_files(myconfig)
    assert get_config('A', 'D') == '45'
    assert get_config('A.D') == '45'
    assert get_config(['A', 'D']) == '45'
    assert get_config(['A', 'D']) == '45'
    assert get_config('A.B.C') == '33'
    assert get_config('A.B.C1.D') == '34'
    assert get_config('A') == {'B.C': '33', 'B.C1': {'D': '34'}, 'D': '45'}
    assert get_config('E.F') == {'val': 6, 'val1': 10, 'G': '16'}
    assert get_config('E.F', val=7) == {'val': 6, 'val1': 10, 'G': '17'}
    assert get_config('E.F', val=7, allowed_keys=['G']) == {'G': '17'}
    assert get_config(
        'E.F', val=7, val1=20) == {
            'val': 6,
            'val1': 10,
            'G': '27'
        }
    assert get_config('E.F', {
        'val': 8,
        'val1': 30
    }) == {
        'val': 6,
        'val1': 10,
        'G': '38'
    }
    assert get_config('E.H', val=7) == '7'
    with pytest.raises(ValueError):
        get_config('O')
    assert get_config('O', nonexisting=7) == 'A7'
