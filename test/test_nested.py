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
from sos.workflow_executor import Base_Executor
from sos import execute_workflow


class TestNested(unittest.TestCase):

    def setUp(self):
        env.reset()
        subprocess.call('sos remove -s', shell=True)
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

    def test_progress_bar(self):
        # progress bar with nested workflow
        script = SoS_Script('''
import time
time.sleep(0)
[sub_1]
[sub_2]
[sub_3]
[sub_4]
[a_1]
[a_2]
[a_3]
sos_run('sub')
[a_4]
[a_5]
''')
        env.verbosity = 1
        wf = script.workflow('a')
        Base_Executor(wf).run()

    def test_nested_workflow(self):
        '''Test the creation and execution of combined workfow'''
        self.touch(['a.txt', 'b.txt', 'b.begin'])
        script = SoS_Script('''
if 'executed' not in locals():
    executed = []
if 'inputs' not in locals():
    inputs = []

[a_1: shared=['executed', 'inputs']]
executed.append(step_name)
inputs.append(_input)

[a_2: shared=['executed', 'inputs']]
executed.append(step_name)
inputs.append(_input)

[a_3: shared=['executed', 'inputs']]
executed.append(step_name)
inputs.append(_input)

[a_4: shared=['executed', 'inputs']]
executed.append(step_name)
output: 'a.done'
inputs.append(_input)
run: expand=True
  touch {_output}

[b_1: shared=['executed', 'inputs']]
executed.append(step_name)
input: 'b.begin'
inputs.append(_input)

[b_2: shared=['executed', 'inputs']]
executed.append(step_name)
inputs.append(_input)

[b_3: shared=['executed', 'inputs']]
executed.append(step_name)
inputs.append(_input)

[b_4: shared=['executed', 'inputs']]
executed.append(step_name)
output: 'b.txt'
inputs.append(_input)

[c: shared=['executed', 'inputs']]
executed.append(step_name)
input: 'a.txt'
output: 'b.txt'
inputs.append(_input)
sos_run('a+b', shared=['executed', 'inputs'])
''')
        env.config['sig_mode'] = 'ignore'
        wf = script.workflow('c')
        Base_Executor(wf).run()
        # order of execution is not guaranteed
        self.assertEqual(
            sorted(env.sos_dict['executed']),
            sorted(
                ['c', 'a_1', 'a_2', 'a_3', 'a_4', 'b_1', 'b_2', 'b_3', 'b_4']))
        env.sos_dict.pop('executed', None)

    def test_looped_nested_workflow(self):
        # step will be looped
        self.touch(['a.txt', 'b.txt'])
        script = SoS_Script('''
if 'executed' not in locals():
    executed = []
if 'inputs' not in locals():
    inputs = []

[a_1:shared=['executed', 'inputs']]
executed.append(step_name)
output: _input[0] + '.a1'
inputs.append(_input)
run: expand=True
    touch {_output}

[a_2:shared=['executed', 'inputs']]
executed.append(step_name)
output: _input[0] + '.a2'
inputs.append(_input)
run: expand=True
    touch {_output}

[c:shared=['executed', 'inputs']]
executed.append(step_name)
input: 'a.txt', 'b.txt', group_by='single'
inputs.append(_input)
sos_run('a', shared=['executed', 'inputs'])
''')
        wf = script.workflow('c')
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['executed'],
                         ['c', 'a_1', 'a_2', 'a_1', 'a_2'])
        #self.assertEqual(env.sos_dict['inputs'], [['a.txt'], ['a.txt'], ['a.txt.a1'], ['b.txt'], ['b.txt'], ['b.txt.a1']])
        for file in ('a.txt.a1', 'a.txt.a1.a2', 'b.txt.a1', 'b.txt.a1.a2'):
            if file_target(file).exists():
                file_target(file).unlink()
        #

    def test_single_looped_nested_workflow(self):
        self.touch(['a.txt', 'b.txt'])
        env.sos_dict.pop('executed', None)
        # allow specifying a single step
        # step will be looped
        script = SoS_Script('''
if 'executed' not in locals():
    executed = []
[a_1:shared='executed']
executed.append(step_name)
[a_2:shared='executed']
executed.append(step_name)
[c_0:shared='executed']
executed.append(step_name)
[c_1:shared='executed']
depends: sos_variable('executed')
executed.append(step_name)
input: 'a.txt', 'b.txt', group_by='single'
sos_run('a:2', shared='executed')
''')
        wf = script.workflow('c')
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['executed'], ['c_0', 'c_1', 'a_2', 'a_2'])
        env.sos_dict.pop('executed', None)
        # allow specifying a single step
        # step will be looped
        script = SoS_Script('''
if 'executed' not in locals():
    executed = []
[a_1:shared='executed']
executed.append(step_name)
[a_2:shared='executed']
executed.append(step_name)
[c_0:shared='executed']
executed.append(step_name)
[c_1:shared='executed']
depends: sos_variable('executed')
executed.append(step_name)
input: 'a.txt', 'b.txt', group_by='single'
sos_run('a:2', shared='executed')
''')
        wf = script.workflow('c')
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['executed'], ['c_0', 'c_1', 'a_2', 'a_2'])
        #
        env.sos_dict.pop('executed', None)

    def test_recursive_nested_workflow(self):
        # recursive subworkflow not allowed
        self.touch(['a.txt', 'b.txt'])
        script = SoS_Script('''
if 'executed' not in locals():
    executed = []
[a_1:shared='executed']
executed.append(step_name)
[a_2:shared='executed']
executed.append(step_name)
[c_0:shared='executed']
executed.append(step_name)
[c_1:shared='executed']
executed.append(step_name)
input: 'a.txt', 'b.txt', group_by='single'
sos_run('a_2+c', shared='executed')
''')
        wf = script.workflow('c')
        self.assertRaises(Exception, Base_Executor(wf).run)
        #
        env.sos_dict.pop('executed', None)
        # nested subworkflow is allowed
        script = SoS_Script('''
if 'executed' not in locals():
    executed = []
[a_1:shared='executed']
executed.append(step_name)
[a_2:shared='executed']
executed.append(step_name)
[a_3:shared='executed']
executed.append(step_name)
[b_1:shared='executed']
executed.append(step_name)
[b_2:shared='executed']
executed.append(step_name)
sos_run('a:1-2', shared='executed')
[c_0:shared='executed']
executed.append(step_name)
[c_1:shared='executed']
depends: sos_variable('executed')
executed.append(step_name)
input: 'a.txt'
sos_run('a+b', shared='executed')
''')
        wf = script.workflow('c')
        Base_Executor(wf).run()
        self.assertEqual(
            env.sos_dict['executed'],
            ['c_0', 'c_1', 'a_1', 'a_2', 'a_3', 'b_1', 'b_2', 'a_1', 'a_2'])
        #
        #
        env.sos_dict.pop('executed', None)

    def test_subworkflow_with_options(self):
        # nested subworkflow with step option and others
        self.touch(['a.txt', 'b.txt'])
        script = SoS_Script('''
if 'executed' not in locals():
    executed = []
[a_1:shared='executed']
executed.append(step_name)
[a_2:shared='executed']
executed.append(step_name)
[a_3:shared='executed']
executed.append(step_name)
[b:shared='executed']
executed.append(step_name)
input: 'a.txt', 'b.txt', group_by='single'
sos_run('a:3+a:1', shared='executed')
[d:shared='executed']
executed.append(step_name)
input: 'a.txt', 'b.txt', group_by='single'
sos_run('a:2', shared='executed')
[e2_2:shared='executed']
executed.append(step_name)
input: 'a.txt', 'b.txt', group_by='single'
''')
        wf = script.workflow('b')
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['executed'],
                         ['b', 'a_3', 'a_1', 'a_3', 'a_1'])
        env.sos_dict.pop('executed', None)
        wf = script.workflow('d')
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['executed'], ['d', 'a_2', 'a_2'])
        env.sos_dict.pop('executed', None)
        wf = script.workflow('e2')
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['executed'], ['e2_2'])
        #
        # clean up
        file_target('a.done').unlink()

    def test_dynamic_nested_workflow(self):
        '''Test nested workflow controlled by command line option'''
        script = SoS_Script('''
if 'executed' not in locals():
    executed = []
parameter: wf='a'

[a_1:shared='executed']
executed.append(step_name)
[a_2:shared='executed']
executed.append(step_name)
[a_3:shared='executed']
executed.append(step_name)
[b_1:shared='executed']
executed.append(step_name)
[b_2:shared='executed']
executed.append(step_name)
[b_3:shared='executed']
executed.append(step_name)

[default:shared='executed']
executed.append(step_name)
sos_run(wf, shared='executed')
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--wf', 'b']).run()
        self.assertEqual(env.sos_dict['executed'],
                         ['default', 'b_1', 'b_2', 'b_3'])
        #
        env.sos_dict.pop('executed', None)
        Base_Executor(wf, args=['--wf', 'a']).run()
        self.assertEqual(env.sos_dict['executed'],
                         ['default', 'a_1', 'a_2', 'a_3'])

    def test_sos_run(self):
        '''Test action sos_run with keyword parameters'''
        for f in ['0.txt', '1.txt']:
            if file_target(f).exists():
                file_target(f).unlink()
        script = SoS_Script(r'''
[A]
parameter: num=5
run: expand=True
    touch {num}.txt

[batch]
for k in range(2):
    sos_run('A', num=k)
''')
        wf = script.workflow('batch')
        Base_Executor(wf).run()
        for f in ['0.txt', '1.txt']:
            self.assertTrue(file_target(f).target_exists())
            file_target(f).unlink()
        #
        # if we do not pass num, parameter would not change
        for f in ['0.txt', '1.txt']:
            if file_target(f).exists():
                file_target(f).unlink()
        script = SoS_Script(r'''
[A]
parameter: num=5
run: expand=True
    touch {num}.txt

[batch]
for num in range(2):
    sos_run('A')
''')
        wf = script.workflow('batch')
        Base_Executor(wf).run()
        for f in ['0.txt', '1.txt']:
            self.assertFalse(file_target(f).target_exists())
        self.assertTrue(file_target('5.txt').target_exists())
        file_target('5.txt').unlink()
        #
        # test parameter shared to send and return vars
        #
        script = SoS_Script(r'''
[A: shared='k']
k += 10

[batch]
for k in range(2):
    sos_run('A', shared='k')
    run(f"touch {k}.txt")
''')
        wf = script.workflow('batch')
        Base_Executor(wf).run()
        for f in ['10.txt', '11.txt']:
            self.assertTrue(file_target(f).target_exists())
            file_target(f).unlink()

    def test_da_gof_dynamic_nested_workflow(self):
        #
        # Because we are not sure which workflows would be executed
        # until run time, the DAG should not contain nested workflow
        # until runtime.
        #
        for f in [
                'B0.txt', 'B0.txt.p', 'B1.txt', 'B1.txt.p', 'B2.txt', 'B2.txt.p'
        ]:
            if file_target(f).exists():
                file_target(f).unlink()
        #
        #  A1 <- P <- B
        #  A1 <- P <- B
        #  A2
        #
        #  ALL calls A and B with parameter
        #
        script = SoS_Script('''
[A_1]
parameter: num = 2
input: f"B{num}.txt.p"

[B: provides='B{num}.txt']
run: expand=True
    touch 'B{num[0]}.txt'

[P: provides='{filename}.p']
input: filename
run: expand=True
    touch {_output}

[ALL]

for i in range(3):
    sos_run('A', num=i)


''')
        # the workflow should call step K for step C_2, but not C_3
        wf = script.workflow('ALL')
        Base_Executor(wf).run()
        for f in [
                'B0.txt', 'B0.txt.p', 'B1.txt', 'B1.txt.p', 'B2.txt', 'B2.txt.p'
        ]:
            self.assertTrue(file_target(f).target_exists())
            file_target(f).unlink()

    def test_outcome_oriented_nested_workflow(self):
        '''test nested workflow triggered by targets'''
        if os.path.isfile('test_15.txt'):
            os.remove('test_15.txt')
        execute_workflow('''
[A: provides='test_{idx}.txt']
_output.touch()

[default]
sos_run(targets='test_15.txt')
        ''')
        self.assertTrue(os.path.isfile('test_15.txt'))

    def test_passing_vars_to_nested_workflow(self):
        '''Test if variables can be passed to nested workflows'''
        script = SoS_Script(r"""
import time
import random

[nested]
parameter: nested=True
parameter: seed=1
print(f'I am nested {nested} with seed {seed}')

[0]
reps = range(5)
input: for_each='reps'
import random
nested = _reps
seed = random.randint(1, 1000)
print(f'Passing {seed} to {nested}')
sos_run('nested', nested=nested, seed=seed)

""")
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_user_defined_func(self):
        '''Test the use of user-defined functions in SoS script'''
        script = SoS_Script(r"""

def myfunc():
  return 'a'

[1: shared={'test':'_output'}]
output: myfunc()

myfunc()

""")
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['test'], ['a'])
        # User defined function should also work under nested workflows
        # This is difficult because the 'local namespace' is usually
        # not seen inside function definition. The solution now is to
        # use a single workspace.
        script = SoS_Script(r"""

def myfunc():
    # test if builtin functions (sum and range) can be used here.
    return 'a' + str(sum(range(10)))

[1: shared={'test':'_output'}]
output: [myfunc() for i in range(10)][0]

myfunc()

""")
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['test'], ['a45'])

    def test_config_file_of_nested_workflow(self):
        '''Test passing of configurationg to nested workflow'''
        script = SoS_Script('''
[test_1]
parameter: key = None
print(CONFIG[key])

[default_1]
sos_run('test:1', key = '1')
    ''')
        with open('test.conf', 'w') as conf:
            conf.write("""{'1':'hi'}""")
        wf = script.workflow()
        Base_Executor(wf, config={'config_file': 'test.conf'}).run()

    def test_error_from_subworkflow(self):
        '''Test if error from subworkflow is passed to master (#396)'''
        script = SoS_Script('''
[test_1]
R:
  set.seed(xxx)

[default]
sos_run('test')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)

    def test_fun_def(self):
        '''Test defintion of function that can be used by other steps'''
        self.touch(['aa.txt', 'ab.txt'])
        # in nested workflow?
        script = SoS_Script(r"""
def myfunc(a):
    return ['a' + x for x in a]

[mse: shared={'test':'_output'}]
input: myfunc(['a.txt', 'b.txt'])

[1]
sos_run('mse')
""")
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        #
        # Names defined in subworkflow is not returned to the master dict
        self.assertTrue('test' not in env.sos_dict)

    def test_search_path(self):
        '''Test if any action should exit in five seconds in dryrun mode'''
        sos_config_file = os.path.join(
            os.path.expanduser('~'), '.sos', 'config.yml')
        shutil.copy(sos_config_file, 'test.yml')
        #
        subprocess.call(
            'sos config --set sos_path {0}/crazy_path {0}/crazy_path/more_crazy/'
            .format(os.getcwd()),
            shell=True)
        #
        if not os.path.isdir('crazy_path'):
            os.mkdir('crazy_path')
            os.mkdir(os.path.join('crazy_path', 'more_crazy'))
        with open(os.path.join('crazy_path', 'crazy_master.sos'), 'w') as crazy:
            crazy.write('''
[0]
sos_run('cc', source='crazy_slave.sos')

''')
        with open(
                os.path.join('crazy_path', 'more_crazy', 'crazy_slave.sos'),
                'w') as crazy:
            crazy.write('''
[cc_0]
print('hay, I am crazy')
''')

        script = SoS_Script(filename='crazy_master.sos')
        script.workflow()
        #
        shutil.rmtree('crazy_path')
        os.remove(sos_config_file)
        shutil.copy('test.yml', sos_config_file)

    def test_nested_workdir(self):
        '''Test nested runtime option for work directory'''
        if os.path.isdir('tmp'):
            shutil.rmtree('tmp')
        script = SoS_Script('''
[step]
task: workdir='tmp'
run:
    touch 'a.txt'

[default]
sos_run('step', workdir='tmp')
''')
        wf = script.workflow()
        # this should be ok.
        Base_Executor(wf, config={'default_queue': 'localhost'}).run()
        os.path.isfile('tmp/tmp/a.txt')
        shutil.rmtree('tmp')

    def test_failure_of_nested_workflow(self):
        '''Test failure of nested workflow #838'''
        if os.path.isfile('a.txt'):
            os.remove('a.txt')
        script = SoS_Script('''
[something]
input: 'a.txt'

[default]
sos_run('something')
''')
        wf = script.workflow()
        # this should be ok.
        self.assertRaises(Exception, Base_Executor(wf).run)

    def test_nested_from_another_file(self):
        '''Test nested runtime option for work directory'''
        if os.path.isdir('a.txt'):
            os.remove('a.txt')
        with open('another.sos', 'w') as another:
            another.write('''
[whatever]
run:
    touch 'a.txt'

''')
        script = SoS_Script('''
[default]
sos_run('whatever', source='another.sos')
''')
        wf = script.workflow()
        # this should be ok.
        Base_Executor(wf).run()
        self.assertTrue(
            os.path.isfile('a.txt'),
            'a.txt should have been created by nested workflow from another file'
        )

    def test_concurrent_sub_workflow(self):
        '''Test concurrent subworkflow sos_run '''
        script = SoS_Script('''
[A]
parameter: idx=0
import time
time.sleep(5)

[default]
input: for_each=dict(i=range(6))
sos_run('A', idx=i)
''')
        import time
        st = time.time()
        wf = script.workflow()
        # this should be ok.
        Base_Executor(wf, config={'worker_procs': ['8']}).run()
        self.assertTrue(time.time() - st < 30)

    def test_sos_multi_workflow(self):
        '''Test multiple workflows in sos_run '''
        script = SoS_Script('''
[B]
parameter: idx=2
import time
time.sleep(idx)

[A]
import time
time.sleep(idx)

[default]
input: for_each=dict(i=range(4))
sos_run(['A', 'B'], idx=i)
''')
        import time
        st = time.time()
        wf = script.workflow()
        # this should be ok.
        Base_Executor(wf, config={'worker_procs': ['8']}).run()
        self.assertTrue(time.time() - st < 20)

    def test_pass_of_args(self):
        '''Test passing of arguments through sos_run #1164'''
        script = SoS_Script('''
parameter: b=2
[A]
parameter: c=1
print(c)

[default]
sos_run('A', a=2)
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)
        #
        script = SoS_Script('''
parameter: b=2
[A]
parameter: c=1
print(c)

[default]
sos_run('A', b=2)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        #
        script = SoS_Script('''
parameter: b=2
[A]
parameter: c=1
print(c)

[default]
sos_run('A', c=2)
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_nested_dynamic_depends(self):
        '''Test the execution of nested workflow with dynamic depends'''
        script = SoS_Script('''
[A_1]
parameter: num = 20
input: f"B{num}.txt"
depends: _input + '.p'

[B: provides='B{num}.txt']
run: expand=True
    touch {_output}

[P: provides='{filename}.p']
input: filename
run: expand=True
    touch {_output}


[default]
sos_run('A', num=0)
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_neste_with_both_workflow_and_targets(self):
        '''Test nested workflow with both workflow and targets'''
        for file in ('A_out.txt', 'B_out.txt'):
            if os.path.isfile(file):
                os.remove(file)
        execute_workflow(r'''
[A_20]
output: 'A_out.txt'
_output.touch()

[B]
output: 'B_out.txt'
_output.touch()

[default]
sos_run('A', targets='B_out.txt')
''')
        self.assertTrue(os.path.isfile('A_out.txt'))
        self.assertTrue(os.path.isfile('B_out.txt'))


if __name__ == '__main__':
    #suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestParser)
    # unittest.TextTestRunner(, suite).run()
    unittest.main()
