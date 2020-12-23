#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import copy
import os
import stat
import subprocess
import time


from .eval import cfg_interpolate
from .utils import env, textMD5, expand_time, format_duration


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
            self.template_args["filename"] = self.filename
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
        from .parser import SoS_Script
        from .__main__ import get_run_parser

        parser = get_run_parser(interactive=False, with_workflow=True)
        args, workflow_args = parser.parse_known_args(command[2:])
        script = SoS_Script(filename=filename)
        workflow = script.workflow(args.workflow, use_default=not args.__targets__)

        # we send config files also to remote host, but change localhost
        import yaml

        # process -c configfile
        host_cfg_file = os.path.join(
            os.path.expanduser("~"), ".sos", f"config_{self.alias}.yml"
        )
        with open(host_cfg_file, "w") as cfg:
            remote_cfg = copy.deepcopy(env.sos_dict["CONFIG"])
            remote_cfg["localhost"] = self.alias
            yaml.safe_dump(remote_cfg, cfg)
            cfg.flush()
            # copy the files over
            self.agent.send_job_file(f"~/.sos/config_{self.alias}.yml", dir=".")

        self.local_filename = filename

        self.job_name = workflow.calc_md5(workflow_args)

        if os.path.isfile(filename):
            ret = self.agent.send_to_host([filename])
        elif os.path.isfile(filename + ".sos"):
            ret = self.agent.send_to_host([filename + ".sos"])
        elif os.path.isfile(filename + ".ipynb"):
            ret = self.agent.send_to_host([filename + ".ipynb"])

        self.filename = list(ret.values())[0]
        self.command = self.remove_arg(command, "-r")
        # -c only point to local config file.
        self.command = self.remove_arg(self.command, "-c")
        self.command += ["-c", f"~/.sos/config_{self.alias}.yml"]
        # remove --slave mode because the master cannot reach remote slave
        self.command = self.remove_arg(self.command, "-m")
        self.command += ["-M", self.job_name]
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

    def query_workflows(
        self,
        workflows=None,
        check_all=False,
        verbosity=1,
        html=False,
        numeric_times=False,
        age=None,
        tags=None,
        status=None,
    ):
        try:
            return self.agent.check_output(
                "{} status {} -v {} {} {} {} {} {} {}".format(
                    self.agent.config.get("sos", "sos"),
                    "" if workflows is None else " ".join(workflows),
                    verbosity,
                    "--all workflows" if check_all else "",
                    "--html" if html else "",
                    "--numeric-times" if numeric_times else "",
                    f"--age {age}" if age else "",
                    f'--tags {" ".join(tags)}' if tags else "",
                    f'--status {" ".join(status)}' if status else "",
                )
            )
        except subprocess.CalledProcessError as e:
            if verbosity >= 3:
                env.logger.warning(
                    f'Failed to query status of workflows on {self.alias}: {"" if e.stderr is None else e.stderr.decode()}'
                )
            return ""

    def kill_workflows(self, workflows, tags=None, all_workflows=False):
        cmd = "{} kill {} {} {}".format(
            self.agent.config.get("sos", "sos"),
            "" if all_workflows else " ".join(workflows),
            f'--tags {" ".join(tags)}' if tags else "",
            "--all workflows" if all_workflows else "",
        )

        try:
            ret = self.agent.check_output(cmd)
            env.logger.debug(f'"{cmd}" executed with response "{ret}"')
        except subprocess.CalledProcessError:
            env.logger.error(
                "Failed to kill all workflows"
                if all_workflows
                else f'Failed to kill workflows {" ".join(workflows)}'
            )
            return ""
        return ret

    def purge_workflows(
        self, workflows, purge_all=False, age=None, status=None, tags=None, verbosity=2
    ):
        try:
            return self.agent.check_output(
                "{} purge {} {} {} {} {} -v {}".format(
                    self.agent.config.get("sos", "sos"),
                    " ".join(workflows),
                    "--all" if purge_all else "",
                    f"--age {age}" if age is not None else "",
                    f'--status {" ".join(status)}' if status is not None else "",
                    f'--tags {" ".join(tags)}' if tags is not None else "",
                    verbosity,
                )
            )
        except subprocess.CalledProcessError:
            env.logger.error(f"Failed to purge workflows {workflows}")
            return ""


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


class WorkflowPulse:
    def __init__(self, workflow_id):
        self.id = workflow_id
        self._tags = None
        self._status = None
        self._content = {}
        self._start_time = None
        self._complete_time = None
        self.pulse_file = os.path.join(
            os.path.expanduser("~"), ".sos", "workflows", self.id + ".pulse"
        )

    def exists(self):
        return os.path.isfile(self.pulse_file)

    def mark_killed(self):
        from stat import S_IREAD

        os.chmod(self.pulse_file, S_IREAD)

    @property
    def created(self):
        return (
            "Created " + format_duration(time.time() - self._start_time, True) + " ago"
            if self._start_time
            else ""
        )

    @property
    def started(self):
        return (
            "Started " + format_duration(time.time() - self._start_time, True) + " ago"
            if self._start_time
            else ""
        )

    @property
    def duration(self):
        if not self._complete_time:
            return ""
        return "Ran for " + format_duration(int(self._complete_time - self._start_time))

    @property
    def tags(self):
        if self._tags is None:
            self.parse_pulse_file()
        return self._tags

    @property
    def status(self):
        if self._status is None:
            self.parse_pulse_file()
        return self._status

    def get_file(self, ext):
        if ext in self._content:
            return self._content[ext]

        res_file = os.path.join(
            os.path.expanduser("~"), ".sos", "workflows", self.id + ext
        )
        content = ""
        if os.path.isfile(res_file):
            try:
                with open(res_file) as res:
                    content = res.read()
            except:
                pass
        self._content[ext] = content
        return content

    @property
    def script(self):
        return self.get_file(".sos")

    @property
    def stdout(self):
        return self.get_file(".out")

    @property
    def stderr(self):
        return self.get_file(".err")

    @property
    def sh_script(self):
        return self.get_file(".sh")

    def parse_pulse_file(self):
        self._status = "unknown"
        self._tags = ""
        last_active_time = None
        if not os.path.isfile(self.pulse_file):
            self._status = 'missing'
            return
        with open(self.pulse_file) as pulse:
            for line in pulse:
                if line.startswith("#time\t"):
                    continue
                if not line.startswith("#"):
                    try:
                        last_active_time = line.split('\t', 1)[0]
                    except:
                        pass
                    continue
                fields = line.split("\t")
                if len(fields) != 3:
                    env.logger.error(line)
                    continue
                last_active_time = fields[0][1:]
                if fields[1] == "status":
                    self._status = fields[2].strip()
                    if self._status == "running":
                        try:
                            self._start_time = float(fields[0][1:])
                        except:
                            pass
                    elif self._status == "completed":
                        try:
                            self._complete_time = float(fields[0][1:])
                        except:
                            pass
                elif fields[1] == "tags":
                    self._tags = fields[2].strip()
        if not os.stat(self.pulse_file).st_mode & stat.S_IWUSR:
            self._status = 'aborted'
        if last_active_time is None or time.time() - float(last_active_time) > 120:
            self._status = 'failed'


def print_workflow_status(
    workflows,
    check_all=False,
    verbosity: int = 1,
    html: bool = False,
    numeric_times=False,
    age=None,
    tags=None,
    status=None,
):
    import glob

    all_workflows: List = []
    if check_all:
        workflows = glob.glob(
            os.path.join(os.path.expanduser("~"), ".sos", "workflows", "*.pulse")
        )
        all_workflows = [
            (os.path.basename(x)[:-6], os.path.getmtime(x)) for x in workflows
        ]
        if not all_workflows:
            return
    else:
        for t in workflows:
            matched_names = glob.glob(
                os.path.join(
                    os.path.expanduser("~"), ".sos", "workflows", f"{t}*.pulse"
                )
            )
            matched = [
                (os.path.basename(x)[:-6], os.path.getmtime(x)) for x in matched_names
            ]
            if not matched:
                all_workflows.append((t, None))
            else:
                all_workflows.extend(matched)

    if age is not None:
        age = expand_time(age, default_unit="d")
        if age > 0:
            all_workflows = [x for x in all_workflows if time.time() - x[1] >= age]
        else:
            all_workflows = [x for x in all_workflows if time.time() - x[1] <= -age]

    all_workflows = sorted(
        list(set(all_workflows)), key=lambda x: 0 if x[1] is None else x[1]
    )

    if tags:
        all_workflows = [
            x
            for x in all_workflows
            if WorkflowPulse(x[0]).exists()
            and any(y in tags for y in WorkflowPulse(x[0]).tags.split())
        ]

    if not all_workflows:
        return

    workflow_info = [WorkflowPulse(x[0]) for x in all_workflows]
    #
    # automatically remove non-running workflows that are more than 30 days old
    to_be_removed = [
        t
        for s, (t, d) in zip(workflow_info, all_workflows)
        if d is not None and time.time() - d > 30 * 24 * 60 * 60 and s != "running"
    ]

    if status:
        workflow_info = [x for x in workflow_info if x.status in status]
    #
    if verbosity == 0:
        print("\n".join([x.status for x in workflow_info]))
    elif verbosity == 1:
        for info in workflow_info:
            print(f"{info.id}\t{info.status}")
    elif verbosity == 2:
        tsize = 20
        for info in workflow_info:
            print(
                f"{info.id}\t{info.tags.ljust(tsize)}\t{info.duration:<14}\t{info.status}"
            )
    elif verbosity == 3:
        tsize = 20
        for info in workflow_info:
            tsize = max(tsize, len(info.tags))
            print(
                f"{info.id}\t{info.tags.ljust(tsize)}\t{info.created:<14}\t{info.started:<14}\t{info.duration:<14}\t{info.status}"
            )
    elif verbosity == 4:
        import pprint

        for info in workflow_info:
            if info.status == "missing":
                print(f"{info.id}\t{info.status}\n")
                continue
            print(f"WORKFLOW:\t{info.id}")
            print(f"status\t{info.status}")
            print(f"{info.created}")
            if info.started:
                print(f"{info.started}")
            if info.duration:
                print(f"{info.duration}")

            print()
            print("TAGS:\n=====")
            print(info.tags)
            print()

            if info.script:
                print("CRIPT:\n=====")
                print(info.script)

            if info.sh_script:
                print("WRAPPER SCRIPT:\n==============")
                print(info.sh_script)

            if info.stdout:
                print("STDOUT:\n=======")
                print(info.stdout)

            if info.stderr:
                print("STDERR:\n=======")
                print(info.stderr)

    # remove jobs that are older than 1 month
    if to_be_removed:
        purge_workflows(to_be_removed, verbosity=0)


def kill_workflow(workflow):
    wp = WorkflowPulse(workflow)
    status = wp.status
    if status == "completed":
        return "completed"
    with open(
        os.path.join(
            os.path.expanduser("~"), ".sos", "workflows", workflow + ".soserr"
        ),
        "a",
    ) as err:
        err.write(f"Workflow {workflow} killed by sos kill command or workflow engine.")

    wp.mark_killed()
    return "aborted"


def kill_workflows(workflows, tags=None):
    import glob

    if not workflows:
        workflows = glob.glob(
            os.path.join(os.path.expanduser("~"), ".sos", "workflows", "*.pulse")
        )
        all_workflows = [os.path.basename(x)[:-6] for x in workflows]
    else:
        all_workflows = []
        for t in workflows:
            matched = glob.glob(
                os.path.join(
                    os.path.expanduser("~"), ".sos", "workflows", f"{t}*.pulse"
                )
            )
            matched = [os.path.basename(x)[:-6] for x in matched]
            if not matched:
                env.logger.warning(f"{t} does not match any existing workflow")
            else:
                all_workflows.extend(matched)
    if tags:
        all_workflows = [
            x
            for x in all_workflows
            if any(x in tags for x in WorkflowPulse(x).tags.split())
        ]

    if not all_workflows:
        # env.logger.warning("No workflow to kill")
        return
    all_workflows = sorted(list(set(all_workflows)))
    # at most 20 threads
    for wf in all_workflows:
        ret = kill_workflow(wf)
        print(f"{wf}\t{ret}")


def purge_workflows(
    workflows, purge_all=None, age=None, status=None, tags=None, verbosity=2
):
    import glob

    if workflows:
        all_workflows = []
        for t in workflows:
            matched = glob.glob(
                os.path.join(
                    os.path.expanduser("~"), ".sos", "workflows", f"{t}*.pulse"
                )
            )
            matched = [(os.path.basename(x)[:-6], os.path.getmtime(x)) for x in matched]
            if not matched:
                print(f"{t}\tmissing")
            all_workflows.extend(matched)
    elif purge_all or age or status or tags:
        workflows = glob.glob(
            os.path.join(os.path.expanduser("~"), ".sos", "workflows", "*.pulse")
        )
        all_workflows = [
            (os.path.basename(x)[:-6], os.path.getmtime(x)) for x in workflows
        ]
    else:
        raise ValueError(
            "Please specify either workflows or one or more of --all, --status, --tags--age"
        )
    #
    if age is not None:
        age = expand_time(age, default_unit="d")
        if age > 0:
            all_workflows = [x for x in all_workflows if time.time() - x[1] >= age]
        else:
            all_workflows = [x for x in all_workflows if time.time() - x[1] <= -age]

    if status:
        all_workflows = [
            x for x in all_workflows if WorkflowPulse(x[0]).status in status
        ]

    if tags:
        all_workflows = [
            x
            for x in all_workflows
            if any(x in tags for x in WorkflowPulse(x[0]).tags.split())
        ]
    #
    # remoe all workflow files
    all_workflows = set([x[0] for x in all_workflows])
    if all_workflows:
        #
        # find all related files, including those in nested directories
        from collections import defaultdict

        to_be_removed = defaultdict(list)
        for dirname, _, filelist in os.walk(
            os.path.join(os.path.expanduser("~"), ".sos", "workflows")
        ):
            for f in filelist:
                ID = os.path.basename(f).split(".", 1)[0]
                if ID in all_workflows:
                    to_be_removed[ID].append(os.path.join(dirname, f))
        #
        status_cache = {}
        for workflow in all_workflows:
            removed = True
            for f in to_be_removed[workflow]:
                try:
                    if verbosity > 3:
                        if (
                            "WORKFLOW" in env.config["SOS_DEBUG"]
                            or "ALL" in env.config["SOS_DEBUG"]
                        ):
                            env.log_to_file("TASK", f"Remove {f}")
                    os.remove(f)
                except Exception as e:
                    removed = False
                    if verbosity > 0:
                        env.logger.warning(
                            f"Failed to purge workflow {workflow[0]}: {e}"
                        )
            status_cache.pop(workflow, None)
            if removed and verbosity > 1:
                print(f"{workflow}\tpurged")
    elif verbosity > 1:
        env.logger.debug("No matching workflows to purge")
    if purge_all and age is None and status is None and tags is None:
        matched = glob.glob(
            os.path.join(os.path.expanduser("~"), ".sos", "workflows", "*")
        )
        count = 0
        for f in matched:
            if os.path.isdir(f):
                import shutil

                try:
                    shutil.rmtree(f)
                    count += 1
                except Exception as e:
                    if verbosity > 0:
                        env.logger.warning(f"Failed to remove {f}: {e}")
            else:
                try:
                    os.remove(f)
                    count += 1
                except Exception as e:
                    if verbosity > 0:
                        env.logger.warning(f"Failed to remove {e}")
        if count > 0 and verbosity > 1:
            env.logger.info(f"{count} other files and directories are removed.")
    return ""
