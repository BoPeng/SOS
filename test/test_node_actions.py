#!/usr/bin/env python
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

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

    @unittest.skipIf(not shutil.which('node'), 'node not installed')
    def test_node(self):
        '''Test action node'''
        script = SoS_Script(r'''
[0]
node:
var args = process.argv.slice(2);
console.log('Hello ' + args.join(' ') + '!');
''')
        wf = script.workflow()
        Base_Executor(wf).run()


if __name__ == '__main__':
    unittest.main()
