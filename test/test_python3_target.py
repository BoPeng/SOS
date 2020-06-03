#!/usr/bin/env python
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import unittest

from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env
from sos.workflow_executor import Base_Executor


class TestPython3Target(unittest.TestCase):

    def setUp(self):
        env.reset()
        self.temp_files = []

    def tearDown(self):
        for f in self.temp_files:
            if file_target(f).exists():
                file_target(f).unlink()

    def test_py_module(self):
        '''Test target Py_Module'''
        if file_target('report.md').exists():
            file_target('report.md').unlink()
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate', autoinstall=True)
from tabulate import tabulate
table = [["Sun",696000,1989100000],["Earth",6371,5973.6],
    ["Moon",1737,73.5],["Mars",3390,641.85]]
report: output='report.md', expand=True
    {tabulate(table)}
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertTrue(file_target('report.md').target_exists('target'))
        with open('report.md') as rep:
            self.assertEqual(
                rep.read().strip(), '''
-----  ------  -------------
Sun    696000     1.9891e+09
Earth    6371  5973.6
Moon     1737    73.5
Mars     3390   641.85
-----  ------  -------------
'''.strip())

    def test_py_module_with_version(self):
        '''Test target Py_Module'''
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate', version='2.0', autoinstall=True)
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)
        #
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate>=2.0')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)
        #
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate==20.0')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)
        #
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate<2.0')
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_upgrade_py_module(self):
        '''Test upgrade py module #1246'''
        # first install tabulate == 0.7.5
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate==0.7.5', autoinstall=True)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # test should pass
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate'), Py_Module('tabulate==0.7.5')
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # test for newer version should fail
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate==0.8.3')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)
        # auto install should work
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate==0.8.3', autoinstall=True)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # test for old version should fail
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate'), Py_Module('tabulate==0.8.3')
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # test for old version should fail
        script = SoS_Script(r'''
[10]
depends: Py_Module('tabulate==0.7.5')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)


if __name__ == '__main__':
    unittest.main()
