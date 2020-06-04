#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import subprocess
import unittest
from io import StringIO

from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env
# if the test is imported under sos/test, test interacive executor
from sos.workflow_executor import Base_Executor
from sos import execute_workflow


def assertDAG(dag, content):
    '''Compare the content of dag in a file, to one of more DAG (strings).'''
    if isinstance(dag, str):
        with open(dag) as d:
            # only get the first DAG
            dot = 'strict' + d.read().split('strict')[1]
    else:
        out = StringIO()
        dag.save(out)
        dot = out.getvalue()

    def sorted_dot(dot):
        return sorted([
            x.strip()
            for x in dot.split('\n')
            if x.strip() and not 'digraph' in x
        ])

    if isinstance(content, str):
        assert sorted_dot(dot) == sorted_dot(content)
    else:
        assert sorted_dot(dot) in [sorted_dot(x) for x in content]


class TestDAG(unittest.TestCase):

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

    def assertDAG(self, dag, content):
        if isinstance(dag, str):
            with open(dag) as d:
                # only get the first DAG
                dot = 'strict' + d.read().split('strict')[1]
        else:
            out = StringIO()
            dag.save(out)
            dot = out.getvalue()
        self.assertEqual(
            sorted([
                x.strip()
                for x in dot.split('\n')
                if x.strip() and not 'digraph' in x
            ]),
            sorted([
                x.strip()
                for x in content.split('\n')
                if x.strip() and not 'digraph' in x
            ]))

    def test_simple_dag(self):
        '''Test DAG with simple dependency'''
        for filename in ('a.txt', 'a1.txt'):
            with open(filename, 'w') as tmp:
                tmp.write('hey')
        # basica case
        # 1 -> 2 -> 3 -> 4
        script = SoS_Script('''
[A_1]

[A_2]

[A_3]

[A_4]
        ''')
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''strict digraph "" {
A_2;
A_4;
A_1;
A_3;
A_2 -> A_3;
A_1 -> A_2;
A_3 -> A_4;
}
''')
        # basica case
        # 1 -> 2 -> 3 -> 4
        script = SoS_Script('''
[A_1]

[A_2]

[A_3]
input: 'a.txt'

[A_4]

        ''')
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''strict digraph "" {
A_2;
A_4;
A_1;
A_3;
A_1 -> A_2;
A_3 -> A_4;
}
''')

        #
        # 1 -> 2 -> 3 -> 4
        #
        script = SoS_Script('''
[A_1]
input: 'a.txt'
output: 'b.txt'

[A_2]
input: 'b.txt'
output: 'c.txt'

[A_3]
input: 'c.txt'
output: 'd.txt'

[A_4]
input: 'd.txt'
output: 'e.txt'

        ''')
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''strict digraph "" {
A_2;
A_4;
A_1;
A_3;
A_2 -> A_3;
A_1 -> A_2;
A_3 -> A_4;
}
''')
        #
        # 1 -> 2
        # 3 -> 4 (3 does not have any input)
        #
        script = SoS_Script('''
[B_1]
input: 'a.txt'
output: 'b.txt'

[B_2]
input: 'b.txt'
output: 'c.txt'

[B_3]
input: None
output: 'd.txt'

[B_4]
input: 'd.txt'
output: 'e.txt'

        ''')
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''strict digraph "" {
B_2;
B_4;
B_1;
B_3;
B_1 -> B_2;
B_3 -> B_4;
}
''')
        #
        # 1 -> 2
        # 3 -> 4 (3 depends on something else)
        #
        script = SoS_Script('''
[B_1]
input: 'a.txt'
output: 'b.txt'

[B_2]
input: 'b.txt'
output: 'c.txt'

[B_3]
input: 'a1.txt'
output: 'd.txt'

[B_4]
input: 'd.txt'
output: 'e.txt'

        ''')

        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''strict digraph "" {
B_1;
B_4;
B_2;
B_3;
B_1 -> B_2;
B_3 -> B_4;
}
''')
        #
        # (1) -> 2
        # (1) -> 3 -> 4
        #
        # 2 and 3 depends on the output of 1
        script = SoS_Script('''
[C_1]
input: 'a.txt'
output: 'b.txt'

[C_2]
input: 'b.txt'
output: 'c.txt'

[C_3]
input:  'b.txt'
output: 'd.txt'

[C_4]
depends: 'd.txt'
output: 'e.txt'

        ''')
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''
strict digraph "" {
C_1;
C_4;
C_2;
C_3;
C_1 -> C_2;
C_1 -> C_3;
C_3 -> C_4;
}
''')
        for filename in ('a.txt', 'a1.txt'):
            os.remove(filename)

    def test_undetermined(self):
        '''Test DAG with undetermined input.'''
        #
        for filename in ('a.txt', 'd.txt'):
            with open(filename, 'w') as tmp:
                tmp.write('hey')
        # input of step 3 is undertermined so
        # it depends on all its previous steps.
        script = SoS_Script('''
[C_1]
input: 'a.txt'
output: 'b.txt'

[C_2]
input: 'b.txt'
output: 'c.txt'

[C_3]
input:  dynamic('*.txt')
output: 'd.txt'

[C_4]
depends: 'd.txt'
output: 'e.txt'

        ''')
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        dag.show_nodes()
        # dag.save('a.dot')
        self.assertDAG(
            dag, '''
strict digraph "" {
C_1;
C_4;
C_2;
C_3;
C_1 -> C_2;
C_2 -> C_3;
C_3 -> C_4;
}
''')
        #
        # output of step
        #
        script = SoS_Script('''
[C_1]
input: 'a.txt'
output: 'b.txt'

[C_2]
input: 'b.txt'
output: 'c.txt'

[C_3]
input:  dynamic('*.txt')

[C_4]
depends: 'd.txt'
output: 'e.txt'

        ''')
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''
strict digraph "" {
C_1;
C_4;
C_2;
C_3;
C_1 -> C_2;
C_2 -> C_3;
C_3 -> C_4;
}
''')
        for filename in ('a.txt', 'd.txt'):
            os.remove(filename)

    def test_auxiliary_steps(self):
        script = SoS_Script('''
[K: provides='{name}.txt']
output: f"{name}.txt"

run: expand=True
    touch '{name}.txt'

[C_2]
input: 'b.txt'
output: 'c.txt'

run:
    touch c.txt

[C_3]
input: 'a.txt'

        ''')
        # a.txt exists and b.txt does not exist
        with open('a.txt', 'w') as atfile:
            atfile.write('garbage')
        if os.path.isfile('b.txt'):
            os.remove('b.txt')
        # the workflow should call step K for step C_2, but not C_3
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        #
        # Ticket 363:
        #
        # we have two possibilities here, one is to ignore a.txt,
        # and one is to regenerate a.txt because it is not generated
        # by sos (without signature)        #
        #
        dag.show_nodes()
        self.assertDAG(
            dag, '''
strict digraph "" {
"K (b.txt)";
C_3;
C_2;
"K (b.txt)" -> C_2;
}
''')

    def test_cycle(self):
        '''Test cycle detection of DAG'''
        #
        #  A.txt --> B.txt
        #
        #  B.txt --> C.txt
        #
        #  C.txt --> A.txt
        #
        script = SoS_Script('''
[A_1]
input: 'A.txt'
output: 'B.txt'

[A_2]
output: 'C.txt'

[A_3]
output: 'A.txt'
        ''')
        # the workflow should call step K for step C_2, but not C_3
        wf = script.workflow()
        self.assertRaises(RuntimeError, Base_Executor(wf).initialize_dag)

    def test_long_chain(self):
        '''Test long make file style dependencies.'''
        #
        for f in [
                'A1.txt', 'A2.txt', 'C2.txt', 'B2.txt', 'B1.txt', 'B3.txt',
                'C1.txt', 'C3.txt', 'C4.txt'
        ]:
            if file_target(f).exists():
                file_target(f).unlink()
        #
        #  A1 <- B1 <- B2 <- B3
        #   |
        #   |
        #  \/
        #  A2 <- B2 <- C1 <- C2 <- C4
        #                    C3
        #
        script = SoS_Script('''
[A_1]
input: 'B1.txt'
output: 'A1.txt'
run:
    touch A1.txt

[A_2]
depends:  'B2.txt'
output: 'A2.txt'
run:
    touch A2.txt

[B1: provides='B1.txt']
depends: 'B2.txt'
run:
    touch B1.txt

[B2: provides='B2.txt']
depends: 'B3.txt', 'C1.txt'
run:
    touch B2.txt

[B3: provides='B3.txt']
run:
    touch B3.txt

[C1: provides='C1.txt']
depends: 'C2.txt', 'C3.txt'
run:
    touch C1.txt

[C2: provides='C2.txt']
depends: 'C4.txt'
run:
    touch C2.txt

[C3: provides='C3.txt']
depends: 'C4.txt'
run:
    touch C3.txt

[C4: provides='C4.txt']
run:
    touch C4.txt

''')
        # the workflow should call step K for step C_2, but not C_3
        wf = script.workflow()
        #env.verbosity = 4
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''
strict digraph "" {
"C4 (C4.txt)";
"B1 (B1.txt)";
"C1 (C1.txt)";
"C2 (C2.txt)";
"C3 (C3.txt)";
A_1;
"B2 (B2.txt)";
"B3 (B3.txt)";
A_2;
"C4 (C4.txt)" -> "C2 (C2.txt)";
"C4 (C4.txt)" -> "C3 (C3.txt)";
"B1 (B1.txt)" -> A_1;
"C1 (C1.txt)" -> "B2 (B2.txt)";
"C2 (C2.txt)" -> "C1 (C1.txt)";
"C3 (C3.txt)" -> "C1 (C1.txt)";
A_1 -> A_2;
"B2 (B2.txt)" -> "B1 (B1.txt)";
"B2 (B2.txt)" -> A_2;
"B3 (B3.txt)" -> "B2 (B2.txt)";
}
''')
        Base_Executor(wf).run()
        for f in [
                'A1.txt', 'A2.txt', 'C2.txt', 'B2.txt', 'B1.txt', 'B3.txt',
                'C1.txt', 'C3.txt', 'C4.txt'
        ]:
            t = file_target(f)
            self.assertTrue(t.target_exists(), f + ' should exist')
            t.unlink()

    def test_target(self):
        '''Test executing only part of a workflow.'''
        #
        for f in [
                'A1.txt', 'A2.txt', 'C2.txt', 'B2.txt', 'B1.txt', 'B3.txt',
                'C1.txt', 'C3.txt', 'C4.txt'
        ]:
            if file_target(f).exists():
                file_target(f).unlink()
        #
        #  A1 <- B1 <- B2 <- B3
        #   |
        #   |
        #  \/
        #  A2 <- B2 <- C1 <- C2 <- C4
        #                    C3
        #
        script = SoS_Script('''
[A1]
input: 'B1.txt'
output: 'A1.txt'
run:
    touch A1.txt

[A2]
depends:  'B2.txt'
run:
    touch A2.txt

[B1: provides='B1.txt']
depends: 'B2.txt'
run:
    touch B1.txt

[B2: provides='B2.txt']
depends: 'B3.txt', 'C1.txt'
run:
    touch B2.txt

[B3: provides='B3.txt']
run:
    touch B3.txt

[C1: provides='C1.txt']
depends: 'C2.txt', 'C3.txt'
run:
    touch C1.txt

[C2: provides='C2.txt']
depends: 'C4.txt'
run:
    touch C2.txt

[C3: provides='C3.txt']
depends: 'C4.txt'
run:
    touch C3.txt

[C4: provides='C4.txt']
run:
    touch C4.txt

        ''')
        # the workflow should call step K for step C_2, but not C_3
        wf = script.workflow(use_default=False)
        #
        # test 1, we only need to generate target 'B1.txt'
        dag = Base_Executor(wf).initialize_dag(targets=['B1.txt'])
        # note that A2 is no longer mentioned
        self.assertDAG(
            dag, '''
strict digraph "" {
"B3 (B3.txt)";
"C4 (C4.txt)";
"C2 (C2.txt)";
"C1 (C1.txt)";
"B1 (B1.txt)";
"B2 (B2.txt)";
"C3 (C3.txt)";
"B3 (B3.txt)" -> "B2 (B2.txt)";
"C4 (C4.txt)" -> "C3 (C3.txt)";
"C4 (C4.txt)" -> "C2 (C2.txt)";
"C2 (C2.txt)" -> "C1 (C1.txt)";
"C1 (C1.txt)" -> "B2 (B2.txt)";
"B2 (B2.txt)" -> "B1 (B1.txt)";
"C3 (C3.txt)" -> "C1 (C1.txt)";
}
''')
        Base_Executor(wf).run(targets=['B1.txt'])
        for f in ['A1.txt', 'A2.txt']:
            self.assertFalse(file_target(f).target_exists())
        for f in [
                'C2.txt', 'B2.txt', 'B1.txt', 'B3.txt', 'C1.txt', 'C3.txt',
                'C4.txt'
        ]:
            t = file_target(f)
            self.assertTrue(t.target_exists())
            t.unlink()
        #
        # test 2, we would like to generate two files
        dag = Base_Executor(wf).initialize_dag(targets=['B2.txt', 'C2.txt'])
        # note that A2 is no longer mentioned
        self.assertDAG(
            dag, '''
strict digraph "" {
"C4 (C4.txt)";
"B2 (B2.txt)";
"C3 (C3.txt)";
"B3 (B3.txt)";
"C2 (C2.txt)";
"C1 (C1.txt)";
"C4 (C4.txt)" -> "C2 (C2.txt)";
"C4 (C4.txt)" -> "C3 (C3.txt)";
"C3 (C3.txt)" -> "C1 (C1.txt)";
"B3 (B3.txt)" -> "B2 (B2.txt)";
"C2 (C2.txt)" -> "C1 (C1.txt)";
"C1 (C1.txt)" -> "B2 (B2.txt)";
}
''')
        Base_Executor(wf).run(targets=['B2.txt', 'C2.txt'])
        for f in ['A1.txt', 'B1.txt', 'A2.txt']:
            self.assertFalse(file_target(f).target_exists())
        for f in ['C2.txt', 'B2.txt', 'B3.txt', 'C1.txt', 'C3.txt', 'C4.txt']:
            t = file_target(f)
            self.assertTrue(t.target_exists())
            t.unlink()
        #
        # test 3, generate two separate trees
        #
        dag = Base_Executor(wf).initialize_dag(targets=['B3.txt', 'C2.txt'])
        # note that A2 is no longer mentioned
        self.assertDAG(
            dag, '''
strict digraph "" {
"B3 (B3.txt)";
"C2 (C2.txt)";
"C4 (C4.txt)";
"C4 (C4.txt)" -> "C2 (C2.txt)";
}
''')
        Base_Executor(wf).run(targets=['B3.txt', 'C2.txt'])
        for f in ['A1.txt', 'B1.txt', 'A2.txt', 'B2.txt', 'C1.txt', 'C3.txt']:
            self.assertFalse(file_target(f).target_exists())
        for f in ['C2.txt', 'B3.txt', 'C4.txt']:
            t = file_target(f)
            self.assertTrue(t.target_exists())
            t.unlink()

    def test_pattern_reuse(self):
        '''Test repeated use of steps that use pattern and produce different files.'''
        #
        for f in [
                'A1.txt', 'A2.txt', 'B1.txt', 'B1.txt.p', 'B2.txt', 'B2.txt.p'
        ]:
            if file_target(f).exists():
                file_target(f).unlink()
        #
        #  A1 <- P <- B1
        #  A1 <- P <- B2
        #  A2
        #
        script = SoS_Script('''
[A_1]
input: 'B1.txt.p', 'B2.txt.p'
output: 'A1.txt'
run:
    touch A1.txt

[A_2]
output: 'A2.txt'
run:
    touch A2.txt

[B1: provides='B1.txt']
run:
    touch B1.txt

[B2: provides='B2.txt']
run:
    touch B2.txt

[P: provides='{filename}.p']
input: filename
run: expand=True
    touch {_output}
''')
        # the workflow should call step K for step C_2, but not C_3
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''
strict digraph "" {
"P (B2.txt.p)";
"B1 (B1.txt)";
"B2 (B2.txt)";
A_2;
A_1;
"P (B1.txt.p)";
"P (B2.txt.p)" -> A_1;
"B1 (B1.txt)" -> "P (B1.txt.p)";
"B2 (B2.txt)" -> "P (B2.txt.p)";
A_1 -> A_2;
"P (B1.txt.p)" -> A_1;
}
''')
        Base_Executor(wf).run()
        for f in [
                'A1.txt', 'A2.txt', 'B1.txt', 'B1.txt.p', 'B2.txt', 'B2.txt.p'
        ]:
            t = file_target(f)
            self.assertTrue(t.target_exists(), '{} should exist'.format(f))
            t.unlink()

    def test_parallel_execution(self):
        '''Test basic parallel execution
        A1 <- None
        A2 <- B2
        '''
        for f in ['A1.txt', 'B2.txt', 'A2.txt']:
            if file_target(f).exists():
                file_target(f).unlink()
        script = SoS_Script('''
[A_1]
output: 'A1.txt'
run:
    sleep 0
    touch A1.txt

[A_2]
input:  'B2.txt'
output: 'A2.txt'
run:
    sleep 0
    touch A2.txt

[B: provides='B2.txt']
output: 'B2.txt'
run:
    touch B2.txt


''')
        # the workflow should call step K for step C_2, but not C_3
        wf = script.workflow()
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''
strict digraph "" {
A_1;
A_2;
"B (B2.txt)";
"B (B2.txt)" -> A_2;
}
''')
        env.max_jobs = 4
        #env.verbosity = 4
        Base_Executor(wf).run()
        # the process is slower after switching to spawn mode
        for f in ['A1.txt', 'B2.txt', 'A2.txt']:
            if file_target(f).exists():
                file_target(f).unlink()

    def test_shared_dependency(self):
        #
        # shared variable should introduce additional dependency
        #
        for f in ['A1.txt']:
            if file_target(f).exists():
                file_target(f).unlink()
        #
        # A1 introduces a shared variable ss, A3 depends on ss but not A2
        #
        script = SoS_Script('''
[A_1: shared='ss']
ss = 'A1'

[A_2]
input: None

run:
    sleep 0

[A_3]
input: None
import time
time.sleep(0)
with open(f"{ss}.txt", 'w') as tmp:
    tmp.write('test')

''')
        wf = script.workflow('A')
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''
strict digraph "" {
A_3;
A_1;
A_2;
A_1 -> A_3;
}
''')
        env.max_jobs = 3
        Base_Executor(wf).run()
        for f in ['A1.txt']:
            self.assertTrue(file_target(f).target_exists())
            file_target(f).unlink()

    def test_literal_connection(self):
        '''Testing the connection of steps with by variables.'''
        for f in ['A1.txt']:
            if file_target(f).exists():
                file_target(f).unlink()
        #
        # A1 introduces a shared variable ss, A3 depends on ss but not A2
        #
        script = SoS_Script('''
[A_1: shared='p']
run:
    touch 'A1.txt'

p = 'A1.txt'

[A_2]
input: None

run:
    sleep 0

[A_3]
input: p
depends: sos_variable('p')

run:
    sleep 0

[A_4]
input: p
depends: sos_variable('p')
run:
    sleep 0

[A_5]
input: dynamic(p)
depends: sos_variable('p')
''')
        wf = script.workflow('A')
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''
strict digraph "" {
A_1;
A_4;
A_2;
A_3;
A_5;
A_1 -> A_4;
A_1 -> A_3;
A_1 -> A_5;
}
''')
        env.max_jobs = 3
        Base_Executor(wf).run()
        for f in ['A1.txt']:
            self.assertTrue(file_target(f).target_exists())
            file_target(f).unlink()

    def test_variable_target(self):
        '''Test dependency caused by variable usage.'''
        script = SoS_Script(r'''
[A: shared='b']
b = 1

[C: shared={'c':'k'}]
k = 2

[all: shared='p']
depends: sos_variable('c'), sos_variable('b')

p = c + b

''')
        wf = script.workflow('all')
        Base_Executor(wf).run()
        self.assertTrue(env.sos_dict['p'], 3)

    def test_reverse_shared_variable(self):
        '''Test shared variables defined in auxiliary steps'''
        if file_target('a.txt').exists():
            file_target('a.txt').unlink()
        script = SoS_Script(r'''
[A: shared='b', provides='a.txt']
b = 1
run:
    touch a.txt

[B_1]
depends: 'a.txt'

[B_2]
print(b)

''')
        wf = script.workflow('B')
        Base_Executor(wf).run()
        self.assertTrue(env.sos_dict['b'], 1)

    def test_chained_depends(self):
        '''Test chain dependent'''
        script = SoS_Script(r'''
# this step provides variable `var`
[index: provides='{filename}.bam.bai']
input: f"{filename}.bam"
run: expand=True
   echo "Generating {_output}"
   touch {_output}

[call: provides='{filename}.vcf']
input:   f"{filename}.bam"
depends: f"{_input}.bai"
run: expand=True
   echo "Calling variants from {_input} with {_depends} to {_output}"
   touch {_output}
''')
        if file_target('a.bam.bai').exists():
            file_target('a.bam.bai').unlink()
        if file_target('a.vcf').exists():
            file_target('a.vcf').unlink()
        self.touch('a.bam')
        Base_Executor(script.workflow()).run(targets=['a.vcf'])
        for f in ('a.vcf', 'a.bam', 'a.bam.bai'):
            if file_target(f).exists():
                file_target(f).unlink()

    def test_output_of_dag(self):
        '''Test output of dag'''
        #
        #for f in ['A1.txt', 'A2.txt', 'C2.txt', 'B2.txt', 'B1.txt', 'B3.txt', 'C1.txt', 'C3.txt', 'C4.txt']:
        #    if file_target(f).exists():
        #        file_target(f).unlink()
        #
        #  A1 <- B1 <- B2 <- B3
        #   |
        #   |
        #  \/
        #  A2 <- B2 <- C1 <- C2 <- C4
        #                    C3
        #
        script = SoS_Script('''
[A_1]
input: 'B1.txt'
output: 'A1.txt'
run:
    touch A1.txt

[A_2]
depends:  'B2.txt'
run:
    touch A2.txt

[B1: provides='B1.txt']
depends: 'B2.txt'
run:
    touch B1.txt

[B2: provides='B2.txt']
depends: 'B3.txt', 'C1.txt'
run:
    touch B2.txt

[B3: provides='B3.txt']
run:
    touch B3.txt

[C1: provides='C1.txt']
depends: 'C2.txt', 'C3.txt'
run:
    touch C1.txt

[C2: provides='C2.txt']
depends: 'C4.txt'
run:
    touch C2.txt

[C3: provides='C3.txt']
depends: 'C4.txt'
run:
    touch C3.txt

[C4: provides='C4.txt']
run:
    touch C4.txt

        ''')
        # the workflow should call step K for step C_2, but not C_3
        wf = script.workflow(use_default=False)
        #
        # test 1, we only need to generate target 'B1.txt'
        Base_Executor(
            wf,
            config={
                'output_dag': 'test_outofdag1.dot',
                'trace_existing': True
            }).initialize_dag(targets=['B1.txt'])
        # note that A2 is no longer mentioned
        self.assertDAG(
            'test_outofdag1.dot', '''
strict digraph "" {
"B3 (B3.txt)";
"C4 (C4.txt)";
"C2 (C2.txt)";
"C1 (C1.txt)";
"B1 (B1.txt)";
"B2 (B2.txt)";
"C3 (C3.txt)";
"B3 (B3.txt)" -> "B2 (B2.txt)";
"C4 (C4.txt)" -> "C3 (C3.txt)";
"C4 (C4.txt)" -> "C2 (C2.txt)";
"C2 (C2.txt)" -> "C1 (C1.txt)";
"C1 (C1.txt)" -> "B2 (B2.txt)";
"B2 (B2.txt)" -> "B1 (B1.txt)";
"C3 (C3.txt)" -> "C1 (C1.txt)";
}
''')
        # test 2, we would like to generate two files
        Base_Executor(
            wf,
            config={
                'output_dag': 'test_outofdag2.dot',
                'trace_existing': True
            }).initialize_dag(targets=['B2.txt', 'C2.txt'])
        # note that A2 is no longer mentioned
        self.assertDAG(
            'test_outofdag2.dot', '''
strict digraph "" {
"C4 (C4.txt)";
"B2 (B2.txt)";
"C3 (C3.txt)";
"B3 (B3.txt)";
"C2 (C2.txt)";
"C1 (C1.txt)";
"C4 (C4.txt)" -> "C2 (C2.txt)";
"C4 (C4.txt)" -> "C3 (C3.txt)";
"C3 (C3.txt)" -> "C1 (C1.txt)";
"B3 (B3.txt)" -> "B2 (B2.txt)";
"C2 (C2.txt)" -> "C1 (C1.txt)";
"C1 (C1.txt)" -> "B2 (B2.txt)";
}
''')
        # test 3, generate two separate trees
        #
        Base_Executor(
            wf,
            config={
                'output_dag': 'test_outofdag3.dot',
                'trace_existing': True
            }).initialize_dag(targets=['B3.txt', 'C2.txt'])
        # note that A2 is no longer mentioned
        self.assertDAG(
            'test_outofdag3.dot', '''
strict digraph "" {
"B3 (B3.txt)";
"C2 (C2.txt)";
"C4 (C4.txt)";
"C4 (C4.txt)" -> "C2 (C2.txt)";
}
''')
        for f in ['C2.txt', 'B3.txt', 'C4.txt', 'test.dot', 'test_2.dot']:
            if file_target(f).exists():
                file_target(f).unlink()

    def test_step_with_multiple_output(self):
        '''Test addition of steps with multiple outputs. It should be added only once'''
        script = SoS_Script('''
[test_1: provides=['{}.txt'.format(i) for i in range(10)]]
output: ['{}.txt'.format(i) for i in range(10)]
run:
  touch {output}

[test_2: provides=['{}.txt'.format(i) for i in range(10, 20)]]
depends: ['{}.txt'.format(i) for i in range(10)]
output: ['{}.txt'.format(i) for i in range(10, 20)]
run:
  touch {output}

[default]
depends: ['{}.txt'.format(i) for i in range(10, 20)]
''')
        wf = script.workflow()
        Base_Executor(wf, config={'output_dag': 'test.dot'}).initialize_dag()
        with open('test.dot') as dot:
            lc = len(dot.readlines())
        self.assertTrue(lc, 6)
        file_target('test.dot').unlink()

    def test_auxiliary_sos_step(self):
        '''Testing the use of sos_step with auxiliary step. #736'''
        script = SoS_Script('''
[default]
depends: '1.txt'

[A_1]
print("Hi")


[C_1: provides = "1.txt"]
depends: sos_step("A_1")
run:
touch 1.txt
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_forward_style_depend(self):
        '''Test the execution of forward-style workflow with undtermined dependency'''
        if file_target('a.txt.bak').exists():
            file_target('a.txt.bak').unlink()
        self.touch('a.txt')
        script = SoS_Script('''
[10]
input: 'a.txt'
output: f"{_input}.bak"
run: expand=True
    cp {_input} {_output}

[20]
depends: "a.txt.bak"
run: expand=True
    ls {_depends}
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertTrue(file_target('a.txt.bak').target_exists())

    def test_sos_step_miniworkflow(self):
        '''Test the addition of mini forward workflows introduced by sos_step'''
        script = SoS_Script('''
[a_1]
print(step_name)

[a_2]
print(step_name)
[a_20]
print(step_name)

[b_1]
print(step_name)

[b_2]
print(step_name)

[b_20]
depends: sos_step('c')
print(step_name)

[c_1]
print(step_name)

[c_2]
print(step_name)

[c_20]
print(step_name)



[default]
depends: sos_step('a'), sos_step('b')
''')
        wf = script.workflow()
        Base_Executor(wf, config={'output_dag': 'test.dot'}).run()
        # note that A2 is no longer mentioned
        self.assertDAG(
            'test.dot', '''
strict digraph "" {
default;
a_1;
a_2;
a_20;
b_1;
b_2;
b_20;
c_1;
c_2;
c_20;
a_1 -> a_2;
a_2 -> a_20;
a_20 -> default;
b_1 -> b_2;
b_2 -> b_20;
b_20 -> default;
c_1 -> c_2;
c_2 -> c_20;
c_20 -> b_20;
}
''')
        file_target('test.dot').unlink()

    def test_compound_workflow(self):
        '''Test the DAG of compound workflow'''
        script = SoS_Script('''
[A_1]
[A_2]
[B]
 ''')
        wf = script.workflow('A+B')
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''strict digraph "" {
A_1;
A_2;
B;
A_1 -> A_2;
A_2 -> B;
}''')
        # with empty depends
        script = SoS_Script('''
[A_1]
[A_2]
[B]
depends:
 ''')
        wf = script.workflow('A+B')
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''strict digraph "" {
A_1;
A_2;
B;
A_1 -> A_2;
A_2 -> B;
}''')
        if os.path.isfile('a.txt'):
            os.remove('a.txt')
        script = SoS_Script('''
[A_1]
[A_2]
[C]
output: 'a.txt'
_output.touch()

[B]
depends: 'a.txt'
 ''')
        # with more depends
        wf = script.workflow('A+B')
        dag = Base_Executor(wf).initialize_dag()
        self.assertDAG(
            dag, '''strict digraph "" {
A_1;
A_2;
B;
"C (a.txt)";
A_1 -> A_2;
A_2 -> B;
"C (a.txt)" -> B;
}''')

    def test_provides_sos_variable(self):
        '''Test provides non-filename targets #1341'''
        execute_workflow('''
[count: provides=sos_variable('numNotebooks')]
numNotebooks = 1

[default]
depends: sos_variable('numNotebooks')
print(f"There are {numNotebooks} notebooks in this directory")
        ''')


def test_multi_named_output(clear_now_and_after):
    '''Test DAG built from multiple named_output #1166'''
    clear_now_and_after('a.txt', 'b.txt', 'test_named_output.dot')

    execute_workflow(
        '''
        [A]
        output: A='a.txt', B='b.txt'
        _output.touch()

        [default]
        input: named_output('A'), named_output('B')
        ''',
        options={'output_dag': 'test_named_output.dot'})
    # note that A2 is no longer mentioned
    assertDAG('test_named_output.dot', [
        '''
        strict digraph "" {
        default;
        "A (B)";
        "A (B)" -> default;
        }
        ''', '''
        strict digraph "" {
        default;
        "A (A)";
        "A (A)" -> default;
        }
        '''
    ])
