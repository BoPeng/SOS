#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import shutil
import subprocess
import sys
import time
import unittest

from sos.hosts import Host
from sos.parser import SoS_Script
from sos.targets import file_target, sos_targets
from sos.utils import env
from sos.workflow_executor import Base_Executor
from sos import execute_workflow


class TestSignature(unittest.TestCase):

    def setUp(self):
        env.reset()
        subprocess.call('sos remove -s', shell=True)
        # self.resetDir('~/.sos')
        self.temp_files = []
        self.resetDir('temp')
        Host.reset()

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

    def resetDir(self, dirname):
        if os.path.isdir(os.path.expanduser(dirname)):
            shutil.rmtree(os.path.expanduser(dirname))
        os.mkdir(os.path.expanduser(dirname))

    def test_signature(self):
        self._testSignature(
            r"""
[*_0]
output: 'temp/a.txt', 'temp/b.txt'
task:
run('''echo "a.txt" > temp/a.txt ''')
run('''echo "b.txt" > temp/b.txt ''')

[1: shared={'oa':'step_output'}]
dest = ['temp/c.txt', 'temp/d.txt']
input: group_by='single', paired_with='dest'
output: _dest

run(f" cp {_input} {_dest[0]} ")
""", 2)

    def test_signature1(self):
        self._testSignature(
            r"""
[*_0]
output: 'temp/a.txt', 'temp/b.txt'

task:
run('''echo "a.txt" > temp/a.txt ''')
run('''echo "b.txt" > temp/b.txt ''')

[1: shared={'oa':'step_output'}]
dest = ['temp/c.txt', 'temp/d.txt']
input: group_by='single', paired_with='dest'
output: _dest

run(f" cp {_input} {_dest[0]} ")
""", 2)
        # script format

    def test_signature2(self):
        self._testSignature(
            r"""
[*_0]
output: 'temp/a.txt', 'temp/b.txt'

run:
echo "a.txt" > temp/a.txt

run:

echo "b.txt" > temp/b.txt

[1: shared={'oa':'step_output'}]
dest = ['temp/c.txt', 'temp/d.txt']
input: group_by='single', paired_with='dest'
output: _dest

task:
run: expand=True
echo cp {_input} {_dest[0]}
cp {_input} {_dest[0]}
""", 2)

    def test_signature_with_shared_variable(self):
        '''Test restoration of signature from variables.'''
        if file_target('a.txt').exists():
            file_target('a.txt').unlink()
        # shared
        script = SoS_Script(r"""
[0: shared='a']
output: 'a.txt'
run:
   touch a.txt

a= 5

[1]
print(a)

""")
        # alias should also be recovered.
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 2)
        # rerun
        env.sos_dict.pop('a')
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        file_target('a.txt').unlink()

    def test_signature_without_output(self):
        # signature without output file
        self._testSignature(
            r"""
[*_0]
output: []

run:
[ -d temp ] || mkdir temp
echo "a.txt" > temp/a.txt

run:

echo "b.txt" > temp/b.txt

[1: shared={'oa':'step_output'}]
dest = ['temp/c.txt', 'temp/d.txt']
input: 'temp/a.txt', 'temp/b.txt', group_by='single', paired_with='dest'
output: _dest

run: expand=True
cp {_input} {_dest[0]}
""", 2)
        # reset env mode
        env.config['sig_mode'] = 'default'
        shutil.rmtree('temp')

    def _testSignature(self, text, steps):
        '''Test recognizing the format of SoS script'''
        script = SoS_Script(text)
        for f in ['temp/a.txt', 'temp/b.txt']:
            if file_target(f).exists():
                file_target(f).unlink()
        #
        # only the first step
        wf = script.workflow(':0')
        env.config['sig_mode'] = 'force'
        res = Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        self.assertTrue(os.path.isfile('temp/a.txt'))
        self.assertTrue(os.path.isfile('temp/b.txt'))
        self.assertTrue(res['__completed__']['__step_completed__'], steps)
        with open('temp/a.txt') as ta:
            self.assertTrue(ta.read(), 'a.txt')
        with open('temp/b.txt') as tb:
            self.assertTrue(tb.read(), 'b.txt')
        env.config['sig_mode'] = 'assert'
        res = Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        # all of them
        wf = script.workflow()
        env.config['sig_mode'] = 'default'
        # generate files (default step 0 and 1)
        Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        # now, rerun in build mode
        env.config['sig_mode'] = 'build'
        res = Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        #
        self.assertTrue(os.path.isfile('temp/c.txt'))
        self.assertTrue(os.path.isfile('temp/d.txt'))
        with open('temp/c.txt') as tc:
            self.assertTrue(tc.read(), 'a.txt')
        with open('temp/d.txt') as td:
            self.assertTrue(td.read(), 'b.txt')
        self.assertEqual(env.sos_dict['oa'],
                         sos_targets('temp/c.txt', 'temp/d.txt'))
        #
        # now in assert mode, the signature should be there
        env.config['sig_mode'] = 'assert'
        res = Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)

        #
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)

        #
        # change script a little bit
        script = SoS_Script('# comment\n' + text)
        wf = script.workflow()
        env.config['sig_mode'] = 'assert'
        res = Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)

        # add some other variable?
        #script = SoS_Script('comment = 1\n' + text)
        #wf = script.workflow()
        #env.config['sig_mode'] = 'assert'
        #self.assertRaises(Exception, Base_Executor(wf).run)

    def test_reexecution(self):
        '''Test -f option of sos run'''
        script = SoS_Script('''

[0]
output: 'a.txt'
run(f"touch {_output}")
''')
        wf = script.workflow()
        try:
            # remove existing output if exists
            file_target('a.txt').unlink()
        except Exception:
            pass
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        # now, rerun should be much faster
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        # rerun takes less than 1 second
        #
        # force rerun mode
        env.config['sig_mode'] = 'ignore'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        # regularly take more than 5 seconds to execute
        try:
            # remove existing output if exists
            os.remove('a.txt')
        except Exception:
            pass

    @unittest.skipIf(sys.platform == 'win32',
                     'Windows executable cannot execute bash loop.')
    def test_signature_after_removal_of_files(self):
        '''test action shrink'''
        if os.path.isfile('largefile.txt'):
            os.remove('largefile.txt')
        script = SoS_Script(r'''
[10]

# generate a file
output: 'largefile.txt'

run: expand='${ }'
    for x in {1..1000}
    do
        echo $x >> ${_output}
    done

''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        # rerun, because this is the final target, it has to be
        # re-generated
        os.remove('largefile.txt')
        res = Base_Executor(wf).run()
        self.assertTrue(os.path.isfile('largefile.txt'))
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        #
        # we discard the signature, the step would still be
        # skipped because file signature will be calculated
        # during verification
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        #
        # now if we touch the file, it needs to be regenerated
        with open('largefile.txt', 'a') as lf:
            lf.write('something')
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        file_target('largefile.txt').unlink()

    @unittest.skipIf(sys.platform == 'win32',
                     'Windows executable cannot be created with chmod.')
    def test_removal_of_intermediate_files(self):
        # if we zap the file, it
        for f in ['midfile.txt', 'finalfile.txt', 'midfile.txt.zapped']:
            if os.path.isfile(f):
                os.remove(f)
        script = SoS_Script(r'''
[10]

# generate a file
output: 'midfile.txt'

run: expand='${ }'
    rm -f ${_output}
    for x in {1..1000}
    do
        echo $x >> ${_output}
    done

[20]
output: 'finalfile.txt'
run: expand=True
    cp {_input} {_output}
    echo "MORE" >> {_output}
''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 2)
        #
        # remove middle file, rerun
        os.remove('midfile.txt')
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        self.assertTrue(os.path.isfile('midfile.txt'))
        #
        # now if we touch the mid file, it needs to be regenerated
        with open('midfile.txt', 'a') as lf:
            lf.write('something')
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        #
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        #
        # if we zap the mid file, it does not need to be rerun
        subprocess.call('sos remove midfile.txt --zap -y', shell=True)
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)

    def test_signature_with_parameter(self):
        '''Test signature'''
        if file_target('myfile.txt').exists():
            file_target('myfile.txt').unlink()
        #
        script = SoS_Script(r'''
parameter: gvar = 10

[10]
# generate a file
output: 'myfile.txt'
# additional comment
run: expand=True
    echo {gvar} > {_output:q}

''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        with open('myfile.txt') as tmp:
            self.assertEqual(tmp.read().strip(), '10')
        #
        # now if we change parameter, the step should be rerun
        wf = script.workflow()
        res = Base_Executor(wf, args=['--gvar', '20']).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        with open('myfile.txt') as tmp:
            self.assertEqual(tmp.read().strip(), '20')
        #
        # do it again, signature should be effective
        wf = script.workflow()
        res = Base_Executor(wf, args=['--gvar', '20']).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        with open('myfile.txt') as tmp:
            self.assertEqual(tmp.read().strip(), '20')

        #
        script = SoS_Script(r'''
parameter: gvar = 10

[10]
# generate a file
output: 'myfile.txt'
# additional comment
run: expand=True
    echo {gvar} > {_output:q}
''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        with open('myfile.txt') as tmp:
            self.assertEqual(tmp.read().strip(), '10')
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        #
        # now if we change parameter, the step should be rerun
        wf = script.workflow()
        res = Base_Executor(wf, args=['--gvar', '20']).run()
        with open('myfile.txt') as tmp:
            self.assertEqual(tmp.read().strip(), '20')
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        #
        # do it again, signature should be effective
        wf = script.workflow()
        res = Base_Executor(wf, args=['--gvar', '20']).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        with open('myfile.txt') as tmp:
            self.assertEqual(tmp.read().strip(), '20')
        file_target('myfile.txt').unlink()

    def test_loop_wise_signature(self):
        '''Test partial signature'''
        for i in range(10, 12):
            if file_target(f'myfile_{i}.txt').exists():
                file_target(f'myfile_{i}.txt').unlink()
        #
        script = SoS_Script(r'''
parameter: gvar = 10

[10]
tt = [gvar]
input: for_each='tt'
output: f"myfile_{_tt}.txt"
run: expand=True
    echo "DO {_tt}"
    echo {_tt} > {_output:q}
''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        ts = os.path.getmtime('myfile_10.txt')
        #
        # now we modify the script
        script = SoS_Script(r'''
parameter: gvar = 10

[10]
tt = [gvar, gvar + 1]
input: for_each='tt'
output: f"myfile_{_tt}.txt"
run: expand=True
    echo "DO {_tt}"
    echo {_tt} > {_output:q}
''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0.5)
        # this file is not regenerated
        self.assertEqual(ts, os.path.getmtime('myfile_10.txt'))
        ts1 = os.path.getmtime('myfile_11.txt')
        #
        # run it again, neither needs to be rerun
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        self.assertEqual(ts, os.path.getmtime('myfile_10.txt'))
        self.assertEqual(ts1, os.path.getmtime('myfile_11.txt'))
        #
        # change again, the second one is already there.
        script = SoS_Script(r'''
parameter: gvar = 10

[10]
tt = [gvar + 1]
input: for_each='tt'
output: f"myfile_{_tt}.txt"
run: expand=True
    echo "DO {_tt}"
    echo {_tt} > {_output:q}
''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        self.assertEqual(ts1, os.path.getmtime('myfile_11.txt'))
        #
        for t in range(10, 12):
            with open('myfile_{}.txt'.format(t)) as tmp:
                self.assertEqual(tmp.read().strip(), str(t))
            file_target('myfile_{}.txt'.format(t)).unlink()

    def test_output_from_signature(self):
        'Test restoration of output from signature' ''
        self.touch(['1.txt', '2.txt'])
        for f in ('1.out', '2.out', '1.2.out', '2.3.out'):
            if file_target(f).exists():
                file_target(f).unlink()
        script = SoS_Script('''
parameter: K = [2,3]

[work_1]
input: "1.txt", "2.txt", group_by = 'single', pattern = '{name}.{ext}'
output: expand_pattern('{_name}.out')
run: expand=True
  touch {_output}

[work_2]

input: group_by = 'single', pattern = '{name}.{ext}', paired_with = ['K']
output: expand_pattern('{_name}.{_K}.out')
run: expand=True
  touch {_output}
    ''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 2)
        # for the second run, output should be correctly constructed
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)
        for file in ['1.out', '2.out', '1.2.out', '2.3.out']:
            file_target(file).unlink()

    def test_signature_with_vars(self):
        '''Test revaluation with variable change'''
        self.touch(('a1.out', 'a2.out'))
        for f in ('b1.out', 'b2.out'):
            if file_target(f).exists():
                file_target(f).unlink()
        script = SoS_Script('''
parameter: DB = {'input': ['a1.out'], 'output': ['b1.out']}
parameter: input_file = DB['input']
parameter: output_file =  DB['output']

[2]
input: input_file, group_by = 1
output: output_file[_index]
run: expand=True
  touch {_output}
  ''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)
        ts = os.path.getmtime('b1.out')
        #
        script = SoS_Script('''
parameter: DB = {'input': ['a1.out', 'a2.out'], 'output': ['b1.out', 'b2.out']}
parameter: input_file = DB['input']
parameter: output_file =  DB['output']

[2]
input: input_file, group_by = 1
output: output_file[_index]
run: expand=True
  touch {_output}
  ''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0.5)
        self.assertEqual(ts, os.path.getmtime('b1.out'))

    def test_action_signature(self):
        '''Test action signature'''
        with open('test_action.txt', 'w') as ta:
            ta.write('#something\n')
        script = SoS_Script(r'''
[1]
input: 'test_action.txt'
run: output='lc.txt', expand=True, tracked='test_action.txt'
    sleep 5
    wc -l {_input[0]} > lc.txt
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # the second time, should skip
        st = time.time()
        Base_Executor(wf).run()
        # should skip
        self.assertLess(time.time() - st, 5)
        #
        script = SoS_Script(r'''
[1]
input: 'test_action.txt'
print('step is changed')
run: output='lc.txt', expand=True, tracked='test_action.txt'
    sleep 5
    wc -l {_input[0]} > lc.txt
''')
        wf = script.workflow()
        st = time.time()
        Base_Executor(wf).run()
        self.assertLess(time.time() - st, 5)

        # force
        env.config['sig_mode'] = 'build'
        Base_Executor(wf).run()

    def test_signature_with_without_task(self):
        '''Test the inclusion of task would not trigger rerun'''
        script = SoS_Script(r'''[1]
output: 'aa'

sh:
  echo aa > aa
''')
        if file_target('aa').exists():
            file_target('aa').unlink()
        wf = script.workflow()
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 1)

        script = SoS_Script(r'''[1]
output: 'aa'

task:



sh:
  echo aa > aa
''')
        wf = script.workflow()
        res = Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        self.assertEqual(res['__completed__']['__step_completed__'], 0)

    def test_signature_with_dynamic_output(self):
        '''Test return of output from dynamic output'''
        for i in range(5):
            if os.path.exists(f'rep_{i}'):
                shutil.rmtree(f'rep_{i}')
        script = SoS_Script(r'''
[1: shared={'step1': 'step_output'}]
input: for_each={'i': range(5)}, concurrent=True
output: dynamic(f'rep_{i}/*.res')

import random, os
os.makedirs(f'rep_{i}', exist_ok=True)
path(f'rep_{i}/{random.randint(0, 10000)}.res').touch()
''')
        wf = script.workflow()
        res = Base_Executor(wf).run()
        files = env.sos_dict['step1']
        self.assertEqual(len(files), 5)
        self.assertEqual(res['__completed__']['__substep_completed__'], 5)
        # rerun
        res = Base_Executor(wf).run()
        files_again = env.sos_dict['step1']
        self.assertEqual(files, files_again)
        self.assertEqual(res['__completed__']['__substep_completed__'], 0)

    def test_ignore_signature(self):
        '''Test ignore signature mode #1028 '''
        script = SoS_Script(r'''
input: for_each={'i': range(3)}, concurrent=True
output: f'out_{i}.txt'
sh: expand=True
  touch {_output}
''')
        env.config['sig_mode'] = 'ignore'
        wf = script.workflow()
        Base_Executor(wf).run()
        env.config['sig_mode'] = 'default'

    def test_rebuid_signature(self):
        '''Test rebuilding signature'''
        if os.path.isfile('a.txt'):
            os.remove('a.txt')
        script = SoS_Script(r'''
[A_1]
output: 'a.txt'
_output.touch()
''')
        wf = script.workflow()
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 1)
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 1)
        env.config['sig_mode'] = 'build'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 1)
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 1)
        # if a.txt is changed, rebuild will rerun
        with open('a.txt', 'a') as atxt:
            atxt.write('aaa')
        env.config['sig_mode'] = 'build'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 1)
        # rerun?
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 1)
        #
        env.config['sig_mode'] = 'force'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 1)
        #
        env.config['sig_mode'] = 'ignore'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 1)

    def test_rebuid_signature_with_substeps(self):
        '''Test rebuilding signature'''
        for i in range(4):
            if os.path.isfile(f'a_{i}.txt'):
                os.remove(f'a_{i}.txt')
        script = SoS_Script(r'''
[A_1]
input: for_each=dict(i=range(4))
output: f'a_{i}.txt'
_output.touch()
''')
        wf = script.workflow()
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 4)
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 4)
        env.config['sig_mode'] = 'build'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 4)
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 4)
        # if a.txt is changed, rebuild will rerun
        for i in range(4):
            with open(f'a_{i}.txt', 'a') as atxt:
                atxt.write('aaa')
        env.config['sig_mode'] = 'build'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 4)
        # rerun?
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 4)
        #
        env.config['sig_mode'] = 'force'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 4)
        #
        env.config['sig_mode'] = 'ignore'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 4)

    def test_rebuid_signature_with_tasks(self):
        '''Test rebuilding signature'''
        for i in range(4):
            if os.path.isfile(f'a_{i}.txt'):
                os.remove(f'a_{i}.txt')
        script = SoS_Script(r'''
[A_1]
input: for_each=dict(i=range(2))
output: f'a_{i}.txt'
task:
_output.touch()
''')
        wf = script.workflow()
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 2)
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 2)
        env.config['sig_mode'] = 'build'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 2)
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 2)
        # if a.txt is changed, rebuild will rerun
        for i in range(2):
            with open(f'a_{i}.txt', 'a') as atxt:
                atxt.write('aaa')
        env.config['sig_mode'] = 'build'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 2)
        # rerun?
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 2)
        #
        env.config['sig_mode'] = 'force'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 2)
        #
        env.config['sig_mode'] = 'ignore'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 2)

    def test_signature_with_dependency_tracing_and_vars(self):
        '''Test signature with parameters with option -T #1200'''
        if os.path.isfile('out.txt'):
            os.remove('out.txt')
        script = SoS_Script(r'''
[analyze]
parameter: par=5
output: 'out.txt'
print(par)
_output.touch()

[default]
input: 'out.txt'
''')
        wf = script.workflow()
        res = Base_Executor(
            wf, config={
                'trace_existing': True
            }, args=['--par', '10']).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 2)
        # change parameter, the step should not be skipped
        res = Base_Executor(
            wf, config={
                'trace_existing': True
            }, args=['--par', '20']).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 2)

    def test_skip_mode(self):
        '''Test skipping mode of signature'''
        for i in range(4):
            with open(f'a_{i}.txt', 'w') as a:
                a.write(f'a_{i}.txt')
            if os.path.isfile(f'a_{i}.bak'):
                os.remove(f'a_{i}.bak')
        #
        script = SoS_Script(r'''
[A_1]
input: [f'a_{i}.txt' for i in range(4)], group_by=1
output: _input.with_suffix('.bak')

with open(_input) as ifile, open(_output, 'w') as ofile:
    ofile.write(ifile.read())
''')
        wf = script.workflow()
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 4)
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 4)
        #
        env.config['sig_mode'] = 'skip'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 4)
        #
        # if result file is changed, skip will still skip
        for i in range(4):
            with open(f'a_{i}.bak', 'a') as bak:
                bak.write('extra')
        #
        env.config['sig_mode'] = 'skip'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_skipped__'], 4)
        # now if we change to default mode, will not skip
        env.config['sig_mode'] = 'default'
        res = Base_Executor(wf).run()
        self.assertEqual(res['__completed__']['__substep_completed__'], 4)

    def test_signature_with_output_vars(self):
        '''Test persistence of _output with vars #1355'''
        if os.path.isfile('test_sig_with_vars.txt'):
            os.remove('test_sig_with_vars.txt')
        script = '''
[10]
output: f'test_sig_with_vars.txt'
_output.touch()
_output.set(seed=1)

[20]
print(_input.seed)
        '''
        execute_workflow(script)
        # second time sshould be fine (using signature
        execute_workflow(script)
        #
        # worker...
        for i in range(2):
            if os.path.isfile(f'test_sig_with_vars_{i}.txt'):
                os.remove(f'test_sig_with_vars_{i}.txt')
        script = '''
[10]
input: for_each=dict(i=range(2))
output: f'test_sig_with_vars_{i}.txt'
_output.touch()
_output.set(seed=1)

[20]
print(_input.seed)
        '''
        execute_workflow(script)
        # second time sshould be fine (using signature
        execute_workflow(script)
        # task
        # worker...
        for i in range(2):
            if os.path.isfile(f'test_sig_with_vars_{i}.txt'):
                os.remove(f'test_sig_with_vars_{i}.txt')
        script = '''
[10]
input: for_each=dict(i=range(2))
output: f'test_sig_with_vars_{i}.txt'
task: queue='localhost'
_output.touch()
_output.set(seed=1)

[20]
print(_input.seed)
        '''
        execute_workflow(script)
        # second time sshould be fine (using signature
        execute_workflow(script)


def test_signature_with_local_parameter():
    '''test for #1372'''
    script = '''
[default]
input:
output: f'constant-file-name.txt'
parameter: n_mx = int
bash: expand=True

  echo {n_mx} > constant-file-name.txt
'''
    execute_workflow(script, args=['--n-mx', '60'])

    assert os.path.isfile('constant-file-name.txt')
    assert open('constant-file-name.txt').read().strip() == '60'

    execute_workflow(script, args=['--n-mx', '80'])
    assert open('constant-file-name.txt').read().strip() == '80'