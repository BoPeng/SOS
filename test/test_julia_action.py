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

    @unittest.skipIf(not shutil.which('julia'), "julia not available")
    def test_julia(self):
        '''Test action Julia'''
        if os.path.isfile('julia_example.txt'):
            os.remove('julia_example.txt')
        script = SoS_Script(r'''
[0]
julia:
    open("julia_example.txt", "w") do f
        write(f, "A, B, C, D\n")
     end
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertTrue(os.path.isfile('julia_example.txt'))


if __name__ == '__main__':
    unittest.main()
