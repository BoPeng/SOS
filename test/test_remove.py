#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import shutil
import subprocess
import unittest
from pathlib import Path


class TestRemove(unittest.TestCase):

    def setUp(self):
        if os.path.isdir('temp'):
            shutil.rmtree('temp')
        os.mkdir('temp')
        os.chdir('temp')
        with open('test.sos', 'w') as script:
            script.write('''
[0]
output:  't_f1'
run:
    touch t_f1

[1]
output:  't_d1/t_f2'
run:
    touch t_d1/t_f2
    touch t_d1/ut_f4

[2]
output:  't_d2/t_d3/t_f3'
run:
    touch t_d2/t_d3/t_f3

''')
        subprocess.call('sos run test -s force', shell=True)
        # create some other files and directory
        for d in ('ut_d1', 'ut_d2', 'ut_d2/ut_d3'):
            os.mkdir(d)
        for f in ('ut_f1', 'ut_d1/ut_f2', 'ut_d2/ut_d3/ut_f3'):
            with open(f, 'w') as tf:
                tf.write(f)

    def assertExists(self, fdlist):
        for fd in fdlist:
            self.assertTrue(os.path.exists(fd), '{} does not exist'.format(fd))

    def assertNonExists(self, fdlist):
        for fd in fdlist:
            self.assertFalse(os.path.exists(fd), '{} still exists'.format(fd))

    def test_setup(self):
        self.assertExists([
            'ut_d1', 'ut_d2', 'ut_d2/ut_d3', 'ut_f1', 'ut_d1/ut_f2',
            'ut_d2/ut_d3/ut_f3'
        ])
        self.assertExists(
            ['t_f1', 't_d1/t_f2', 't_d2/t_d3/t_f3', 't_d2/t_d3', 't_d2'])
        # this is the tricky part, directory containing untracked file should remain
        self.assertExists(['t_d1', 't_d1/ut_f4'])

    def test_remove_all_tracked(self):
        '''test list files'''
        subprocess.call('sos remove . -t -y', shell=True)
        self.assertExists([
            'ut_d1', 'ut_d2', 'ut_d2/ut_d3', 'ut_f1', 'ut_d1/ut_f2',
            'ut_d2/ut_d3/ut_f3'
        ])
        self.assertNonExists(['t_d1/t_f2', 't_d2/t_d3/t_f3'])
        # this is the tricky part, directory containing untracked file should remain
        self.assertExists(['t_d1', 't_f1', 't_d1/ut_f4'])

    def test_remove_specific_tracked(self):
        # note the t_f1, which is under current directory and has to be remove specifically.
        subprocess.call('sos remove t_f1 ut_f1 t_d2 ut_d2 -t -y', shell=True)
        self.assertExists([
            'ut_d1', 'ut_d2', 'ut_d2/ut_d3', 'ut_f1', 'ut_d1/ut_f2',
            'ut_d2/ut_d3/ut_f3', 't_d1/t_f2', 't_d1', 't_d1/ut_f4'
        ])
        self.assertNonExists(['t_f1', 't_d2/t_d3/t_f3'])

    def test_remove_all_untracked(self):
        '''test remove all untracked files'''
        subprocess.call('sos remove . -u -y', shell=True)
        self.assertNonExists(['ut_d1/ut_f2', 't_d1/ut_f4', 'ut_d2/ut_d3/ut_f3'])
        self.assertExists([
            't_d1/t_f2', 't_d2/t_d3/t_f3', 't_d2/t_d3', 't_d2', 't_d1', 't_f1'
        ])
        # this is the tricky part, files under the current directory are not removed
        self.assertExists(['ut_f1'])

    def test_remove_specific_untracked(self):
        # note the t_f1, which is under current directory and has to be remove specifically.
        subprocess.call(
            'sos remove t_f1 ut_f1 ut_d1/ut_f2 t_d1 -u -y', shell=True)
        self.assertNonExists(['ut_f1', 'ut_d1/ut_f2', 't_d1/ut_f4'])
        self.assertExists([
            't_d1/t_f2', 't_d2/t_d3/t_f3', 't_d2/t_d3', 't_d2', 't_d1', 't_f1'
        ])
        self.assertExists(
            ['ut_d1', 'ut_d2', 'ut_d2/ut_d3', 'ut_d2/ut_d3/ut_f3'])

    def test_remove_by_age(self):
        '''test remove by age'''
        subprocess.call('sos remove --age=+1h -y', shell=True)
        # nothing is removed
        self.assertExists([
            'ut_f1', 'ut_d1/ut_f2', 't_d1/ut_f4', 't_d1/t_f2', 't_d2/t_d3/t_f3',
            't_d2/t_d3', 't_d2', 't_d1', 't_f1', 'ut_d1', 'ut_d2',
            'ut_d2/ut_d3', 'ut_d2/ut_d3/ut_f3'
        ])
        #
        subprocess.call('sos remove -t --age=-1h -y', shell=True)
        self.assertNonExists(['t_d1/t_f2', 't_d2/t_d3/t_f3'])
        self.assertExists([
            'ut_f1', 'ut_d1/ut_f2', 't_d1/ut_f4', 't_f1', 't_d2/t_d3', 't_d2',
            't_d1', 'ut_d1', 'ut_d2', 'ut_d2/ut_d3', 'ut_d2/ut_d3/ut_f3'
        ])
        #
        subprocess.call('sos remove -u --age=-1h -y', shell=True)
        self.assertExists([
            'ut_f1', 't_f1', 't_d2/t_d3', 't_d2', 't_d1', 'ut_d1', 'ut_d2',
            'ut_d2/ut_d3'
        ])

    def test_remove_by_size(self):
        '''test remove by size'''
        subprocess.call('sos remove --size=+10M -y', shell=True)
        # nothing is removed
        self.assertExists([
            'ut_f1', 'ut_d1/ut_f2', 't_d1/ut_f4', 't_d1/t_f2', 't_d2/t_d3/t_f3',
            't_d2/t_d3', 't_d2', 't_d1', 't_f1', 'ut_d1', 'ut_d2',
            'ut_d2/ut_d3', 'ut_d2/ut_d3/ut_f3'
        ])
        #
        subprocess.call('sos remove -t --size=-1M -y', shell=True)
        self.assertNonExists(['t_d1/t_f2', 't_d2/t_d3/t_f3'])
        self.assertExists([
            'ut_f1', 'ut_d1/ut_f2', 't_d1/ut_f4', 't_f1', 't_d2/t_d3', 't_d2',
            't_d1', 'ut_d1', 'ut_d2', 'ut_d2/ut_d3', 'ut_d2/ut_d3/ut_f3'
        ])
        #
        subprocess.call('sos remove -u --size=-1M -y', shell=True)
        self.assertExists([
            'ut_f1', 't_f1', 't_d2/t_d3', 't_d2', 't_d1', 'ut_d1', 'ut_d2',
            'ut_d2/ut_d3'
        ])

    def test_remove_all(self):
        '''Test remove all specified files'''
        subprocess.call('sos remove ut_d1 t_d1 ut_d2/ut_d3 -y', shell=True)
        self.assertExists(['t_d2/t_d3/t_f3', 't_d2/t_d3', 't_d2', 't_f1'])

    def test_remove_placeholders(self):
        '''Test remaining placeholder files'''
        # let us create a fake placeholder file
        os.remove('t_f1')
        os.remove('t_d1/t_f2')
        subprocess.call('sos dryrun test.sos', shell=True)
        self.assertFalse(os.path.isfile('t_f1'))
        self.assertFalse(os.path.isfile('t_d1/t_f2'))
        Path('t_f1').touch()
        Path('t_d1/t_f2').touch()
        #
        subprocess.call('sos remove -p', shell=True)
        self.assertFalse(os.path.isfile('t_f1'))
        self.assertFalse(os.path.isfile('t_d1/t_f2'))

    def tearDown(self):
        os.chdir('..')
        shutil.rmtree('temp')


if __name__ == '__main__':
    unittest.main()
