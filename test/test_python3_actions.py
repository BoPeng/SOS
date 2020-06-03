#!/usr/bin/env python
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import unittest

from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env
from sos.workflow_executor import Base_Executor


class TestActions(unittest.TestCase):

    def setUp(self):
        env.reset()
        self.temp_files = []

    def tearDown(self):
        for f in self.temp_files:
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

    def test_python(self):
        '''Test python command. This might fail if python3 is the
        default interpreter'''
        script = SoS_Script(r'''
[0]
python: expand='${ }'
a = {'1': 2}
print(a)
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_python3(self):
        script = SoS_Script(r'''
[0]
python3: expand='${ }'
a = {'1', '2'}
print(a)
''')
        wf = script.workflow()
        Base_Executor(wf).run()


if __name__ == '__main__':
    unittest.main()
