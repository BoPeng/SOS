#!/usr/bin/env python
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import signal
import sys
import threading
import unittest
from contextlib import contextmanager

from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env
# if the test is imported under sos/test, test interacive executor
from sos.workflow_executor import Base_Executor

try:
    import _thread
except Exception:
    import _dummy_thread as _thread

try:
    from sos.docker.client import SoS_DockerClient
    has_docker = True
except ImportError:
    print('Docker is not available')
    has_docker = False


class TimeoutException(Exception):

    def __init__(self, msg=''):
        self.msg = msg


@contextmanager
def time_limit(seconds, msg=''):
    if sys.platform == 'win32':
        # windows system does not have signal SIGALARM so we will
        # have to use a timer approach, which does not work as well
        # as the signal approach
        def timeout_func():
            #env.logger.error('Timed out for operation {}'.format(msg))
            _thread.interrupt_main()

        timer = threading.Timer(seconds, timeout_func)
        timer.start()
        try:
            yield
        except KeyboardInterrupt:
            # important: KeyboardInterrupt does not interrupt time.sleep()
            # because KeyboardInterrupt is handled by Python interpreter but
            # time.sleep() calls a system function.
            raise TimeoutException("Timed out for operation {}".format(msg))
        finally:
            # if the action ends in specified time, timer is canceled
            timer.cancel()
    else:

        def signal_handler(signum, frame):
            raise TimeoutException("Timed out for option {}".format(msg))

        signal.signal(signal.SIGALRM, signal_handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)


try:
    with time_limit(2, 'check docker daemon'):
        has_docker = SoS_DockerClient().client is not None
except Exception as e:
    print(
        'Cannot connect to a docker daemon in 2 seconds. Assuming no docker environment.'
    )
    print(e)
    has_docker = False


class TestDockerActions(unittest.TestCase):

    def setUp(self):
        self.olddir = os.getcwd()
        try:
            # this only works with nose, but is also
            # only needed by nose
            os.chdir(os.path.dirname(__file__))
        except Exception:
            pass
        env.reset()
        self.temp_files = []

    def tearDown(self):
        for f in self.temp_files:
            file_target(f).unlink()
        os.chdir(self.olddir)

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

    @unittest.skipIf(not has_docker or sys.platform == 'win32',
                     'Skip test because docker is not installed.')
    def test_bash_in_docker(self):
        '''Test action bash in docker environment'''
        script = SoS_Script(r'''
[0]
run:  container='docker://ubuntu'
echo 'Echo'
''')
        wf = script.workflow()
        Base_Executor(wf).run()


#    @unittest.skipIf(not has_docker or sys.platform != 'win32', 'Skip test because docker is not installed.')
#    def testBatchScriptInDocker(self):
#        '''Test action powershell in docker environment'''
#        script = SoS_Script(r'''
#[0]
#run:  container='docker://microsoft/windowsservercore'
#dir c:\
#''')
#        wf = script.workflow()
#        Base_Executor(wf).run()

    @unittest.skipIf(not has_docker or sys.platform == 'win32',
                     'Skip test because docker is not installed.')
    def test_sh_in_docker(self):
        '''Test action sh in docker environment'''
        # test docker
        script = SoS_Script(r'''
[0]
run: container='docker://ubuntu'
echo 'Echo
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)
        #
        Base_Executor(wf).run(mode='dryrun')

    @unittest.skipIf(not has_docker or sys.platform == 'win32',
                     'Skip test because docker is not installed.')
    def test_docker_build_linux_image(self):
        '''Test action docker build'''
        script = SoS_Script(r'''
[0]
docker_build:  tag='test/docker_build'
#
# Super simple example of a Dockerfile
#
FROM ubuntu:latest
MAINTAINER Andrew Odewahn "odewahn@oreilly.com"

RUN apt-get update

WORKDIR /home
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # build with more options
        script = SoS_Script(r'''
[0]
docker_build:  tag='test/docker_build1', label='label with space', compress=True, memory='2G'
#
# Super simple example of a Dockerfile
#
FROM ubuntu:latest
MAINTAINER Andrew Odewahn "odewahn@oreilly.com"

WORKDIR /home
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(not has_docker or sys.platform != 'win32' or
                     'APPVEYOR' in os.environ,
                     'Skip test because docker is not installed.')
    def test_docker_build_windows_image(self):
        '''Test action docker build'''
        script = SoS_Script(r'''
[0]
docker_build:  tag='test/docker_build'
# Indicates that the windowsservercore image will be used as the base image.
FROM microsoft/windowsservercore

# Metadata indicating an image maintainer.
MAINTAINER someone@microsoft.com

''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(not has_docker or sys.platform == 'win32',
                     'Skip test because docker is not installed.')
    def test_docker_image(self):
        '''Test docker_image option'''
        script = SoS_Script(r'''
import os
import glob
[0]
fastq_files = glob.glob('data/*.fastq')
input_volume = os.path.dirname(fastq_files[0])
output_volume = os.getcwd()

run: container='docker://compbio/ngseasy-fastqc:1.0-r001',
    volumes=[f"{input_volume}:/input_data", f"{output_volume}:/output_data"]

    ls -l /input_data
    /usr/local/bin/fastqc /input_data/*.fastq --outdir /output_data
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(
        not has_docker or sys.platform == 'win32',
        'Skip test because docker is not installed, or in travis, which failed for unknown reason'
    )
    def test_docker_image_from_file(self):
        '''Test docker_image load from a file.'''
        # image from a saved file
        script = SoS_Script(r'''
[0]
run:   container='docker://blang/busybox-bash'

[1]
run:
    docker save blang/busybox-bash > hello.tar
    docker rmi -f blang/busybox-bash

[2]
run: container='docker://blang/busybox-bash', docker_file = 'hello.tar'

    echo "a"
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(not has_docker or sys.platform == 'win32',
                     'Skip test because docker is not installed.')
    def test_docker_script_action(self):
        '''Test action sh in docker environment'''
        # test docker
        script = SoS_Script(r'''
[0]
script: container='docker://ubuntu', args='{script}'
echo 'Echo'
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(not has_docker or sys.platform == 'win32',
                     'Skip test because docker is not installed.')
    def test_port_option(self):
        '''Test use of option port in action'''
        script = SoS_Script(r'''
[0]
run:  container='ubuntu', port=True
echo 'Echo'
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        #
        script = SoS_Script(r'''
[0]
run:  container='ubuntu', port=2345
echo 'Echo'
''')
        wf = script.workflow()
        Base_Executor(wf).run()

if __name__ == '__main__':
    unittest.main()
