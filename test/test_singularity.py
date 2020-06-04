#!/usr/bin/env python
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import sys
import shutil
import unittest

from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env
from sos.workflow_executor import Base_Executor


class TestSingularityActions(unittest.TestCase):

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

    @unittest.skipIf(not shutil.which('singularity'),
                     'Skip test because docker is not installed.')
    def test_bash_in_singularity(self):
        '''Test action bash in singularity environment'''
        script = SoS_Script(r'''
[0]
#run:  container='shub://singularityhub/ubuntu'
run:  container='docker://ubuntu'
echo 'Echo'
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(not shutil.which('singularity') or
                     sys.platform == 'win32' or 'TRAVIS' in os.environ or
                     'APPVEYOR' in os.environ,
                     'Skip test because docker is not installed.')
    def test_singularity_build_linux_image(self):
        '''Test action singularity build, needs authentication so no travis test'''
        script = SoS_Script(r'''
singularity_build: dest='lolcow.simg', sudo=True, notest=True
Bootstrap: docker
From: ubuntu:16.04
%post
    apt-get -y update
    apt-get -y install fortune cowsay lolcat
%environment
    export LC_ALL=C
    export PATH=/usr/games:$PATH
%runscript
    fortune | cowsay | lolcat
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(not shutil.which('singularity') or
                     sys.platform == 'win32' or 'TRAVIS' in os.environ or
                     'APPVEYOR' in os.environ,
                     'Skip test because docker is not installed.')
    def test_singularity_build_from_shub(self):
        # needs authentication and cannot test on travis
        script = SoS_Script(r'''
singularity_build(src='shub://GodloveD/lolcow', dest='lolcow_shub.simg', sudo=True, notest=True, force=True)
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(not shutil.which('singularity') or sys.platform == 'win32',
                     'Skip test because docker is not installed.')
    def test_singularity_build_from_docker(self):
        script = SoS_Script(r'''
singularity_build(src='docker://godlovedc/lolcow', dest='lolcow_docker.simg', sudo=True, notest=True, force=True)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        # test handling simg with singularity
        script = SoS_Script(r'''
run: container='lolcow_docker.simg'
  ls /
''')
        wf = script.workflow()
        Base_Executor(wf).run()


if __name__ == '__main__':
    unittest.main()
