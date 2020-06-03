#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import shutil
import subprocess
import unittest

from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env
# if the test is imported under sos/test, test interacive executor
from sos.workflow_executor import Base_Executor


class TestOutcome(unittest.TestCase):

    def setUp(self):
        env.reset()
        subprocess.call('sos remove -s', shell=True)
        # self.resetDir('~/.sos')
        self.temp_files = []

    def tearDown(self):
        for f in self.temp_files:
            if file_target(f).exists():
                file_target(f).unlink()

    def touch(self, files):
        '''create temporary files'''
        if isinstance(files, str):
            files = [files]
        #
        for f in files:
            with open(f, 'w') as tmp:
                tmp.write('test')
        #
        self.temp_files.extend(files)

    def resetDir(self, dirname):
        if os.path.isdir(os.path.expanduser(dirname)):
            shutil.rmtree(os.path.expanduser(dirname))
        os.mkdir(os.path.expanduser(dirname))

    def removeIfExists(self, targets):
        if isinstance(targets, str):
            targets = [targets]
        for target in targets:
            if os.path.isfile(target):
                os.remove(target)

    def test_plain_target(self):
        '''Test sos run -t filename'''
        self.removeIfExists('t_a.txt')
        script = SoS_Script('''
[A]
output: 't_a.txt'
_output.touch()
''')
        wf = script.workflow(use_default=False)
        Base_Executor(wf).run(targets=['t_a.txt'])
        self.assertTrue(os.path.isfile('t_a.txt'))

    def test_target_in_named(self):
        '''Test sos run -t filename'''
        self.removeIfExists('t_na.txt')
        script = SoS_Script('''
[A]
output: res='t_na.txt'
_output.touch()
''')
        wf = script.workflow(use_default=False)
        Base_Executor(wf).run(targets=['t_na.txt'])
        self.assertTrue(os.path.isfile('t_na.txt'))

    def test_named_output_as_target(self):
        '''Test sos run -t named_output'''
        self.removeIfExists('t_no.txt')
        script = SoS_Script('''
[A]
output: res='t_no.txt'
_output.touch()
''')
        wf = script.workflow(use_default=False)
        Base_Executor(wf).run(targets=['res'])
        self.assertTrue(os.path.isfile('t_no.txt'))

    def test_provides_target(self):
        '''Test sos run -t filename with exact match'''
        self.removeIfExists('t_pa.txt')
        script = SoS_Script('''
[A: provides="t_pa.txt"]
_output.touch()
''')
        wf = script.workflow(use_default=False)
        Base_Executor(wf).run(targets=['t_pa.txt'])
        self.assertTrue(os.path.isfile('t_pa.txt'))
        #
        self.removeIfExists('t_pa.txt')
        script = SoS_Script('''
[A: provides="t_pa.txt"]
output: 't_pa.txt'
_output.touch()
''')
        wf = script.workflow(use_default=False)
        Base_Executor(wf).run(targets=['t_pa.txt'])
        self.assertTrue(os.path.isfile('t_pa.txt'))
        #
        self.removeIfExists('t_pa.txt')
        script = SoS_Script('''
[A: provides="t_pa.txt"]
output: pa='t_pa.txt'
_output.touch()
''')
        wf = script.workflow(use_default=False)
        Base_Executor(wf).run(targets=['t_pa.txt'])
        self.assertTrue(os.path.isfile('t_pa.txt'))
        #
        self.removeIfExists('t_pa.txt')
        script = SoS_Script('''
[A: provides="t_pa.txt"]
output: pa='t_pa_none.txt'
_output.touch()
''')
        wf = script.workflow(use_default=False)
        self.assertRaises(
            Exception, Base_Executor(wf).run, targets=['t_pa.txt'])

    def test_provides_pattern(self):
        '''Test sos run -t filename with pattern matching'''
        self.removeIfExists('t_ma.txt')
        script = SoS_Script('''
[A: provides="{filename}.txt"]
_output.touch()
''')
        wf = script.workflow(use_default=False)
        Base_Executor(wf).run(targets=['t_ma.txt'])
        self.assertTrue(os.path.isfile('t_ma.txt'))


if __name__ == '__main__':
    unittest.main()
