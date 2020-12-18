#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import copy
import os
import subprocess

from .eval import cfg_interpolate
from .utils import env, textMD5


class WorkflowEngine:
    def __init__(self, agent):
        self.agent = agent
        self.config = agent.config
        self.alias = self.config["alias"]

    def remove_arg(self, argv, arg):
        r_idx = [idx for idx, x in enumerate(argv) if x.startswith(arg)]
        if not r_idx:
            return argv
        else:
            r_idx = r_idx[0]
        # find next option
        r_next = [idx for idx, x in enumerate(argv[r_idx + 1 :]) if x.startswith("-")]
        if r_next:
            argv = argv[:r_idx] + argv[r_idx + 1 + r_next[0] :]
        else:
            argv = argv[:r_idx]
        return argv

    def expand_template(self):
        try:
            if self.local_filename.lower().endswith(".ipynb"):
                from .converter import extract_workflow

                script = extract_workflow(self.local_filename)
            else:
                with open(self.local_filename) as script_file:
                    script = script_file.read()
            self.job_name = textMD5(script)
            self.template_args["filename"] = self.filename
            self.template_args["script"] = script
            self.template_args["command"] = self.command
            self.template_args["job_name"] = self.job_name
            self.job_text = (
                cfg_interpolate(self.workflow_template, self.template_args) + "\n"
            )
        except Exception as e:
            raise ValueError(
                f"Failed to generate job file for the execution of workflow: {e}"
            )
        try:
            wf_dir = os.path.join(os.path.expanduser("~"), ".sos", "workflows")
            if not os.path.isdir(wf_dir):
                os.makedirs(wf_dir)

            self.job_file = os.path.join(wf_dir, self.job_name + ".sh")

            # do not translate newline under windows because the script will be executed
            # under linux/mac
            with open(self.job_file, "w", newline="") as job:
                job.write(self.job_text)
        except Exception as e:
            raise RuntimeError(
                f"Failed to submit workflow {self.command} with script \n{self.job_text}\n: {e}"
            )
        return True

    def execute_workflow(self, filename, command, **template_args):
        # there is no need to prepare workflow (e.g. copy over)
        if os.path.isfile(filename):
            ret = self.agent.send_to_host([filename])
        elif os.path.isfile(filename + ".sos"):
            ret = self.agent.send_to_host([filename + ".sos"])
        elif os.path.isfile(filename + ".ipynb"):
            ret = self.agent.send_to_host([filename + ".ipynb"])
        else:
            raise RuntimeError(f"Failed to locate script {filename}")

        # we send config files also to remote host, but change localhost
        import tempfile
        import yaml

        # process -c configfile
        with tempfile.NamedTemporaryFile(suffix='.yml', mode='w',
            dir=os.path.join(os.path.expanduser('~'), '.sos')) as temp:
            remote_cfg = copy.deepcopy(env.sos_dict['CONFIG'])
            remote_cfg['localhost'] = self.alias
            yaml.safe_dump(remote_cfg, temp)
            temp.flush()
            # copy the files over
            cfg_file = f'~/.sos/{os.path.basename(temp.name)}'
            self.agent.send_job_file(cfg_file, dir='.')

        self.local_filename = filename
        self.filename = list(ret.values())[0]
        self.command = self.remove_arg(command, "-r")
        # -c only point to local config file.
        self.command = self.remove_arg(self.command, "-c")
        self.command += ['-c', cfg_file]
        # remove --slave mode because the master cannot reach remote slave
        self.command = self.remove_arg(self.command, "-m")
        # replace absolute path with relative one because remote sos might have
        # a different path.
        if os.path.basename(command[0]) == "sos":
            self.command[0] = "sos"
            self.command[2] = self.filename
        elif os.path.basename(command[0]) == "sos-runner":
            self.command[0] = "sos-runner"
            self.command[1] = self.filename
        else:
            raise ValueError(
                f"Failed to generate remote execution command: {self.command}"
            )
        self.command = subprocess.list2cmdline(self.command)
        self.template_args = copy.deepcopy(self.config)
        self.template_args.update(template_args)
        env.log_to_file("WORKFLOW", f"Execute command on remote host: {self.command}")
        return True


class BackgroundProcess_WorkflowEngine(WorkflowEngine):
    def __init__(self, agent):
        super(BackgroundProcess_WorkflowEngine, self).__init__(agent)
        if "workflow_template" in self.config:
            self.workflow_template = self.config["workflow_template"].replace(
                "\r\n", "\n"
            )
        else:
            self.workflow_template = None

    def execute_workflow(self, filename, command, **template_args):
        #
        # calling super execute_workflow would set cleaned versions
        # of self.filename, self.command, and self.template_args
        if not super(BackgroundProcess_WorkflowEngine, self).execute_workflow(
            filename, command, **template_args
        ):
            env.log_to_file(
                "WORKFLOW", f'Failed to prepare workflow with command "{command}"'
            )
            return False

        if self.workflow_template:
            if not self._execute_workflow_with_template():
                return False
        else:
            if not self._execute_workflow():
                return False
        return True

    def _execute_workflow(self):
        # if no template, use a default command
        env.log_to_file("WORKDLOW", f'Execute "{self.command}"')
        try:
            self.agent.check_call(self.command, under_workdir=True)
        except Exception as e:
            raise RuntimeError(f"Failed to submit workflow {self.command}: {e}")
        return True

    def _execute_workflow_with_template(self):
        """Submit workflows by interpolating a shell script defined in workflow_template"""
        self.expand_template()

        try:
            # then copy the job file to remote host if necessary
            self.agent.send_job_file(self.job_file, dir="workflows")

            cmd = f"bash ~/.sos/workflows/{os.path.basename(self.job_file)}"
            env.log_to_file(
                "WORKFLOW", f'Execute "{self.command}" with script {self.job_text}'
            )
            self.agent.check_call(cmd, under_workdir=True)
        except Exception as e:
            raise RuntimeError(
                f"Failed to submit workflow {self.command} with script \n{self.job_text}\n: {e}"
            )
        finally:
            try:
                os.remove(self.job_file)
            except Exception as e:
                env.logger.debug(f"Failed to remove temporary workflow file: {e}")
        return True
