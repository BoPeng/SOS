#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import sys
import subprocess
import unittest

from sos.hosts import Host
from sos.targets import file_target
from sos.utils import env
from sos.parser import SoS_Script
from sos.workflow_executor import Base_Executor

has_docker = True
try:
    subprocess.check_output('docker ps | grep test_sos', shell=True).decode()
except subprocess.CalledProcessError:
    subprocess.call('sh build_test_docker.sh', shell=True)
    try:
        subprocess.check_output(
            'docker ps | grep test_sos', shell=True).decode()
    except subprocess.CalledProcessError:
        print('Failed to set up a docker machine with sos')
        has_docker = False

# if sys.platform == 'win32':
#    with open('~/docker.yml', 'r') as d:
#        cfg = d.read()
#    with open('~/docker.yml', 'w') as d:
#        d.write(cfg.replace('/home/', 'c:\\Users\\'))


class TestRemote(unittest.TestCase):

    def setUp(self):
        env.reset()
        # self.resetDir('~/.sos')
        self.temp_files = []
        Host.reset()
        # remove .status file left by failed workflows.
        subprocess.call('sos purge', shell=True)

    def tearDown(self):
        for f in self.temp_files:
            file_target(f).unlink()

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def test_remote_execute(self):
        if os.path.isfile('result_remote.txt'):
            os.remove('result_remote.txt')
        if os.path.isfile('local.txt'):
            os.remove('local.txt')
        with open('local.txt', 'w') as w:
            w.write('something')
        self.assertEqual(
            subprocess.call(
                'sos remote push docker --files local.txt -c ~/docker.yml',
                shell=True), 0)
        with open('test_remote.sos', 'w') as tr:
            tr.write('''
[10]
input: 'local.txt'
output: 'result_remote.txt'
task:

run:
  cp local.txt result_remote.txt
  echo 'adf' >> 'result_remote.txt'

''')
        self.assertEqual(
            subprocess.call(
                'sos run test_remote.sos -c ~/docker.yml -r docker -s force -q localhost',
                shell=True), 0)
        self.assertFalse(file_target('result_remote.txt').target_exists())
        #self.assertEqual(subprocess.call('sos preview result_remote.txt -c ~/docker.yml -r docker', shell=True), 0)
        #self.assertNotEqual(subprocess.call('sos preview result_remote.txt', shell=True), 0)
        self.assertEqual(
            subprocess.call(
                'sos remote pull docker --files result_remote.txt -c ~/docker.yml',
                shell=True), 0)
        self.assertTrue(file_target('result_remote.txt').target_exists())
        #self.assertEqual(subprocess.call('sos preview result_remote.txt', shell=True), 0)
        with open('result_remote.txt') as w:
            content = w.read()
            self.assertTrue('something' in content, 'Got {}'.format(content))
            self.assertTrue('adf' in content, 'Got {}'.format(content))
        # test sos remote run
        self.assertEqual(
            subprocess.call(
                'sos remote run docker -c  ~/docker.yml --cmd cp result_remote.txt result_remote1.txt ',
                shell=True), 0)
        self.assertEqual(
            subprocess.call(
                'sos remote pull docker --files result_remote1.txt -c ~/docker.yml',
                shell=True), 0)
        self.assertTrue(file_target('result_remote1.txt').target_exists())

#     @unittest.skipIf(sys.platform == 'win32' or not has_docker,
#                      'No symbloc link problem under win32 or no docker')
#     def testToHostRename(self):
#         '''Test to_host with dictionary'''
#         script = SoS_Script(r'''
# [1]
# sh:
#    echo "1" > 1.txt
#
# [10]
# task: to_host={'1.txt': '2.txt'}, from_host={'3.txt': '2.txt'}
# with open('2.txt', 'a') as t:
#   t.write('2\n')
# ''')
#         wf = script.workflow()
#         Base_Executor(
#             wf,
#             config={
#                 'config_file': '~/docker.yml',
#                 'default_queue': 'docker',
#                 'sig_mode': 'force',
#             }).run()
#         self.assertTrue(os.path.isfile('3.txt'))
#         with open('3.txt') as txt:
#             content = txt.read()
#             self.assertEqual('1\n2\n', content, 'Got {}'.format(content))

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def test_from_host_option(self):
        '''Test from_remote option'''
        if os.path.isfile('llp'):
            os.remove('llp')
        script = SoS_Script('''
[10]
task: from_host='llp'
with open('llp', 'w') as llp:
    llp.write("LLP")
''')
        wf = script.workflow()
        Base_Executor(
            wf,
            config={
                'config_file': '~/docker.yml',
                'wait_for_task': True,
                'default_queue': 'docker',
                'sig_mode': 'force',
            }).run()
        self.assertTrue(os.path.isfile('llp'))

#     @unittest.skipIf(not has_docker, "Docker container not usable")
#     def test_from_host_option_dict(self):
#         # dict form
#         if os.path.isfile('llp'):
#             os.remove('llp')

#         script = SoS_Script('''
# [10]
# task: from_host={'llp': 'll'}
# with open('llp', 'w') as llp:
#     llp.write("LLP")
# ''')
#         wf = script.workflow()
#         Base_Executor(
#             wf,
#             config={
#                 'config_file': '~/docker.yml',
#                 'default_queue': 'docker',
#             }).run()
#         self.assertTrue(os.path.isfile('llp'))

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def test_local_from_host_option(self):
        '''Test from_remote option'''
        if os.path.isfile('llp'):
            os.remove('llp')
        script = SoS_Script('''
[10]
task: from_host='llp'
sh:
    echo "LLP" > llp
''')
        wf = script.workflow()
        Base_Executor(
            wf,
            config={
                'config_file': '~/docker.yml',
                # do not wait for jobs
                'wait_for_task': True,
                'sig_mode': 'force',
                'default_queue': 'localhost',
            }).run()
        self.assertTrue(os.path.isfile('llp'))
        os.remove('llp')
        # dict form is deprecated


#         script = SoS_Script('''
# [10]
# task: from_host={'llp': 'll'}
# sh:
#     echo "LLP" > ll
# ''')
#         wf = script.workflow()
#         Base_Executor(
#             wf,
#             config={
#                 'config_file': '~/docker.yml',
#                 # do not wait for jobs
#                 'wait_for_task': True,
#                 'sig_mode': 'force',
#                 'default_queue': 'localhost',
#             }).run()
#         self.assertTrue(os.path.isfile('llp'))
#         os.remove('llp')

    def test_worker_procs(self):
        # test -j option
        script = SoS_Script('''
[1]
input: for_each=dict(i=range(10))

bash: expand=True
    echo {i}
''')
        wf = script.workflow()
        Base_Executor(
            wf,
            config={
                'sig_mode': 'force',
                'worker_proces': ['1', 'localhost:2']
            }).run()

    def test_worker_procs_with_task(self):
        # test -j option
        script = SoS_Script('''
[1]
input: for_each=dict(i=range(10))

task: trunk_size = 0

bash: expand=True
    echo {i}
''')
        wf = script.workflow()
        Base_Executor(
            wf,
            config={
                'sig_mode': 'force',
                'default_queue': 'localhost',
                'worker_proces': ['1', 'localhost:2'],
            }).run()

if __name__ == '__main__':
    unittest.main()
