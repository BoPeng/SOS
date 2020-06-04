#!/usr/bin/env python
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import glob
import os
import shutil
import socket
import sys
import time
import unittest
import pytest

from sos import execute_workflow

from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env
from sos.workflow_executor import Base_Executor
from sos import execute_workflow


def internet_on(host='8.8.8.8', port=80, timeout=3):
    '''Test if internet is connected '''
    try:
        socket.setdefaulttimeout(timeout)
        socket.create_connection(("www.google.com", 80))
        return True
    except Exception as e:
        print(e)
        return False


with_network = internet_on()


def testAcceptableArgs():
    '''test acceptable args of options'''
    with pytest.raises(Exception):
        execute_workflow(r"""
            run: unrecog=1
            echo 'a'
            """)


def testGetOutput():
    '''Test utility function get_output'''
    execute_workflow(r"""
        [0: shared='ret']
        ret = get_output('echo blah')
        """)
    # use strip because there would be \r\n under windows
    assert env.sos_dict['ret'].strip() == 'blah'

    execute_workflow(r"""
        [0: shared='ret']
        ret = get_output('echo blah', show_command=True)
        """)
    assert [x.strip() for x in env.sos_dict['ret'].splitlines()
           ] == ['$ echo blah', 'blah']

    execute_workflow(r"""
        [0: shared='ret']
        ret = get_output('echo blah', show_command=True, prompt='% ')
        """)
    assert [x.strip() for x in env.sos_dict['ret'].splitlines()
           ] == ['% echo blah', 'blah']

    with pytest.raises(Exception):
        execute_workflow(r"""
            [0]
            get_output('catmouse')
        """)

    with pytest.raises(Exception):
        execute_workflow(r"""
            [0]
            ret = get_output('cat -h')
            """)


def testFailIf(temp_factory):
    '''Test action fail if'''
    temp_factory('a.txt')

    # should fail in dryrun mode
    with pytest.raises(Exception):
        execute_workflow(r"""
            [0]
            input: 'a.txt'
            fail_if(len(input) == 1)
            """)

    with pytest.raises(Exception):
        execute_workflow(r"""
            [0]
            input: 'a.txt', 'b.txt'
            fail_if(len(input) == 2)
        """)


def testDelayedFailIf():
    # test fail_if of killing another running substep
    st = time.time()
    with pytest.raises(Exception):
        execute_workflow(
            r"""
            import time

            [10]
            time.sleep(8)

            [20]
            input: None
            time.sleep(2)
            fail_if(True)
            """,
            config={'worker_procs': ['3']})

    assert time.time(
    ) - st >= 8, 'Test test should fail only after step 10 is completed'


def testDelayedFailIfFromNestedWorkflow():
    # test fail_if of killing another running substep

    st = time.time()
    with pytest.raises(Exception):
        execute_workflow(
            r"""
            import time

            [default]
            sos_run('a')

            [a_10]
            time.sleep(8)

            [a_20]
            input: None
            time.sleep(2)
            fail_if(True)
        """,
            config={'worker_procs': ['3']})

    assert time.time(
    ) - st >= 8, 'Test test should fail only after step 10 is completed'


def testWarnIf(temp_factory):
    '''Test action fail if'''
    execute_workflow(
        r"""
        [0]
        warn_if(input is None, 'Expect to see a warning message')
        """,
        options={'run_mode': 'dryrun'})

    temp_factory('a.txt', 'b.txt')
    execute_workflow(
        r"""
        [0]
        input: 'a.txt', 'b.txt'
        warn_if(len(_input) == 1)
        """,
        options={'run_mode': 'dryrun'})


def testStopIf():
    '''Test action stop_if'''
    execute_workflow(r'''
        [0: shared='result']
        rep = range(20)
        result = []
        input: for_each='rep'

        stop_if(_rep > 10)
        result.append(_rep)
        ''')
    assert env.sos_dict['result'] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_stop_if_1(clear_now_and_after):
    # stop_if should not be treated as error so the previously
    # generated output file will be removed
    clear_now_and_after('test_stop_if_0.txt', 'test_stop_if_1.txt')

    execute_workflow(r'''
        [10]
        rep = range(2)
        input: for_each='rep'
        output: f'test_stop_if_{_rep}.txt'

        _output.touch()
        stop_if(_rep == 1, no_output=True)

        [20]
        assert(step_input.contains('test_stop_if_0.txt'))
        assert(not step_input.contains('test_stop_if_1.txt'))
        ''')

    assert os.path.isfile('test_stop_if_0.txt')
    assert not (os.path.isfile('test_stop_if_1.txt'))


def test_stop_if_2(clear_now_and_after):
    # stop_if should not be treated as error so the previously
    # generated output file will not be removed
    clear_now_and_after('test_stop_if_0.txt', 'test_stop_if_1.txt')

    execute_workflow(r'''
        [10]
        rep = range(2)
        input: for_each='rep'
        output: f'test_stop_if_{_rep}.txt'

        _output.touch()
        stop_if(_rep == 1)

        [20]
        assert(step_input.contains('test_stop_if_0.txt'))
        assert(step_input.contains('test_stop_if_1.txt'))
        ''')
    assert os.path.isfile('test_stop_if_0.txt')
    assert os.path.isfile('test_stop_if_1.txt')


def testSkipIf():
    '''Test action stop_if'''
    execute_workflow(r'''
        [0: shared='result']
        rep = range(20)
        result = []
        input: for_each='rep'

        skip_if(_rep > 10)
        result.append(_rep)
        ''')
    assert env.sos_dict['result'] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_skip_if_1(clear_now_and_after):
    clear_now_and_after([f'test_stop_if_{rep}.txt' for rep in range(2)])

    # stop_if should not be treated as error so the previously
    # generated output file will be removed

    execute_workflow(r'''
        [10]
        rep = range(2)
        input: for_each='rep'
        output: f'test_stop_if_{_rep}.txt'

        _output.touch()
        skip_if(_rep == 1)

        [20]
        assert(step_input.contains('test_stop_if_0.txt'))
        assert(not step_input.contains('test_stop_if_1.txt'))

        ''')

    assert os.path.isfile('test_stop_if_0.txt')
    assert not os.path.isfile('test_stop_if_1.txt')


def testDoneIf(clear_now_and_after):
    'Test action done_if'
    clear_now_and_after([f'test_done_if_{rep}.txt' for rep in range(2)])

    execute_workflow(r'''
        [10]
        rep = range(2)
        input: for_each='rep'
        output: f'test_done_if_{_rep}.txt'

        _output.touch()
        done_if(_rep == 1)

        [20]
        assert(step_input.contains('test_done_if_0.txt'))
        assert(step_input.contains('test_done_if_1.txt'))

        ''')
    assert (os.path.isfile('test_done_if_0.txt'))
    assert (os.path.isfile('test_done_if_1.txt'))


def testRun():
    '''Test action run'''
    execute_workflow(r'''
        [0]
        run:
        echo 'Echo'
        ''')


@pytest.mark.skipif(
    sys.platform == 'win32', reason="Under windows, echo 'Echo is perfectly OK")
def test_run_1():

    with pytest.raises(Exception):
        execute_workflow(r'''
            [0]
            run:
            echo 'Echo
            ''')


def testRunWithShebang():
    execute_workflow(r'''
        [0]
        run:
        #!/usr/bin/env python
        print('Echo')
        ''')


@pytest.mark.skipif(not shutil.which('perl'), reason="Needs perl")
def testPerl():
    '''Test action perl'''
    execute_workflow(r'''
        [0]
        perl:
        use strict;
        use warnings;

        print "hi NAME\n";
        ''')


@pytest.mark.skipif(not shutil.which('ruby'), reason="Needs ruby")
def testRuby():
    '''Test action ruby'''
    execute_workflow(r'''
        [0]
        ruby:
        line1 = "Cats are smarter than dogs";
        line2 = "Dogs also like meat";

        if ( line1 =~ /Cats(.*)/ )
        puts "Line1 contains Cats"
        end
        if ( line2 =~ /Cats(.*)/ )
        puts "Line2 contains  Dogs"
        end
        ''')


@pytest.mark.skipif(
    not with_network or 'TRAVIS' in os.environ or 'APPVEYOR' in os.environ,
    reason='Skip test because of no internet connection or in travis test')
def testDownload(temp_factory, clear_now_and_after):
    '''Test download of resources'''

    clear_now_and_after('tmp')
    temp_factory(dir='tmp')

    # test decompress tar.gz file
    execute_workflow(r'''
        [0]
        download(['http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/CancerGeneCensus-20170912.DB.gz'],
        dest_dir='tmp', decompress=True)
        ''')
    assert (os.path.isfile('tmp/CancerGeneCensus-20170912.DB'))

    #
    # testing the download of single file
    #
    execute_workflow(r'''
        [0]
        download: dest_file='tmp/refgene.ppp'
        http://bioinformatics.mdanderson.org/Software/VariantTools/repository/resource/refgene.pkl
        ''')

    assert (os.path.isfile('tmp/refgene.ppp'))

    # test option dest_dir
    execute_workflow(r'''
        [0]
        download: dest_dir='tmp'
        http://bioinformatics.mdanderson.org/Software/VariantTools/repository/resource/refgene.pkl
        ''')
    assert (os.path.isfile('tmp/refgene.pkl'))


@pytest.mark.skipif(
    not with_network or 'TRAVIS' in os.environ or 'APPVEYOR' in os.environ,
    reason='Skip test because of no internet connection or in travis test')
def testDownloadMissingFile(temp_factory, clear_now_and_after):

    clear_now_and_after('tmp')
    temp_factory(dir='tmp')

    with pytest.raises(Exception):
        execute_workflow(r'''
            [0]
            download: dest_dir='tmp', decompress=True, max_jobs=2
            http://bioinformatics.mdanderson.org/Software/VariantTools/repository/resource/non-existing.gz
            http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/CancerGeneCensus-20170912.DB.gz
            http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/CancerGeneCensus.ann
            http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/DGV-hg38_20160831.ann
            ''')

    assert (os.path.isfile('tmp/CancerGeneCensus.ann'))


@pytest.mark.skipif(
    not with_network or 'TRAVIS' in os.environ or 'APPVEYOR' in os.environ,
    reason='Skip test because of no internet connection or in travis test')
def testDownloadLargeFile(temp_factory, clear_now_and_after):
    # test decompress tar.gz, .zip and .gz files

    clear_now_and_after('tmp')
    temp_factory(dir='tmp')

    execute_workflow(r'''
        [0]
        download: dest_dir='tmp', decompress=True
        http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/CancerGeneCensus-20170912.DB.gz
        http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/CancerGeneCensus.ann
        http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/DGV-hg38_20160831.ann
        ''')

    # run in build mode
    execute_workflow(r'''
        [0]
        download: dest_dir='tmp', decompress=True
        http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/CancerGeneCensus-20170912.DB.gz
        http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/CancerGeneCensus.ann
        http://bioinformatics.mdanderson.org/Software/VariantTools/repository/annoDB/DGV-hg38_20160831.ann
        ''')


@pytest.mark.skipif(not shutil.which('pandoc'), reason="Needs pandoc")
def testPandoc(clear_now_and_after):
    '''Test action pandoc'''
    clear_now_and_after('report.md', 'myreport.html')

    execute_workflow(r'''
        [10]

        report: output='report.md'
        ## Some random figure

        Generated by matplotlib


        [100]
        # generate report
        output: 'myreport.html'
        pandoc(input='report.md', output=_output[0])
        ''')

    assert (os.path.isfile('myreport.html'))


@pytest.mark.skipif(not shutil.which('pandoc'), reason="Needs pandoc")
def test_pandoc_1(clear_now_and_after):
    clear_now_and_after('a.md', 'myreport.html')

    execute_workflow(r'''
        [10]
        report: output='a.md'
        ## Some random figure

        Generated by matplotlib


        [100]
        # generate report
        output: 'myreport.html'
        pandoc(input='a.md', output=_output[0])
        ''')

    assert os.path.isfile('myreport.html')


@pytest.mark.skipif(not shutil.which('pandoc'), reason="Needs pandoc")
def test_pandoc_2(clear_now_and_after):
    clear_now_and_after('a.md', 'myreport.html')
    #
    # another case is no output
    execute_workflow(r'''
        [10]
        report: output='a.md'
        ## Some random figure

        Generated by matplotlib


        [100]
        # generate report
        pandoc(input='a.md')
        ''')


@pytest.mark.skipif(not shutil.which('pandoc'), reason="Needs pandoc")
def test_pandoc_2(clear_now_and_after):
    # test acceptance of a list of input filenames
    clear_now_and_after('default_10.md', 'default_20.md', 'output.html')

    execute_workflow(r'''
        [10]
        report: output='default_10.md'
        A_10

        [20]
        report: output='default_20.md'
        A_20

        [100]
        # generate report
        pandoc(input=['default_10.md', 'default_20.md'], output='output.html')
        ''')

    for f in ['default_10.md', 'default_20.md', 'output.html']:
        assert file_target(f).target_exists()


def testReport(clear_now_and_after):
    '''Test action report'''
    clear_now_and_after('report.txt')

    script = r'''
        [A]
        parameter: num=5
        report: output='report.txt', expand=True
        touch {num}.txt

        '''
    # run twice
    execute_workflow(script, args=['--num', '7'])
    execute_workflow(script, args=['--num', '5'])

    with open('report.txt') as report:
        assert report.read() == 'touch 5.txt\n\n'


def test_report_1(clear_now_and_after):
    '''Test action report'''
    clear_now_and_after('report.txt')

    execute_workflow(r'''
        [A]
        report: output='report.txt', expand=True
        {step_name}

        [A_10]
        report: output='report.txt', expand=True
        {step_name}
        ''')
    with open('report.txt') as report:
        assert report.read() == 'A_10\n\n'


def test_report_2(clear_now_and_after):
    '''Test action report'''
    clear_now_and_after('a.txt', 'out.txt')

    execute_workflow(r'''
        [A_1]
        run: output='a.txt'
            echo something > a.txt

        report(input='a.txt', output='out.txt')
        ''')

    for name in ('a.txt', 'out.txt'):
        with open(name) as report:
            assert report.read().strip() == 'something'


def test_report_3(clear_now_and_after):
    '''Test action report'''
    clear_now_and_after('report.txt')
    #
    execute_workflow(r'''
        [A_1]
        run: output='a.txt'
        echo something > a.txt

        [A_2]
        run: output='b.txt'
        echo something else > b.txt

        [A_3]
        report(input=['a.txt', 'b.txt'], output='out.txt')
        ''')
    for name in ('a.txt', 'b.txt', 'out.txt'):
        assert (file_target(name).target_exists())


def test_report_4(clear_now_and_after):
    '''Test action report'''
    clear_now_and_after('a.txt')

    # test report to other types of output: path
    execute_workflow(r'''
        [A_1]
        report: output=path('a.txt')
        something
        ''')


def test_report_5(clear_now_and_after):
    '''Test action report'''
    clear_now_and_after('a.txt')

    execute_workflow(r'''
        [A_1]
        output: 'a.txt'
        report: output=_output[0]
        something
        ''')


def test_report_6(clear_now_and_after):
    '''Test action report'''
    clear_now_and_after('a.txt')

    # test report to other types of output: sos_targets
    execute_workflow(r'''
        [A_1]
        output: 'a.txt'
        report: output=_output
        something
        ''')


def test_report_7(clear_now_and_after):
    '''Test action report'''
    clear_now_and_after('a.txt')

    with pytest.raises(Exception):
        execute_workflow(r'''
            [A_1]
            output: 'a.txt', 'b.txt'
            report: output=_output
            something
            ''')


def testOptionWorkdir(temp_factory):
    '''Test option workdir of tasks'''
    temp_factory(dir='temp_wdr')

    with open(os.path.join('temp_wdr', 'a.txt'), 'w') as tmp:
        tmp.write('hello')

    execute_workflow(r'''
        [A_1]
        run: workdir='temp_wdr'
        cp -f a.txt a2.txt
        ''')

    assert file_target(os.path.join('temp_wdr', 'a2.txt')).target_exists()
    with open(os.path.join('temp_wdr', 'a.txt')) as tmp:
        assert 'hello' == tmp.read()


def testActionScript():
    '''Test action script'''
    execute_workflow(r'''
        [A_1]
        script: interpreter='python'
        with open('something.txt', 'w') as tmp:
            tmp.write('something')
        ''')

    assert file_target('something.txt').target_exists()
    with open('something.txt') as tmp:
        assert 'something' == tmp.read()


def testRegenerateReport(clear_now_and_after):
    '''Testing the regeneration of report once is needed. The problem
    here is the 'input' parameter of report.'''
    clear_now_and_after('a1.md', 'a2.md', 'out.md')
    script = r'''
        [A_1]
        output: 'a1.txt', 'a1.md'
        run:
        echo 'a1' >> a1.txt

        report: output='a1.md'
        a1

        [A_2]
        output: 'a2.txt', 'a2.md'
        run:
        echo 'a2' >> a2.txt
        report: output='a2.md'
        a2

        [A_3]
        input: 'a1.md', 'a2.md'
        output: 'out.md'
        report:     input=['a1.md', 'a2.md'], output='out.md'
        '''
    execute_workflow(script)
    with open('a1.md') as a:
        assert a.read() == 'a1\n\n'
    with open('a2.md') as a:
        assert a.read() == 'a2\n\n'
    with open('out.md') as a:
        assert a.read() == 'a1\n\na2\n\n'

    clear_now_and_after('a1.md', 'a2.md', 'out.md')
    execute_workflow(script)


def testActiveActionOption(temp_factory):
    '''Test the active option of actions'''
    # disallow
    with pytest.raises(Exception):
        SoS_Script('''
[1]
rep = range(5)
input: for_each = 'rep'
# ff should change and be usable inside run
ff = f"{_rep}.txt"
run:  expand=True, active=1,2
echo {ff}
touch temp/{ff}
''')
    #
    for active, result in [
        ('0', ['temp/0.txt']),
        ('-1', ['temp/4.txt']),
        ('(1,2)', ['temp/1.txt', 'temp/2.txt']),
        ('[2,3]', ['temp/2.txt', 'temp/3.txt']),
        ('(0,2,4)', ['temp/0.txt', 'temp/2.txt', 'temp/4.txt']),
        ('slice(1,None)',
         ['temp/1.txt', 'temp/2.txt', 'temp/3.txt', 'temp/4.txt']),
        ('slice(1,-2)', ['temp/1.txt', 'temp/2.txt']),
        ('slice(None,None,2)', ['temp/0.txt', 'temp/2.txt', 'temp/4.txt']),
        ('True',
         ['temp/0.txt', 'temp/1.txt', 'temp/2.txt', 'temp/3.txt',
          'temp/4.txt']),
        ('False', []),
    ]:
        temp_factory(dir='temp')
        # test first iteration
        execute_workflow(
            ('''
            [1]
            rep = range(5)
            input: for_each = 'rep'
            # ff should change and be usable inside run
            ff = f"{_rep}.txt"
            run:  expand=True, active=%s
            echo {ff}
            touch temp/{ff}
            ''' % active).replace('/', os.sep),
            options={'sig_mode': 'force'})

        files = list(glob.glob(os.path.join('temp', '*.txt')))
        assert sorted(files) == sorted([x.replace('/', os.sep) for x in result])


def test_action_option_template(clear_now_and_after):
    clear_now_and_after('template_output.txt')

    execute_workflow('''
        run:  template='cat {filename}'
            echo 'whatever' > template_output.txt
        ''')
    assert not os.path.isfile('template_output.txt')


def test_action_option_template_name(config_factory):
    cfg = config_factory('''\
        action_templates:
            cat: |
                cat {filename}

        ''')
    execute_workflow(
        '''
        run:  template_name='cat'
            echo 'whatever' > template_output.txt
        ''',
        args=['-c', cfg])
    assert not os.path.isfile('template_output.txt')


if __name__ == '__main__':
    unittest.main()
