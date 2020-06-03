#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import shutil
import subprocess
import unittest
from argparse import Namespace

from sos.converter import extract_workflow
from sos.utils import env


class TestConvert(unittest.TestCase):

    def setUp(self):
        env.reset()
        if not os.path.isdir('temp'):
            os.mkdir('temp')
        with open('temp/script1.sos', 'w') as script:
            script.write('''
[0]
seq = range(3)
input: for_each='seq'
output: "test${_seq}.txt"
print(output)
''')
        with open('temp/script2.sos', 'w') as script:
            # with tab after run:
            script.write('''
[0]
seq = range(3)
input: for_each='seq'
output: "test${_seq}.txt"
run:			concurrent=True
    echo 'this is test script'

[10]
report('this is action report')
''')
        self.scripts = ['temp/script1.sos', 'temp/script2.sos']

    def tearDown(self):
        shutil.rmtree('temp')

    def test_script_to_html(self):
        '''Test sos show script --html'''
        for script_file in self.scripts:
            self.assertEqual(
                subprocess.call(
                    f'sos convert {script_file} {script_file}.html',
                    shell=True), 0)
            self.assertEqual(
                subprocess.call(
                    f'sos convert {script_file} {script_file}.html --linenos',
                    shell=True), 0)
            #
            self.assertEqual(
                subprocess.call(['sos', 'convert', script_file, '--to',
                                 'html']), 0)

    def test_extract_workflow(self):
        '''Test extract workflow from ipynb file'''
        content = extract_workflow('sample_workflow.ipynb')
        print(content)
        self.assertEqual(
            content, '''\
#!/usr/bin/env sos-runner
#fileformat=SOS1.0

# this comment will be included but not shown in help message
# because it is for the global
[global]
a = 1
# this comment will become the comment for parameter b
parameter: b=2
parameter: c=3
# this comment will become the comment for parameter d
parameter: d='d'

# this is a section comment, will be displayed
[default]
print(f'Hello {a}')

''')


if __name__ == '__main__':
    #suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestConvert)
    # unittest.TextTestRunner().run(suite)
    unittest.main()
