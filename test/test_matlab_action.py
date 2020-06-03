#!/usr/bin/env python
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import shutil
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

    @unittest.skipIf(not shutil.which('matlab'), 'Matlab not installed')
    def test_matlab(self):
        '''Test action matlab'''
        if os.path.isfile('/tmp/matlab_example.txt'):
            os.remove('/tmp/matlab_example.txt')
        script = SoS_Script(r'''
[0]
matlab:
    f = fopen("/tmp/matlab_example.txt", "w");
    fprintf(f, "A, B, C, D\n");
    fclose(f);
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertTrue(os.path.isfile('/tmp/matlab_example.txt'))


if __name__ == '__main__':
    unittest.main()
