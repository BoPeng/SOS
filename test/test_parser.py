#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import pytest
import subprocess
import unittest

from sos.parser import ParsingError, SoS_Script
from sos.targets import file_target, sos_targets, path, paths
from sos.utils import ArgumentError, env
from sos.workflow_executor import Base_Executor
from sos.converter import extract_workflow
from sos import execute_workflow

section1_sos = '''
#!/usr/bin/env sos-runner
#fileformat=SOS1.1

#
# this is a test sos script
#
var1='value1'
var2 = 'value2'
var3 = [var1,
  var2]

# section
# description for workflow section
#
[parameters]
# par1 string
par1 = 'var1'

# par2 list
par2 = ['a', 'b', 'c']

# par3 multiline
par3 = ['a',
	'b']

[*_0]
var0 = '0'

[section_10]
#
#step 10
var1 = 'a'

[section_2 : shared='var3']
var2 = 'a'
input: var1
output: var2

var3 = 'a'

[section_3, *_4 : shared='var4']
output:
	var2,
	var3

print()
var4= 'value4'

[chapter_5]
var4='5'
 '''


class TestParser(unittest.TestCase):

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

    def test_file_format(self):
        '''Test recognizing the format of SoS script'''
        # file format must be 'fileformat=SOSx.x'
        self.assertRaises(ParsingError, SoS_Script, '#fileformat=SS2')
        self.assertRaises(ParsingError, SoS_Script, '#fileformat=SOS1.0beta')
        #
        # parse a larger script with gormat 1.1
        SoS_Script(section1_sos)
        # not the default value of 1.0
        #self.assertEqual(script.format_version, '1.1')

    def test_workflows(self):
        '''Test workflows defined in SoS script'''
        script = SoS_Script('''[0]''')
        self.assertEqual(sorted(script.workflows), [''])
        script = SoS_Script('''[0]\n[1]''')
        self.assertEqual(sorted(script.workflows), [''])
        script = SoS_Script('''[0]\n[*_1]''')
        self.assertEqual(sorted(script.workflows), [''])
        script = SoS_Script('''[0]\n[*_1]\n[auxiliary:provides='{a}.txt']''')
        self.assertEqual(sorted(script.workflows), ['', 'auxiliary'])
        script = SoS_Script('''[0]\n[*_1]\n[human_1]''')
        self.assertEqual(sorted(script.workflows), ['', 'human'])
        script = SoS_Script('''[0]\n[*_1]\n[human_1]\n[mouse_2]''')
        self.assertEqual(sorted(script.workflows), ['', 'human', 'mouse'])
        script = SoS_Script('''[0]\n[*_1]\n[human_1]\n[mouse_2]\n[s*_2]''')
        self.assertEqual(sorted(script.workflows), ['', 'human', 'mouse'])
        # unnamed
        script = SoS_Script('''[0]\n[*_1]\n[human_1]\n[mouse]\n[s*_2]''')
        self.assertEqual(sorted(script.workflows), ['', 'human', 'mouse'])
        #
        # workflow name with -
        script = SoS_Script('''[proc-1]\n[test-case_2]''')
        self.assertEqual(sorted(script.workflows), ['proc-1', 'test-case'])
        script.workflow('proc-1')
        script.workflow('proc-1 + test-case:2')

    def test_sections(self):
        '''Test section definitions'''
        # bad names
        for badname in ['56_1', '_a', 'a_', '1x', '*', '?']:
            self.assertRaises(ParsingError, SoS_Script, '[{}]'.format(badname))
        # bad options
        for badoption in ['ss']:
            self.assertRaises(ParsingError, SoS_Script,
                              '[0:{}]'.format(badoption))
        # allowed names
        for name in ['a5', 'a_5', '*_0', 'a*1_100']:
            SoS_Script('[{}]'.format(name))
        # allowed names with alias
        for name in [
                'a5 (p1)', 'a_5 (something fun)', '*_0 (no way)', 'a*1_100'
        ]:
            SoS_Script('[{}]'.format(name))
        # duplicate sections
        self.assertRaises(ParsingError, SoS_Script, '''[1]\n[1]''')
        self.assertRaises(ParsingError, SoS_Script, '''[1]\n[3]\n[2,1]''')
        self.assertRaises(ParsingError, SoS_Script, '''[a_1]\n[a_3]\n[*_1]''')
        #
        # no duplicated section header
        SoS_Script('''[a_1]\n[a_3]\n[b*_1]''')
        #
        # global section
        self.assertRaises(ParsingError, SoS_Script, '''[global, step_10]''')

    def test_parameters(self):
        '''Test ending parameters with new line #1311'''
        self.assertRaises(ParsingError, SoS_Script,
                          '''input: ['a.txt'\n\n'b.txt']\n''')

    def test_global_variables(self):
        '''Test definition of variables'''
        # allow definition
        SoS_Script('''a = '1' ''')
        SoS_Script('''a = ['a', 'b'] ''')
        # but this one has incorrect syntax
        self.assertRaises(ParsingError, SoS_Script, '''a = 'b  ''')
        # This one also does not work because b is not defined.
        #delattr(env, 'sos_dict')
        #script = SoS_Script('''a = b\n[0] ''')
        #wf = script.workflow()
        #dag = Base_Executor(wf).run(mode='dryrun')
        #self.assertRaises(ValueError, Base_Executor(wf).run, dag)
        # multi-line string literal
        SoS_Script('''a = """
this is a multi line
string """
''')
        # multi-line list literal, even with newline in between
        SoS_Script('''a = [
'a',

'b'
]
''')
        #
        SoS_Script(section1_sos)
        # not the default value of 1.0
        #
        script = SoS_Script('''
[global]
a = 1

[b]
print(a)
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_parameters(self):
        '''Test parameters section'''
        # directive not allowed in parameters
        script = SoS_Script(section1_sos)
        wf = script.workflow('chapter:0')
        # self.assertRaises(ArgumentError, Base_Executor(wf).run,
        #    args=['--not_exist'])
        # self.assertRaises(ArgumentError, Base_Executor(wf).run,
        #    args=['--par1', 'a', 'b'])
        #
        script = SoS_Script('''
parameter: a = [1, 2]
[0]
''')
        wf = script.workflow()
        self.assertEqual(list(wf.parameters().keys()), ['a'])
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['a'], [1, 2])
        env.sos_dict.pop('a')
        wf = script.workflow()
        Base_Executor(wf, args=['--a', '3']).run()
        self.assertEqual(env.sos_dict['a'], [3])
        env.sos_dict.pop('a')
        wf = script.workflow()
        Base_Executor(wf, args=['--a', '3', '5']).run()
        self.assertEqual(env.sos_dict['a'], [3, 5])
        env.sos_dict.pop('a')
        #
        script = SoS_Script('''
# comment
# comment
parameter: a = ['a.txt', 'b.txt']
[0]
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['a'], ['a.txt', 'b.txt'])
        env.sos_dict.pop('a')
        wf = script.workflow()
        Base_Executor(wf, args=['--a', '3']).run()
        self.assertEqual(env.sos_dict['a'], ['3'])
        env.sos_dict.pop('a')
        wf = script.workflow()
        Base_Executor(wf, args=['--a', '3', '5']).run()
        self.assertEqual(env.sos_dict['a'], ['3', '5'])
        env.sos_dict.pop('a')
        #
        # test parameter using global definition
        script = SoS_Script('''
a="100"

# comment
parameter: b=str(int(a)+1)
[0]
''')
        wf = script.workflow()
        self.assertEqual(list(wf.parameters().keys()), ['b'])
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['b'], '101')
        env.sos_dict.pop('b')
        #
        env.sos_dict.clear()
        script = SoS_Script('''
a=100

parameter: b=a+1
[0]
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['b'], 101)
        env.sos_dict.pop('b')
        #
        script = SoS_Script('''
a=100

parameter: b=a+1.
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '1000']).run()
        #
        self.assertEqual(env.sos_dict['b'], 1000)
        env.sos_dict.pop('b')
        #
        # argument has hve a value
        self.assertRaises(ParsingError, SoS_Script, '''
[0]
parameter: b=

''')
        # if it is a type, must provide value
        script = SoS_Script('''
# comment
parameter: b = int
[0]
''')
        wf = script.workflow()
        self.assertRaises(ArgumentError, Base_Executor, wf)
        #
        script = SoS_Script('''
parameter: b = list
[0]
''')
        self.assertEqual(list(wf.parameters().keys()), ['b'])
        wf = script.workflow()
        self.assertRaises(ArgumentError, Base_Executor, wf)
        # also require the type
        script = SoS_Script('''
parameter: b = int
[0]
''')
        wf = script.workflow()
        self.assertRaises(
            ArgumentError, Base_Executor, wf, args=['--b', 'file'])
        #
        script = SoS_Script('''
parameter: b = int
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '5']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], 5)
        env.sos_dict.pop('b')
        # string
        script = SoS_Script('''
parameter: b = str
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '5']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], '5')
        env.sos_dict.pop('b')
        # list is ok
        script = SoS_Script('''
parameter: b = list
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '5']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], ['5'])
        # bool required
        script = SoS_Script('''
# comment
parameter: b = bool
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], True)
        env.sos_dict.pop('b')
        Base_Executor(wf, args=['--no-b']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], False)
        env.sos_dict.pop('b')
        # bool with default True
        script = SoS_Script('''
parameter: b = True
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=[]).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], True)
        env.sos_dict.pop('b')
        Base_Executor(wf, args=['--b']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], True)
        env.sos_dict.pop('b')
        Base_Executor(wf, args=['--no-b']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], False)
        env.sos_dict.pop('b')
        # bool with default False
        script = SoS_Script('''
parameter: b = False
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=[]).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], False)
        env.sos_dict.pop('b')
        Base_Executor(wf, args=['--b']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], True)
        env.sos_dict.pop('b')
        Base_Executor(wf, args=['--no-b']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], False)
        env.sos_dict.pop('b')
        #
        # parameters cannot coincide with a readonly global variable
        # are masked by previous definition
        script = SoS_Script('''
a = 4
parameter: a = 5
[0]
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor, wf, args=['--a', 7])
        #self.assertEqual(env.sos_dict['a'], 4)
        #
        # test parameters with dash
        script = SoS_Script('''
parameter: a_b = 5
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a_b', '10']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'], 10)
        env.sos_dict.pop('a_b')
        Base_Executor(wf, args=['--a-b', '10']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'], 10)
        env.sos_dict.pop('a_b')
        #
        #
        script = SoS_Script('''
parameter: a_b = int
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a_b', '10']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'], 10)
        env.sos_dict.pop('a_b')
        Base_Executor(wf, args=['--a-b', '10']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'], 10)
        env.sos_dict.pop('a_b')
        #
        # test support for type path, paths, file_target and sos_targets
        script = SoS_Script('''
parameter: path_var = path
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--path-var', 'a.txt']).run(mode='run')
        self.assertTrue(isinstance(env.sos_dict['path_var'], path))
        #
        script = SoS_Script('''
parameter: path_var = file_target
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--path-var', 'a.txt']).run(mode='run')
        self.assertTrue(isinstance(env.sos_dict['path_var'], file_target))
        #
        script = SoS_Script('''
parameter: path_var = paths
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--path-var', 'a.txt', 'b.txt']).run(mode='run')
        self.assertTrue(isinstance(env.sos_dict['path_var'], paths))
        #
        #
        script = SoS_Script('''
parameter: path_var = sos_targets
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--path-var', 'a.txt']).run(mode='run')
        self.assertTrue(isinstance(env.sos_dict['path_var'], sos_targets))

        #
        script = SoS_Script('''
parameter: path_var = path('a.txt')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--path-var', 'a.txt']).run(mode='run')
        self.assertTrue(isinstance(env.sos_dict['path_var'], path))
        #
        script = SoS_Script('''
parameter: path_var = file_target('a.txt')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--path-var', 'a.txt']).run(mode='run')
        self.assertTrue(isinstance(env.sos_dict['path_var'], file_target))
        #
        script = SoS_Script('''
parameter: path_var = paths('a.txt')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--path-var', 'a.txt', 'b.txt']).run(mode='run')
        self.assertTrue(isinstance(env.sos_dict['path_var'], paths))
        #
        #
        script = SoS_Script('''
parameter: path_var = sos_targets('a.txt')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--path-var', 'a.txt']).run(mode='run')
        self.assertTrue(isinstance(env.sos_dict['path_var'], sos_targets))

        #
        # Test allow the use of sos keywords as parameters #1041
        script = SoS_Script('''\
[1]
parameter: input = 5
output = 10
python: expand=True
  print({input})
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # multiple parameters
        script = SoS_Script('''
parameter: a_b = int
[0]
parameter: c_b = list
''')
        wf = script.workflow()
        self.assertEqual(sorted(list(wf.parameters().keys())), ['a_b', 'c_b'])

    def test_param_in_task(self):
        '''Test specification of parameters in tasks'''
        self.assertRaises(
            Exception, SoS_Script, '''\
[1]
output: 'a.txt'
task:
parameter: value = 'a'
sh: expand=True
echo {value} >  a.txt
''')
        # multiple parameters
        script = SoS_Script('''
parameter: a_b = int
[0]
parameter: c_b = list
''')
        wf = script.workflow()
        self.assertEqual(sorted(list(wf.parameters().keys())), ['a_b', 'c_b'])

    def test_type_trait_parameter(self):
        # type trait
        script = SoS_Script('''
parameter: b
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '5']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], '5')
        #
        script = SoS_Script('''
parameter: b :str
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '5']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], '5')
        #
        script = SoS_Script('''
parameter: b : list
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '5']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], ['5'])

        #
        script = SoS_Script('''
parameter: b : list = 5
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '5']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], 5)
        #
        script = SoS_Script('''
parameter: b : int
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', '5']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'], 5)

    def test_input_target(self):
        # test input of targets
        script = SoS_Script('''
parameter: b : file_target
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'].__class__.__name__, 'file_target')
        #
        script = SoS_Script('''
parameter: b = file_target('file')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'].__class__.__name__, 'file_target')
        #
        script = SoS_Script('''
parameter: b : sos_targets
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', 'aaa', 'bbb']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'].__class__.__name__, 'sos_targets')
        #
        script = SoS_Script('''
parameter: b = sos_targets('file')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'].__class__.__name__, 'sos_targets')
        #
        script = SoS_Script('''
parameter: a_b : file_target
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a-b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'].__class__.__name__, 'file_target')
        #
        script = SoS_Script('''
parameter: a_b = file_target('file')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a-b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'].__class__.__name__, 'file_target')
        #
        script = SoS_Script('''
parameter: a_b : sos_targets
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a-b', 'aaa', 'bbb']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'].__class__.__name__, 'sos_targets')
        #
        script = SoS_Script('''
parameter: a_b = sos_targets('file')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a-b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'].__class__.__name__, 'sos_targets')
        #
        #
        #
        #
        script = SoS_Script('''
parameter: b : path
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'].__class__.__name__, 'path')
        #
        script = SoS_Script('''
parameter: b = path('file')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'].__class__.__name__, 'path')
        #
        script = SoS_Script('''
parameter: b : paths
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', 'aaa', 'bbb']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'].__class__.__name__, 'paths')
        #
        script = SoS_Script('''
parameter: b = paths('file')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['b'].__class__.__name__, 'paths')
        #
        script = SoS_Script('''
parameter: a_b : path
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a-b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'].__class__.__name__, 'path')
        #
        script = SoS_Script('''
parameter: a_b = path('file')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a-b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'].__class__.__name__, 'path')
        #
        script = SoS_Script('''
parameter: a_b : paths
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a-b', 'aaa', 'bbb']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'].__class__.__name__, 'paths')
        #
        script = SoS_Script('''
parameter: a_b = paths('file')
[0]
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--a-b', 'aaa']).run(mode='dryrun')
        self.assertEqual(env.sos_dict['a_b'].__class__.__name__, 'paths')
        #

    def test_section_directives(self):
        '''Test directives of sections'''
        # cannot be in the global section
        # multi-line OK
        SoS_Script('''
[0]
input: 'filename',
    'filename1'

''')
        # An abusive case with multi-line OK, from first column ok
        SoS_Script('''
[0]
input: 'filename',
'filename1',
filename4,
opt1=value
output:
    blah

depends:
'something else'
''')
        # option with expression ok
        SoS_Script('''
[0]
input: 'filename',  'filename2', opt=value==1

''')
        # unrecognized directive, allowed now
        SoS_Script('''
[0]
something: 'filename',  filename2, opt=value==1
''')
        # need commma
        self.assertRaises(ParsingError, SoS_Script, '''
[0]
input: 'filename'  filename2
''')
        # can be after action
        SoS_Script('''
[0]
func()
input: 'filename',  'filename2', opt=value==1
''')
        # assignments between directives are allowed
        SoS_Script('''
[0]
input: 'filename',  'filename2', opt=value==1
a = 'some text'
output: 'filename',  'filename2', opt=value==1
''')
        # can have action between directives
        SoS_Script('''
[0]
input: 'filename',  'filename2', opt=value==1
abc
output: 'filename',  'filename2', opt=value==1
''')

    def test_script_format(self):
        '''Test putting scripts directly under action'''
        script = SoS_Script('''
[0]
input: 'filename',  'filename2', opt=value==1
R:

open('something')
save.put()

''')
        script = SoS_Script('''
[0]
input: 'filename',  'filename2', opt=value==1
R: concurrent = True

open('something')
save.put()

''')
        script = SoS_Script('''
[0]
input: 'filename',  'filename2', opt=value==1
R: concurrent = True,
    workdir = 'someelse else'

open('something')
save.put()

''')
        # test dedent
        script = SoS_Script('''
[0]
python3:
    from sos.runtime import logger
    logger.warning('I am from a dented text')
    if 1:
        logger.warning('Another warning')
''')
        script.workflow()
        # with section head in the script,
        # this will not work even if the embedded
        # python script is perfectly valid.
        self.assertRaises(
            ParsingError, SoS_Script, '''
[0]
input: 'filename',  'filename2', opt=value==1
python3:

with open('something') as e:
   e.write("""
[section]
""")

''')
        # scripts with section head-like lines
        script = SoS_Script('''
[0]
R:
some::function(param)
''')
        script.workflow()
        #
        # script with first-line indent
        #
        script = SoS_Script('''
[0]
sh:
  echo "a"

sh('echo "b"')
''')
        script.workflow()
        #
        # script with triple quote and format string
        # #1211
        script = SoS_Script('''
[1]
python3: expand = "${ }"

  ld = ${'100'}
  a =  """doc"""

''')
        wf = script.workflow()
        Base_Executor(wf).run()
        script = SoS_Script('''
[default]
report: expand = "${ }"
  ld_file = ${_input['']:r}
  """ \'\'\'
  {}, ${_output:r},

''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_input(self):
        '''Test input directive'''
        self.touch(['a.txt', 'b.txt', 'a.pdf', 'a0', 'a1'])
        script = SoS_Script('''
[0]
files = ['a.txt', 'b.txt']

input: 'a.pdf', files

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        #
        # test input types
        script = SoS_Script('''
[0:shared={'i':'_input', 'o':'_output'}]
files = (f"a{i}" for i in range(2))
input: {'a.txt', 'b.txt'}, files
output: (f"a{x}" for x in _input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(
            sorted(env.sos_dict['i']),
            sos_targets(['a.txt', 'a0', 'a1', 'b.txt']))
        self.assertEqual(
            sorted(env.sos_dict['o']),
            sos_targets(['aa.txt', 'aa0', 'aa1', 'ab.txt']))

    def test_group_by(self):
        '''Test group_by parameter of step input'''
        # group_by = 'all'
        self.touch(['a{}.txt'.format(x) for x in range(15)])
        #
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: [f'a{x}.txt' for x in range(1, 5)], group_by='all'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'],
                         [sos_targets('a1.txt', 'a2.txt', 'a3.txt', 'a4.txt')])
        self.assertEqual(env.sos_dict['executed'][0].labels, ['0'] * 4)
        # group_by = 'single'
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 5)], group_by='single'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a1.txt'),
            sos_targets('a2.txt'),
            sos_targets('a3.txt'),
            sos_targets('a4.txt')
        ])
        # group_by = 'pairs'
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: [f'a{x}.txt' for x in range(1, 5)], group_by='pairs'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(
            env.sos_dict['executed'],
            [sos_targets('a1.txt', 'a3.txt'),
             sos_targets('a2.txt', 'a4.txt')])
        # group_by = 'pairs2'
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: [f'a{x}.txt' for x in range(1, 9)], group_by='pairs2'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a1.txt', 'a2.txt', 'a5.txt', 'a6.txt'),
            sos_targets('a3.txt', 'a4.txt', 'a7.txt', 'a8.txt')
        ])
        # group_by = 'pairs3'
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: [f'a{x}.txt' for x in range(1, 13)], group_by='pairs3'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a1.txt', 'a2.txt', 'a3.txt', 'a7.txt', 'a8.txt',
                        'a9.txt'),
            sos_targets('a4.txt', 'a5.txt', 'a6.txt', 'a10.txt', 'a11.txt',
                        'a12.txt')
        ])

        # group_by = 'pairwise'
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 5)], group_by='pairwise'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a1.txt', 'a2.txt'),
            sos_targets('a2.txt', 'a3.txt'),
            sos_targets('a3.txt', 'a4.txt')
        ])
        # group_by = 'pairwiseN'
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 7)], group_by='pairwise2'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a1.txt', 'a2.txt', 'a3.txt', 'a4.txt'),
            sos_targets('a3.txt', 'a4.txt', 'a5.txt', 'a6.txt')
        ], f'obtained {env.sos_dict["executed"]}')

        # group_by = 'combinations'
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 5)], group_by='combinations'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a1.txt', 'a2.txt'),
            sos_targets('a1.txt', 'a3.txt'),
            sos_targets('a1.txt', 'a4.txt'),
            sos_targets('a2.txt', 'a3.txt'),
            sos_targets('a2.txt', 'a4.txt'),
            sos_targets('a3.txt', 'a4.txt')
        ])

        # group_by = 'combinations3'
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 5)], group_by='combinations3'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets(['a1.txt', 'a2.txt', 'a3.txt']),
            sos_targets(['a1.txt', 'a2.txt', 'a4.txt']),
            sos_targets(['a1.txt', 'a3.txt', 'a4.txt']),
            sos_targets(['a2.txt', 'a3.txt', 'a4.txt'])
        ], f'obtained {env.sos_dict["executed"]}')
        # group_by chunks specified as integers
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 10)], group_by=3

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a1.txt', 'a2.txt', 'a3.txt'),
            sos_targets('a4.txt', 'a5.txt', 'a6.txt'),
            sos_targets('a7.txt', 'a8.txt', 'a9.txt')
        ])
        # group_by chunks specified as integer strings
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 10)], group_by='3'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a1.txt', 'a2.txt', 'a3.txt'),
            sos_targets('a4.txt', 'a5.txt', 'a6.txt'),
            sos_targets('a7.txt', 'a8.txt', 'a9.txt')
        ])
        # number of files should be divisible by group_by
        self.touch(['a{}.txt'.format(x) for x in range(1, 10)])
        script = SoS_Script('''
[0]

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 10)], group_by=4

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode="dryrun")
        # incorrect value causes an exception
        script = SoS_Script('''
[0]

executed = []
input: ['a{}.txt'.format(x) for x in range(1, 10)], group_by='something'

executed.append(_input)

''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run, mode="dryrun")
        #
        # group by label
        file_target('c.txt').touch()
        script = SoS_Script('''
[A]
output: 'a.txt'
_output.touch()

[B]
input: for_each={'i': range(2)}
output: 'b.txt', 'b1.txt', group_by=1
_output.touch()

[0: shared='executed']
executed = []

input: 'c.txt', output_from(['A', 'B']), group_by='label'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('c.txt'),
            sos_targets('a.txt'),
            sos_targets('b.txt', 'b1.txt')
        ])
        #
        # group_by='pairlabel'
        file_target('c.txt').touch()
        script = SoS_Script('''
[A]
output: 'a1.txt', 'a2.txt'
_output.touch()

[B]
output: 'b1.txt', 'b2.txt'
_output.touch()

[0: shared='executed']
executed = []

input: output_from(['A', 'B']), group_by='pairlabel'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(
            env.sos_dict['executed'],
            [sos_targets('a1.txt', 'b1.txt'),
             sos_targets('a2.txt', 'b2.txt')])
        # group_by='pairlabel3'
        self.touch(['c{}.txt'.format(x) for x in range(1, 7)])

        script = SoS_Script('''
[A]
output: [f'a{x}.txt' for x in range(1, 7)]
_output.touch()

[B]
output: [f'b{x}.txt' for x in range(1, 7)]
_output.touch()

[0: shared='executed']
executed = []

input: [f'c{x}.txt' for x in range(1, 7)], output_from(['A', 'B']), group_by='pairlabel3'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('c1.txt', 'c2.txt', 'c3.txt', 'a1.txt', 'a2.txt',
                        'a3.txt', 'b1.txt', 'b2.txt', 'b3.txt'),
            sos_targets('c4.txt', 'c5.txt', 'c6.txt', 'a4.txt', 'a5.txt',
                        'a6.txt', 'b4.txt', 'b5.txt', 'b6.txt')
        ])
        # group_by='pairlabel3'
        self.touch(['c{}.txt'.format(x) for x in range(1, 7)])

        script = SoS_Script('''
[A]
output: 'a1.txt'
_output.touch()

[B]
output: [f'b{x}.txt' for x in range(1, 7)]
_output.touch()

[0: shared='executed']
executed = []

input: [f'c{x}.txt' for x in range(1, 3)], output_from(['A', 'B']), group_by='pairlabel3'

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('c1.txt', 'a1.txt', 'b1.txt', 'b2.txt', 'b3.txt'),
            sos_targets('c2.txt', 'a1.txt', 'b4.txt', 'b5.txt', 'b6.txt')
        ])
        #
        # group by function
        file_target('c.txt').touch()
        script = SoS_Script('''
[0: shared='executed']
executed = []

def grp(x):
    return  [x[:3], x[3:]]

input: ['a{}.txt'.format(x) for x in range(5)], group_by=grp

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'],
                         [['a0.txt', 'a1.txt', 'a2.txt'], ['a3.txt', 'a4.txt']])
        #
        # group by lambda function
        file_target('c.txt').touch()
        script = SoS_Script('''
[0: shared='executed']
executed = []

input: ['a{}.txt'.format(x) for x in range(6)], group_by=lambda x: zip(x[:3], x[3:])

executed.append(_input)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(
            env.sos_dict['executed'],
            [['a0.txt', 'a3.txt'], ['a1.txt', 'a4.txt'], ['a2.txt', 'a5.txt']])

    def test_output_group_by(self):
        '''Test group_by parameter of step output'''
        # group_by = 'all'
        self.touch(['a{}.txt'.format(x) for x in range(4)])
        #
        script = SoS_Script('''
[0: shared='executed']

executed = []
input: ['a{}.txt'.format(x) for x in range(4)], group_by=2
output: ['a{}.txt.bak'.format(x) for x in range(4)], group_by=2

executed.append(_output)

''')
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'], [
            sos_targets('a0.txt.bak', 'a1.txt.bak'),
            sos_targets('a2.txt.bak', 'a3.txt.bak')
        ])

    def test_steps_with_step_name(self):
        '''Test from steps'''
        script = SoS_Script('''
[step_10]

output: 'a.txt'
_output.touch()

[step_20]
input: output_from(step_name.split('_')[0] + '_10')
print(_input)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        #
        script = SoS_Script('''
[step_10]

output: 'a.txt'
_output.touch()

[step_20]
input: output_from(10)
print(_input)
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_section_actions(self):
        '''Test actions of sections'''
        SoS_Script("""
[0]
func('''
multiline
string''', with_option=1
)
""")
        self.assertRaises(ParsingError, SoS_Script, '''
[0]
func(
''')

    def test_longer_code(self):
        '''Test definition of classes (with intermediate newlines) in step.'''
        script = SoS_Script('''# first block

[0: shared='b']
class A:
    def __init__(self):
        pass

    # the newline above should be fine because SoS treat this as
    # regular lines
    def __call__(self):
        return 0

b = A()()

''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['b'], 0)

    def test_combined_workflow(self):
        '''Test the creation and execution of combined workfow'''
        script = SoS_Script('''
a0 = 0
if 'executed' not in locals():
    executed = []
parameter: a = a0 + 1
[a_1: shared='executed']
executed.append(step_name)
[a_2: shared='executed']
executed.append(step_name)
[a_3: shared='executed']
executed.append(step_name)
[a_4: shared='executed']
executed.append(step_name)
output: 'out_a_4'
[b_1: shared=['executed', 'input_b1']]
executed.append(step_name)
input_b1 = _input
[b_2: shared='executed']
executed.append(step_name)
[b_3: shared='executed']
executed.append(step_name)
[b_4: shared='executed']
executed.append(step_name)
[c: shared='executed']
executed.append(step_name)
[d: shared='executed']
executed.append(step_name)
''')
        wf = script.workflow('a+b')
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(
            env.sos_dict['executed'],
            ['a_1', 'a_2', 'a_3', 'a_4', 'b_1', 'b_2', 'b_3', 'b_4'])
        self.assertEqual(env.sos_dict['a'], 1)
        self.assertEqual(env.sos_dict['input_b1'], ['out_a_4'])
        #
        env.sos_dict.pop('executed', None)
        wf = script.workflow('a: 1-2 + a:4 + b:3-')
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'],
                         ['a_1', 'a_2', 'a_4', 'b_3', 'b_4'])
        #
        env.sos_dict.pop('executed', None)
        wf = script.workflow('a+c+d')
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['executed'],
                         ['a_1', 'a_2', 'a_3', 'a_4', 'c', 'd'])

    def test_yaml_config(self):
        '''Test config file in yaml format'''
        with open('myconfig.yml', 'w') as config:
            config.write('''
# Lines beginning with # are skipped when the JSON is parsed, so we can
# put comments into our JSON configuration files
{
    StoreOwner : "John Doe",

    # List of items that we sell
    Fruits: [ "apple", "banana", "pear" ],
    Price: 1.05
}
''')
        with open('config.sos', 'w') as sos:
            sos.write('''
[0]
print(CONFIG['StoreOwner'])
print(CONFIG.get('StoreOwner', 'something'))
print(CONFIG.get('StoreOwnerSpouse', 'someone else'))
#print(CONFIG.StoreOwner)
''')
        # run the command
        self.assertEqual(
            subprocess.call(
                'sos run config.sos -c myconfig.yml',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        # now test the value
        script = SoS_Script(filename='config.sos')
        wf = script.workflow()
        Base_Executor(wf, config={'config_file': 'myconfig.yml'}).run()
        self.assertEqual(env.sos_dict['CONFIG']['Price'], 1.05)
        self.assertEqual(env.sos_dict['CONFIG']['StoreOwner'], 'John Doe')
        self.assertEqual(env.sos_dict['CONFIG']['Fruits'],
                         ['apple', 'banana', 'pear'])

    def test_var_output(self):
        '''Test early appearance of variable output'''
        script = SoS_Script('''
[0]
seq = range(3)
input: for_each='seq'
output: f"test{_seq}.txt"
print(_output)
''')
        wf = script.workflow()
        # this does not work before until we make variable output available sooner
        Base_Executor(wf).run(mode='dryrun')

    def test_cell(self):
        '''Test ignoring %cell'''
        SoS_Script('''
%cell 1
[step ]
a = 1
''')

    def test_overwrite_keyword(self):
        '''Test overwrite sos keyword with user defined one.'''
        if file_target('a.txt').exists():
            file_target('a.txt').unlink()
        #
        script = SoS_Script('''
def run(script):
    pass

[1]
run:
    touch a.txt
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertFalse(os.path.isfile('a.txt'))
        #
        script = SoS_Script('''
parameter: run = 5

[1]
run:
    touch a.txt
''')
        wf = script.workflow()
        # this is ok, see https://github.com/vatlab/SoS/issues/1221
        Base_Executor(wf).run()

    def test_comments(self):
        '''Test the use of comments in sos script'''
        # extract workflow from ipynb
        wf = extract_workflow('sample_workflow.ipynb')
        self.assertFalse('this is a test workflow' in wf)
        self.assertEqual(
            wf.count('this comment will be included but not shown in help'), 1)
        self.assertTrue(
            wf.count('this comment will become the comment for parameter b'), 1)
        self.assertTrue(
            wf.count('this comment will become the comment for parameter d'), 1)
        self.assertFalse('this is a cell with another kernel' in wf)
        self.assertFalse(
            'this comment will not be included in exported workflow' in wf)

    def test_help_message(self):
        '''Test help message from ipynb notebook'''
        msg = subprocess.check_output(
            'sos run sample_workflow.ipynb -h', shell=True).decode()
        self.assertFalse(
            'this comment will be included but not shown in help' in msg)
        self.assertTrue(
            msg.count('this comment will become the comment for parameter b'),
            1)
        self.assertTrue(
            msg.count('this comment will become the comment for parameter d'),
            1)
        self.assertTrue(msg.count('--c 3 (as int)'), 1)
        self.assertTrue(
            msg.count('this is a section comment, will be displayed'), 1)
        self.assertFalse('this is a test workflow' in msg)
        self.assertFalse('this is a cell with another kernel' in msg)
        self.assertFalse(
            'this comment will not be included in exported workflow' in msg)

    def test_help_on_multi_workflow(self):
        '''Test help message of sos file (#985)'''
        with open('test_msg.sos', 'w') as script:
            script.write('''\
[workflow_a_10,workflow_b]
[workflow_a_20]
[default]
''')
        msg = subprocess.check_output(
            'sos run test_msg.sos -h', shell=True).decode()
        self.assertTrue('workflow_a' in msg)
        self.assertTrue('workflow_b' in msg)
        # when introducing sections
        self.assertTrue('workflow_a_10, workflow_b' in msg)
        self.assertTrue('default' in msg)

    def test_parameter_abbreviation(self):
        '''Test potential problem caused by parameter abbreviation #1053'''
        if os.path.isfile('0914.txt'):
            os.remove('0914.txt')
        script = SoS_Script('''
[global]
parameter: name = '0914'

[1]
parameter: n = 4
output: f'{name}.txt'
print(_output)
_output.touch()
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--n', '5']).run()
        self.assertTrue(os.path.isfile('0914.txt'))

    def test_named_input(self):
        '''Test named input'''
        for filename in ('a.txt', 'b.txt'):
            with open(filename, 'w') as out:
                out.write(filename + '\n')
        script = SoS_Script('''
[1]
input: {'A': 'a.txt', 'B': 'b.txt'}, group_by='pairlabel'
output: 'c.txt'
assert _input['A'] == [file_target('a.txt')]
assert _input['B'] == [file_target('b.txt')]
with open(_output, 'w') as out:
    out.write(open(_input['A']).read())
    out.write(open(_input['B']).read())
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertTrue(open('c.txt').read(), 'a.txt\nb.txt\n')

    def test_named_output_in_depends(self):
        '''Test named_output in depends statement'''
        script = SoS_Script('''
[A]
output: A='a.txt'
_output.touch()

[default]
depends: named_output('A')
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_output_from_in_depends(self):
        '''Test output_from in depends statement'''
        script = SoS_Script('''
[A]
output: A='a.txt'
_output.touch()

[default]
depends: output_from('A')
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_named_output_in_output(self):
        '''Test named_output in output statement'''
        script = SoS_Script('''
[A]
output: Aa='a.txt'
_output.touch()

[default]
output: named_output('Aa')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)

    def test_output_from_in_output(self):
        '''Test output_from in output statement'''
        script = SoS_Script('''
[Aa]
output: A='a.txt'
_output.touch()

[default]
output: output_from('Aa')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)

    def test_sos_step_in_input(self):
        '''Test sos_step in input statement'''
        script = SoS_Script('''
[A]
output: A='a.txt'
_output.touch()

[default]
input: sos_step('A')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)

    def test_sos_variable_in_input(self):
        '''Test sos_variable in input statement'''
        script = SoS_Script('''
[A]
a=1

[default]
input: sos_variable('a')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)

    def test_sos_variable_in_output(self):
        '''Test sos_variable in output statement'''
        script = SoS_Script('''
[A]
a = 1

[default]
output: sos_variable('a')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)

    def test_sos_variable_with_keywordargument(self):
        '''Test output_from in output statement'''
        script = SoS_Script('''
[A]
a = 1

[default]
depends: sos_variable(var='a')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)

    def test_wide_card_step_name(self):
        '''test resolving step name with *'''
        script = SoS_Script('''
[A_1]

[*_2]
assert step_name == 'A_2', f'step_name is {step_name}, A_2 expected'
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_outfrom_prev_step(self):
        '''Test output_from(-1) from output_from '''
        script = SoS_Script('''
[A_1]
output: 'A_1.txt'
_output.touch()

[A_2]
input: output_from(-1)

[default]
depends: sos_step('A_2')

''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_step_from_numeric_step(self):
        '''Test sos_step(index) #1209'''
        script = SoS_Script('''
[1]

[2]
depends: sos_step('1')
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        #
        script = SoS_Script('''
[1]

[2]
depends: sos_step(1)

''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_depends_on_step_with_unspecified_input(self):
        for file in ('A_1.txt', 'A_2.txt', 'A_3.txt', 'A_4.txt'):
            if os.path.isfile(file):
                os.remove(file)
        #
        script = SoS_Script('''
[A_1]
output: f'{step_name}.txt'
_output.touch()

[A_2]
output: f'{step_name}.txt'
_output.touch()

[A_3]
output: f'{step_name}.txt'
_output.touch()

[A_4]
output: f'{step_name}.txt'
_output.touch()

[default]
depends: sos_step('A_2')
''')
        wf = script.workflow()
        # this should execute A_2 and A_1
        Base_Executor(wf).run()
        self.assertTrue(os.path.isfile('A_1.txt'))
        self.assertTrue(os.path.isfile('A_2.txt'))
        self.assertFalse(os.path.isfile('A_3.txt'))
        self.assertFalse(os.path.isfile('A_4.txt'))
        #
        for file in ('A_1.txt', 'A_2.txt', 'A_3.txt', 'A_4.txt'):
            if os.path.isfile(file):
                os.remove(file)
        #
        script = SoS_Script('''
[A_1]
output: f'{step_name}.txt'
_output.touch()

[A_2]
output: f'{step_name}.txt'
_output.touch()

[A_3]
output: f'{step_name}.txt'
_output.touch()

[A_4]
output: f'{step_name}.txt'
_output.touch()

[default]
input: output_from('A_2')
''')
        wf = script.workflow()
        # this should execute A_2 and A_1
        Base_Executor(wf).run()
        self.assertTrue(os.path.isfile('A_1.txt'))
        self.assertTrue(os.path.isfile('A_2.txt'))
        self.assertFalse(os.path.isfile('A_3.txt'))
        self.assertFalse(os.path.isfile('A_4.txt'))
        #
        for file in ('A_1.txt', 'A_2.txt', 'A_3.txt', 'A_4.txt'):
            if os.path.isfile(file):
                os.remove(file)
        #
        script = SoS_Script('''
[A_1]
output: f'{step_name}.txt'
_output.touch()

[A_2]
input: None
output: f'{step_name}.txt'
_output.touch()

[A_3]
output: f'{step_name}.txt'
_output.touch()

[A_4]
output: f'{step_name}.txt'
_output.touch()

[default]
input: output_from('A_2')
''')
        wf = script.workflow()
        # this should execute A_2 and A_1
        Base_Executor(wf).run()
        self.assertFalse(os.path.isfile('A_1.txt'))
        self.assertTrue(os.path.isfile('A_2.txt'))
        self.assertFalse(os.path.isfile('A_3.txt'))
        self.assertFalse(os.path.isfile('A_4.txt'))

    def test_output_from_workflow(self):
        '''Test output from workflow'''
        #
        #
        for file in ('A_1.txt', 'A_2.txt'):
            if os.path.isfile(file):
                os.remove(file)
        #
        script = SoS_Script(r'''
[A_1]
output: f'{step_name}.txt'

with open(_output, 'w') as out:
    out.write(step_name)
    out.write('\n')

[A_2]
output: f'{step_name}.txt'

with open(_output, 'w') as out:
    out.write(step_name)
    out.write('\n')

[default]
input: output_from('A')

with open(_input) as content:
    assert content.read() == 'A_2\n'

''')
        wf = script.workflow()
        # this should execute A_2 and A_1
        Base_Executor(wf).run()
        #
        self.assertTrue(os.path.isfile('A_1.txt'))
        self.assertTrue(os.path.isfile('A_2.txt'))

    def test_execute_global_section(self):
        '''Global section should be executed only once #1219'''
        script = SoS_Script(r'''
[global]
parameter: A=[1, 2]
parameter: B=[]
B.extend(A)

[default]
assert B == [1, 2]
input: for_each=dict(i=range(2))
assert B == [1, 2]
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_para_from_nested_workflow(self):
        '''Test passing arguments to nested workflow #1229'''
        script = SoS_Script(r'''
[A]
parameter: param = str
print(param)

[B]
print(step_name)

[default]
sos_run('B+A')
''')
        wf = script.workflow()
        Base_Executor(wf, args=['--param', '2']).run()

    def test_indented_action(self):
        '''Test the use of indented action in script format'''
        #
        # not indented
        script = SoS_Script(r'''
[1]
if True:
    sh:
echo something

[2]
for i in range(2):
    sh:
echo something
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # not indented double action
        script = SoS_Script(r'''
[1]
if True:
    sh:
echo something

    python:
print(1)
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)
        # not indented Python structure
        script = SoS_Script(r'''
[1]
if True:
    sh:
echo something
else:
    python:
print(1)
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)
        #  indented
        script = SoS_Script(r'''
[1]
if True:
    sh:
        echo something

[2]
for i in range(2):
    sh:
        echo something
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # indented double action
        script = SoS_Script(r'''
[1]
if True:
    sh:
        echo something
    python:
        print(1)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # indented Python structure
        script = SoS_Script(r'''
[1]
if True:
    sh:
        echo something
else:
    python:
        print(1)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # indented, nested structure
        script = SoS_Script(r'''
[1]
for i in range(2):
    if True:
        sh:
            echo something
    else:
        python:
            print(1)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        #
        # wrong nested action
        script = SoS_Script(r'''
report:
    name: 'a.txt'
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_task_param_var(self):
        '''Test global parameter passed to task parameters #1281'''
        script = SoS_Script(r'''
[global]
parameter: job_size = 60

[1]
task: trunk_size = job_size
bash:
        echo 1
''')
        wf = script.workflow()
        Base_Executor(wf, config={'default_queue': 'localhost'}).run()

    def test_task_param_var_to_substep(self):
        '''Test global parameter passed to task parameters in substep #1281'''
        script = SoS_Script(r'''
[global]
parameter: job_size = 60

[1]
input: for_each=dict(i=range(3))

task: trunk_size = job_size
bash: expand=True
        echo {i}
''')
        wf = script.workflow()
        Base_Executor(wf, config={'default_queue': 'localhost'}).run()

    def test_empty_parameter(self):
        # parameter: without content #1283
        script = SoS_Script(r'''
parameter:

print(1)
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_sequential_substeps(self):
        '''Test sequential execution of substeps'''
        script = SoS_Script(r'''
[10: shared='sum']
sum = 0
input: for_each=dict(i=range(4)), concurrent=False
sum += i
print(f'sum is {sum} at index {_index}')
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        self.assertEqual(env.sos_dict['sum'], 6)

    def test_limited_concurrency(self):
        '''Set concurrent=INT'''

        script = SoS_Script(r'''
[10]
input: for_each=dict(i=range(6)), concurrent=2
print(i)
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def test_concurrent_substep_with_step_import(self):
        ''' Test concurrent substep with step leval import statement #1354'''
        execute_workflow('''
import time
input: for_each=dict(i=range(2))
time.sleep(0)
        ''')


def test_type_hint():
    '''We should be able to differentiate type hint and sos action
    '''
    SoS_Script('''a : list = 5''')
    SoS_Script('''a : list''')
    # action
    SoS_Script('''a : input='filename' ''')
    # action
    SoS_Script('''a : expand='${ }' ''')


def test_sos_step_in_output():
    '''Test sos_step in output statement'''
    with pytest.raises(Exception):
        execute_workflow('''
            [A]
            output: A='a.txt'
            _output.touch()

            [default]
            output: sos_step('A')
        ''')
