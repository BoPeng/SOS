#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import socket
import sys
import unittest

from sos.eval import accessed_vars, on_demand_options
from sos.parser import SoS_Script
from sos.pattern import expand_pattern, extract_pattern
from sos.targets import executable, sos_targets, file_target, sos_step
# these functions are normally not available but can be imported
# using their names for testing purposes
from sos.utils import WorkflowDict, env, get_logger, stable_repr, split_fstring, as_fstring
from sos.workflow_executor import analyze_section
from sos.workflow_executor import Base_Executor


def internet_on(host='8.8.8.8', port=80, timeout=3):
    '''Test if internet is connected '''
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except Exception as e:
        print(e)
        return False


with_network = internet_on()


class TestUtils(unittest.TestCase):

    def setUp(self):
        env.reset()

    def test_logger(self):
        '''Test logging level'''
        for verbosity in [0, 1, 2, 3, 4]:
            env.verbosity = verbosity
            get_logger().debug(
                'Verbosity {}:debug message with ``empahsized text`` in between'
                .format(env.verbosity))
            get_logger().info(
                'Verbosity {}:info message with ``empahsized text`` in between'
                .format(env.verbosity))
            get_logger().warning(
                'Verbosity {}:warning message with ``empahsized text`` in between'
                .format(env.verbosity))
            get_logger().error(
                'Verbosity {}:error message with ``empahsized text`` in between'
                .format(env.verbosity))

    def test_workflow_dict(self):
        '''Test workflow dict with attribute access'''
        env.reset()
        d = WorkflowDict()
        d['a'] = 1
        self.assertEqual(d['a'], 1)
        #
        d['a'] += 1
        self.assertEqual(d['a'], 2)

    def test_pattern_match(self):
        '''Test snake match's pattern match facility'''
        res = extract_pattern('{a}-{b}.txt', ['file-1.txt', 'file-ab.txt'])
        self.assertEqual(res['a'], ['file', 'file'])
        self.assertEqual(res['b'], ['1', 'ab'])
        res = extract_pattern('{a}-{b}.txt', ['file--ab--cd.txt'])
        self.assertEqual(res['a'], ['file--ab-'])
        self.assertEqual(res['b'], ['cd'])
        res = extract_pattern('{path}/{to}/{file}.txt', ['/tmp//1.txt'])
        self.assertEqual(res['path'], [None])
        self.assertEqual(res['to'], [None])
        self.assertEqual(res['file'], [None])
        res = extract_pattern('{path}/{to}/{file}.txt', ['/tmp/test/1.txt.txt'])
        self.assertEqual(res['path'], ['/tmp'])
        self.assertEqual(res['to'], ['test'])
        self.assertEqual(res['file'], ['1.txt'])
        # expand_pattern
        env.sos_dict = WorkflowDict({
            'os': os,
            'a': 100,
            'b': 'file name',
            'c': ['file1', 'file2', 'file 3'],
            'd': {
                'a': 'file1',
                'b': 'file2'
            },
        })
        self.assertEqual(expand_pattern('{b}.txt'), ['file name.txt'])
        self.assertEqual(
            expand_pattern('{c}.txt'), ['file1.txt', 'file2.txt', 'file 3.txt'])
        self.assertEqual(
            expand_pattern('{a}_{c}.txt'),
            ['100_file1.txt', '100_file2.txt', '100_file 3.txt'])

    def test_accessed_vars(self):
        '''Test accessed vars of a SoS expression or statement.'''
        self.assertEqual(accessed_vars('''a = 1'''), set())
        self.assertEqual(accessed_vars('''a = b + 2.0'''), {'b'})
        self.assertEqual(accessed_vars('''a = "C"'''), set())
        self.assertEqual(accessed_vars('''a = "C" + f"{D}"'''), {'D'})
        self.assertEqual(accessed_vars('''a = 1 + f"{D + 20:f}" '''), {'D'})
        self.assertEqual(
            accessed_vars(
                '''k, "a.txt", "b.txt", par=f(something) ''', mode='eva'),
            {'k', 'f', 'something'})
        # this is a complicated case because the actual variable depends on the
        # result of an expression... However, in the NO-evaluation case, this is
        # the best we can do.
        self.assertEqual(accessed_vars('''c + f"{D + 1}" '''), {'c', 'D'})
        self.assertEqual(
            accessed_vars('''a, b=2, c=d ''', mode='eva'), {'a', 'd'})

    def test_progress_bar(self):
        '''Test progress bar'''
        env.verbosity = 1
        #
        script = SoS_Script('''

[1]
[2]
[3]
[4]
[5]
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_text_repr(self):
        # the " as the last character can lead to problems...
        script = SoS_Script('''
run:
    echo "Hi, This is from bash"''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # windows does not have #! mechanism so the python code
        # would incorrectly be executed as bat
        if sys.platform == 'win32':
            return
        for text in ('"""a"""', '"b"', r'"""\na\\nb"""', r"'''a\nb'''",
                     """ "a'\\"='" """):
            script = SoS_Script(r'''
a = 1
run: expand=True
   #!/usr/bin/env python
   with open('tmp.txt', 'w') as tmp:
      tmp.write({} + '{}')
k = """b"""'''.format(text, '{a}'))
            wf = script.workflow()
            Base_Executor(wf).run()
            with open('tmp.txt') as tmp:
                self.assertEqual(tmp.read(), eval(text) + '1')
        os.remove('tmp.txt')

    def test_analyze_section(self):
        '''Test analysis of sections (statically)'''
        script = SoS_Script('''
g1 = 'a'
g2 = 1
parameter: p1 = 5
parameter: infiles = 'a.txt'

[A_1: shared='b']
b = p1 + 2
input:  infiles
output: None
c = 5

[A_2]
b = [1, 2, 3]
input: for_each='b'
depends: 'some.txt', executable('ls')
import time
import random

r = random.randint(1, 5)
time.sleep(r)

[A_3]
input: None
print(p1)

[A_4]
input: None
task:
python: expand=True
   print(f'{output}')

[A_5]
task:
   print(f'{_output}')
''')
        wf = script.workflow('A')
        Base_Executor(wf)
        for section in wf.sections:
            res = analyze_section(section)
            if section.names[0][1] == '1':
                self.assertTrue(res['step_input'].undetermined())
                self.assertEqual(res['step_depends'], sos_targets())
                self.assertEqual(res['step_output'], sos_targets())
                self.assertEqual(res['environ_vars'], {'b', 'p1', 'infiles'})
                self.assertEqual(res['signature_vars'], {'c'})
                self.assertEqual(res['changed_vars'], {'b'})
            elif section.names[0][1] == '2':
                self.assertEqual(res['step_input'], sos_targets())
                self.assertEqual(res['step_depends'],
                                 sos_targets('some.txt', executable('ls')))
                self.assertTrue(res['step_output'].unspecified())
                # for_each will not be used for DAG
                self.assertEqual(res['environ_vars'],
                                 {'b', 'for_each', 'executable'})
                self.assertEqual(res['signature_vars'], {'r', 'time', 'random'})
                self.assertEqual(res['changed_vars'], set())
            elif section.names[0][1] == '4':
                self.assertTrue('output' in res['signature_vars'])
            elif section.names[0][1] == '5':
                self.assertTrue('output' not in res['signature_vars'])

    def test_on_demand_options(self):
        '''Test options that are evaluated upon request.'''
        options = on_demand_options({'a': '"est"', 'b': 'c', 'c': 'e + 2'})
        env.sos_dict = WorkflowDict({
            'e': 10,
        })
        self.assertEqual(options['a'], 'est')
        self.assertRaises(KeyError, options.__getitem__, 'd')
        self.assertRaises(ValueError, options.__getitem__, 'b')
        self.assertEqual(options['c'], 12)
        #
        env.sos_dict.set('e', 20)
        self.assertEqual(options['c'], 22)
        env.sos_dict.set('c', 200)
        self.assertEqual(options['b'], 200)

    def test_stable_repr(self):
        self.assertEqual(stable_repr({1, 2, '3', '1'}), "{'1', '3', 1, 2}")
        self.assertEqual(stable_repr({1: 2, 3: 4}), "{1:2, 3:4}")
        self.assertEqual(stable_repr([1, 3, 4]), "[1, 3, 4]")

    def test_file_sig(self):
        '''test save and validate of file signature'''
        with open('test_sig.txt', 'w') as ts:
            ts.write('ba')
        a = file_target('test_sig.txt')
        a.write_sig()
        self.assertTrue(a.validate())
        #
        a.zap()
        self.assertTrue(a.validate())
        with open('test_sig.txt', 'w') as ts:
            ts.write('bac')
        self.assertFalse(a.validate())

    def test_split_fstring(self):
        '''Test function to split f-string in pieces '''
        for string, pieces in [
            ('hello world', ['hello world']),
            ('hello world {', None),
            ('{ hello world', None),
            ('hello world {}', None),
            ('hello world {a b}', None),
            ('{{hello world', ['{{hello world']),
            ('hello {{}} world', ['hello {{}} world']),
            ('hello {{ world }}', ['hello {{ world }}']),
            ('hello }} world', ['hello }} world']),
            ('hello {1} world', ['hello ', '1', ' world']),
            ('hello {a+b } }} world', ['hello ', 'a+b ', ' }} world']),
            ('hello {a+b:r} }} world', ['hello ', 'a+b:r', ' }} world']),
            ('hello {{{a+b!r} }} world', ['hello {{', 'a+b!r', ' }} world']),
            ('hello {a+b + {1,2}.pop() } }} world',
             ['hello ', 'a+b + {1,2}.pop() ', ' }} world']),
        ]:
            if pieces is None:
                self.assertRaises(SyntaxError, split_fstring, string)
            else:
                self.assertEqual(split_fstring(string), pieces)

    def test_as_fstring(self):
        '''Test as fstring '''
        for string, fstring in [
            ('hello world', 'fr"""hello world"""'),
            ('{{hello world', 'fr"""{{hello world"""'),
            ('hello {{}} world', 'fr"""hello {{}} world"""'),
            ('hello {{ world }}', 'fr"""hello {{ world }}"""'),
            ('hello }} \n world', 'fr"""hello }} \n world"""'),
            (r'hello }} \n world', 'fr"""hello }} \\n world"""'),
            ('hello {1} world', 'fr"""hello {1} world"""'),
            ('hello {a+b } }} world', 'fr"""hello {a+b } }} world"""'),
            ('hello {a+b:r} }} world', 'fr"""hello {a+b:r} }} world"""'),
            ('''hello """ \'\'\' {a+b } }} world''',
             'f\'hello """ \\\'\\\'\\\' {a+b } }} world\''),
            ('hello {{{a+b!r} }} world', 'fr"""hello {{{a+b!r} }} world"""'),
            ('hello {a+b + {1,2}.pop() } }} world',
             'fr"""hello {a+b + {1,2}.pop() } }} world"""'),
            ('''hello {'a'+b !r} }} world''',
             'fr"""hello {\'a\'+b !r} }} world"""'),
            ('''hello """ \'\'\' {'a'+"b" + {"c", "D"}.pop() } }} world''',
             '\'hello """ \\\'\\\'\\\' {0} }} world\'.format(\'a\'+"b" + {"c", "D"}.pop() )'
            ),
        ]:
            self.assertEqual(as_fstring(string), fstring)


def test_analyze_output_from():
    '''Test extracting of from=value option from input'''
    script = SoS_Script('''
[A_1]
input:  output_from('B')

[A_2]
input: something_unknown, sos_groups(output_from(['C1', 'C2']), by=2), group_by=1
''')
    wf = script.workflow('A')
    Base_Executor(wf)
    for section in wf.sections:
        res = analyze_section(section, analysis_type='forward')
        if section.names[0][1] == 1:
            assert res['step_depends'] == sos_targets(sos_step('B'))
        if section.names[0][1] == 2:
            assert res['step_depends'] == sos_targets(
                sos_step('C1'), sos_step('C2'))
