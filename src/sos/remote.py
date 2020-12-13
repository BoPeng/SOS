#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import subprocess
import sys

import pexpect

from .utils import env
from .targets import path
from .hosts import Host


def list_queues(cfg, hosts=[], verbosity=1):
    env.verbosity = 1
    all_hosts = cfg.get("hosts", [])
    if not all_hosts:
        env.logger.warning(
            "No remote host or task queue is defined in ~/.sos/hosts.yml."
        )
        return
    for host in hosts:
        if host not in all_hosts:
            env.logger.warning(f"Undefined host {host}")
    host_description = [
        ["Alias", "Address", "Queue Type", "Description"],
        ["-----", "-------", "----------", "-----------"],
    ]
    for host in sorted([x for x in hosts if x in all_hosts] if hosts else all_hosts):
        try:
            h = Host(host, start_engine=False, test_connection=False)
        except Exception as e:
            if verbosity == 0:
                print(f"{host} ({e})")
            elif verbosity in (1, 2):
                host_description.append([host, "?", "?", str(e)])
            else:
                print(f"Queue:       {host}")
                print(f"Error:       {str(e)}")
                if isinstance(cfg["hosts"][host], dict):
                    print("Configuration:")
                    for key in cfg["hosts"][host].keys():
                        print(f'  {(key + ":").ljust(24)} {cfg["hosts"][host][key]}')
                print()
            continue
        if verbosity == 0:
            print(h.alias)
        elif verbosity in (1, 2):
            host_description.append(
                [h.alias, h._host_agent.address, h._engine_type, h.description]
            )
        else:
            print(f"Queue:       {h.alias}")
            print(f"Address:     {h._host_agent.address}")
            print(f"Queue Type:  {h._engine_type}")
            print(f"Description: {h.description}")
            print("Configuration:")
            keys = sorted(h.config.keys())
            for key in keys:
                print(f'  {(key + ":").ljust(24)} {h.config[key]}')
            print()
    if verbosity in (1, 2):
        width = [(len(x) for x in row) for row in host_description]
        max_width = [max(x) for x in zip(*width)]
        print(
            "\n".join(
                " ".join([t.ljust(w) for t, w in zip(row, max_width)])
                for row in host_description
            )
        )


def status_of_queues(cfg, hosts=[], verbosity=1):
    env.verbosity = 1
    all_hosts = cfg.get("hosts", [])
    if not all_hosts:
        env.logger.warning(
            "No remote host or task queue is defined in ~/.sos/hosts.yml."
        )
        return
    for host in hosts:
        if host not in all_hosts:
            env.logger.warning(f"Undefined host {host}")
    host_description = [
        ["Alias", "Address", "Queue Type", "Running", "Pending", "Completed"],
        ["-----", "-------", "----------", "-------", "-------", "---------"],
    ]
    for host in sorted([x for x in hosts if x in all_hosts] if hosts else all_hosts):
        try:
            h = Host(host, start_engine=True)
            status = h._task_engine.query_tasks(tasks=[], verbosity=0)
            if not status:
                raise ValueError("Failed to query status")
        except Exception as e:
            if verbosity == 0:
                print(f"{host} ({e})")
            elif verbosity in (1, 2):
                host_description.append([host, "?", "?", "?", "?", "?"])
            else:
                print(f"Queue:       {host}")
                print(f"Error:       {str(e)}")
                if isinstance(cfg["hosts"][host], dict):
                    print("Configuration:")
                    for key in cfg["hosts"][host].keys():
                        print(f'  {(key + ":").ljust(24)} {cfg["hosts"][host][key]}')
                print()
            continue
        status = [x.strip() for x in status.splitlines() if x.strip()]
        running = str(status.count("running"))
        pending = str(status.count("pending"))
        completed = str(status.count("completed"))

        if verbosity == 0:
            print(f"{h.alias} {running} {pending} {completed}")
        elif verbosity in (1, 2):
            host_description.append(
                [
                    h.alias,
                    h._host_agent.address,
                    h._engine_type,
                    running,
                    pending,
                    completed,
                ]
            )
        else:
            print(f"Queue:       {h.alias}")
            print(f"Address:     {h._host_agent.address}")
            print(f"Queue Type:  {h._engine_type}")
            print(f"Description: {h.description}")
            for k in set(status):
                if k not in ("running", "pending", "completed"):
                    print(f'{(k+":").ljust(26)} {status.count(k)}')
            print("Configuration:")
            keys = sorted(h.config.keys())
            for key in keys:
                print(f'  {(key + ":").ljust(24)} {h.config[key]}')
            print()
    if verbosity in (1, 2):
        width = [(len(x) for x in row) for row in host_description]
        max_width = [max(x) for x in zip(*width)]
        print(
            "\n".join(
                " ".join([t.ljust(w) for t, w in zip(row, max_width)])
                for row in host_description
            )
        )


def test_ssh(host):
    return host.test_connection()


def test_scp(host):
    if host.address == "localhost":
        return "OK"
    # test send task file
    import random

    tID = random.randint(1, 100000)
    task_filename = os.path.join(os.path.expanduser("~"), ".sos", f"test_{tID}.tmp")
    with open(task_filename, "w") as test_task:
        test_task.write("test task")
    # test scp
    try:
        host.send_job_file(task_filename)
    except Exception as e:
        return str(e)
    # test remove file using ssh
    try:
        host.check_call(f"rm -f ~/.sos/test_{tID}.tmp")
    except Exception as e:
        return str(e)
    # test rsync
    return "OK"


def test_cmd(host, cmd):
    # test the execution of sos commands
    try:
        ret = host.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ret == 0:
            return "OK"
        else:
            return "sos not installed or not accessible on host."
    except Exception as e:
        return str(e)


def test_paths(host):
    if host.address == "localhost":
        return "OK"
    # shared means, if localhost creates a file, it should be
    # instantly available on the remote host
    if not host.path_map:
        return "No path_map between local and remote host."
    import random

    tID = random.randint(1, 100000)
    for local, remote in host.path_map.items():
        if local in host.shared_dirs:
            # will be tested by 'shared'
            continue
        # now, let us see if two directory has the same files?
        if not os.path.isdir(local):
            return f"Mapped directory {local} does not exist."
        # remote?
        try:
            host.check_output(f"ls -a {path(remote):q}")
        except Exception:
            return f"Failed to access shared directory {remote} on remote host."

        # test if local directory is writable
        try:
            with open(os.path.join(local, f".sos_test_{tID}.txt"), "w") as tFile:
                tFile.write(f"{tID}")
        except Exception:
            return f"Failed to write to mapped directory {local}"
        # test if file can be sent
        try:
            host.send_to_host(os.path.join(local, f".sos_test_{tID}.txt"))
        except Exception as e:
            return (
                f"Failed to send files under {local} to remote host under {remote}: {e}"
            )

        # the file should be available on remote host
        try:
            remote_content = host.check_output(f"cat {remote}/.sos_test_{tID}.txt")
        except Exception as e:
            return (
                f"Failed to send files under {local} to remote host under {remote}: {e}"
            )
        if remote_content != str(tID):
            return f"Content of file sent does not match: {tID} sent, {remote_content} received"
        # test retrieving files
        # remove local file
        os.remove(os.path.join(local, f".sos_test_{tID}.txt"))
        # copy file back
        try:
            host.receive_from_host(os.path.join(local, f".sos_test_{tID}.txt"))
        except Exception as e:
            return f"Failed to receive file from remote host {remote}: {e}"
        #
        if not os.path.isfile(os.path.join(local, f".sos_test_{tID}.txt")):
            return (
                f"Failed to receive file from remote host {remote}: file does not exist"
            )
        # check file content?
        with open(os.path.join(local, f".sos_test_{tID}.txt"), "r") as tFile:
            remote_content = tFile.read()
        if remote_content != str(tID):
            return f"Content of received file does not match: {tID} expected, {remote_content} received."
        # if everything ok, remove local and remote test files
        os.remove(os.path.join(local, f".sos_test_{tID}.txt"))
        #
        try:
            remote_content = host.check_output(f"rm {remote}/.sos_test_{tID}.txt")
        except Exception as e:
            return f"Failed to remove test file on remote host: {e}"
    return "OK"


def test_shared(host):
    if host.address == "localhost":
        return "OK (localhost)"
    # shared means, if localhost creates a file, it should be
    # instantly available on the remote host
    for local in host.shared_dirs:
        if local not in host.path_map:
            return f"shared directory {local} not in path_map"
        # now, let us see if two directory has the same files?
        if not os.path.isdir(local):
            return f"shared directory {local} does not exist."
        local_files = os.listdir(local)
        # remote?
        remote = host.path_map[local]
        try:
            remote_files = host.check_output(f"ls -a {path(remote):q}")
        except Exception:
            return f"Failed to access shared directory {remote} on remote host."
        remote_files = [x for x in remote_files.splitlines() if x not in (".", "..")]
        #
        if sorted(local_files) != sorted(remote_files):
            return f"shared directory {local} has different content on remote host under {remote}"

    return f'OK (shared {" ".join(host.shared_dirs)})'


def stty_sane():
    try:
        subprocess.check_call("stty sane", shell=True)
    except Exception:
        pass


def test_queue(host, cmd=None, verbosity=1):
    try:
        h = Host(host, start_engine=False)
    except Exception as e:
        if verbosity > 2:
            env.logger.warning(e)
        return [host, "?", "?", "-", "-", "-", "-", "-"]
    ssh_res = test_ssh(h._host_agent)
    return [
        h.alias,
        h._host_agent.address,
        h._engine_type,
        ssh_res,
        test_scp(h._host_agent) if ssh_res.startswith("OK") else "-",
        test_cmd(h._host_agent, [h.config.get("sos", "sos"), "-h"])
        if ssh_res.startswith("OK")
        else "-",
        test_paths(h._host_agent) if ssh_res.startswith("OK") else "-",
        test_shared(h._host_agent) if ssh_res.startswith("OK") else "-",
    ] + ([] if cmd is None else [test_cmd(h._host_agent, cmd)])


def test_queues(cfg, hosts=[], cmd=None, verbosity=1):
    env.verbosity = verbosity
    all_hosts = cfg.get("hosts", [])
    if not all_hosts:
        env.logger.warning(
            "No remote host or task queue is defined in ~/.sos/hosts.yml."
        )
        return
    host_description = [
        ["Alias", "Address", "Queue Type", "ssh", "scp", "sos", "paths", "shared"]
        + ([] if cmd is None else [" ".join(cmd)]),
        ["-----", "-------", "----------", "---", "---", "---", "-----", "------"]
        + ([] if cmd is None else ["-" * len(" ".join(cmd))]),
    ]
    for host in hosts:
        if host not in all_hosts:
            env.logger.warning(f"Undefined host {host}")
    hosts = [x for x in hosts if x in all_hosts] if hosts else all_hosts
    if not hosts:
        return
    from multiprocessing import Pool

    pool = Pool(min(len(hosts), 10))
    host_description.extend(
        pool.starmap(test_queue, [(x, cmd, verbosity) for x in hosts])
    )
    if verbosity == 0:
        # just print succ or self
        for hd in host_description:
            print(f'{hd[0]} {"OK" if all(x=="OK" for x in hd[3:]) else "FAIL"}')
    elif verbosity in (1, 2):
        shortened = host_description[:2]
        for row in host_description[2:]:
            shortened.append(
                row[:3]
                + [
                    "OK" if x.startswith("OK") else ("-" if x == "-" else "FAIL")
                    for x in row[3:]
                ]
            )
        width = [(len(x) for x in row) for row in shortened]
        max_width = [max(x) for x in zip(*width)]
        print(
            "\n".join(
                " ".join([t.ljust(w) for t, w in zip(row, max_width)])
                for row in shortened
            )
        )
        if any("FAILED" in row for row in shortened):
            print(
                '\nUse command "sos remote --test host -v3" to check details of hosts with failed tests.'
            )
    else:
        for row in host_description[2:]:
            print(f"Alias:       {row[0]}")
            print(f"Address:     {row[1]}")
            print(f"Queue Type:  {row[2]}")
            print(f"ssh:         {row[3]}")
            print(f"scp:         {row[4]}")
            print(f"sos:         {row[5]}")
            print(f"paths:       {row[6]}")
            print(f"shared:      {row[7]}")
            if cmd:
                print(
                    f'{" ".join(cmd)}:{" "*(max(1, 12 - len(" ".join(cmd))))}{row[8]}'
                )
            print()


def copy_public_key(host, agent, password):
    try:
        if password is None:
            import getpass

            password = getpass.getpass(f"Please enter password for {agent.address}: ")
        cmd = f"scp -P {agent.port if agent.port else 22} {os.path.expanduser('~')}/.ssh/id_rsa.pub {agent.address}:id_rsa.pub.{host}"
        env.logger.info(cmd)
        p = pexpect.spawn(cmd, echo=False)
        i = p.expect(
            [
                "(?i)are you sure you want to continue connecting",
                "[pP]assword:",
                pexpect.EOF,
            ]
        )
        if i == 0:
            p.sendline("yes")
            p.expect(
                [
                    "(?i)are you sure you want to continue connecting",
                    "[pP]assword:",
                    pexpect.EOF,
                ],
                timeout=5,
            )

        if i == 1:
            p.sendline(password)
            i = p.expect(["assword:", pexpect.EOF])
            if i == 0:
                p.close(force=True)
                return f"Incorrect password specified (you can try to specify it with command line option --password)"
        if i == 2:
            p.close()
            return f"Failed to copy public key to {agent.address}"
    except Exception as e:
        p.close()
        return f"Failed to copy public key to {host}: {e}"
    #
    # ssh
    try:
        cmd = f"ssh {agent.address} -p {agent.port} '[ -d .ssh ] || mkdir .ssh && chmod 700 .ssh; cat id_rsa.pub.{host} >> .ssh/authorized_keys; rm -f id_rsa.pub.{host}'"
        env.logger.info(cmd)
        p = pexpect.spawn(cmd, echo=False)
        i = p.expect(
            [
                "(?i)are you sure you want to continue connecting",
                "assword:",
                pexpect.EOF,
            ]
        )
        if i == 0:
            p.sendline("yes")
            p.expect(
                [
                    "(?i)are you sure you want to continue connecting",
                    "[pP]assword:",
                    pexpect.EOF,
                ],
                timeout=5,
            )

        if i == 1:
            p.sendline(password)
            i = p.expect(["assword:", pexpect.EOF])
            if i == 0:
                p.close(force=True)
                return f"Incorrect password specified (you can try to specify it with command line option --password)"
        elif i != 1:
            p.close()
            return f"Failed to append public key to .ssh/authorized_keys"
    except Exception as e:
        p.close()
        return f"Failed to append public key to .ssh/authorized_keys: {e}"
    return "OK"


def create_public_key():
    try:
        cmd = "ssh-keygen -t rsa"
        env.logger.info(cmd)
        p = pexpect.spawn(cmd, echo=False)
        while True:
            i = p.expect(
                [
                    "Enter file in which to save .*",
                    "Enter passphrase.*",
                    "Enter same passphrase again:.*",
                    "Overwrite .*",
                    pexpect.EOF,
                ]
            )
            if i in (0, 1, 2):
                p.sendline("")
            elif i == 3:
                p.sendline("y")
            elif i == 4:
                break
    except Exception as e:
        p.close()
        raise RuntimeError(f"Failed to create a public key: {e}")


def setup_remote_access(cfg, hosts=[], password="", verbosity=1):
    env.verbosity = verbosity
    all_hosts = cfg.get("hosts", [])
    if not all_hosts and not hosts:
        env.logger.warning(
            "No remote host or task queue is defined in ~/.sos/hosts.yml."
        )
        return
    for host in hosts:
        if host not in all_hosts:
            env.logger.warning(
                f"Treating undefined host {host} as address of a remote host."
            )
    # public_key
    public_key = os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa.pub")

    from argparse import Namespace

    for host in sorted(hosts if hosts else all_hosts):
        try:
            if host in all_hosts:
                h = Host(host, start_engine=False, test_connection=False)
                host_agent = h._host_agent
                #
                # can ssh already?
                response = test_ssh(host_agent)
                if response.startswith("OK"):
                    env.logger.info(
                        f"Public key access is already enabled for host ``{host}`` with address ``{host_agent.address}``"
                    )
                    continue
                elif "Could not resolve hostname" in response:
                    env.logger.error(response)
                    sys.exit(1)
            else:
                host_agent = Namespace(address=host, port=22)
        except Exception as e:
            env.logger.error(f"Failed to start set up remote engine for {host}: {e}")
            continue

        if os.path.isfile(public_key):
            env.logger.info("Using existing public key .ssh/id_rsa.pub")
        else:
            env.logger.info(f"Public key {public_key} is found. Creating one.")
            create_public_key()
            if not os.path.isfile(public_key):
                raise RuntimeError("Failed to create public key.")
        #
        response = copy_public_key(host, host_agent, password)
        stty_sane()
        if not response.startswith("OK"):
            env.logger.error(response)
            sys.exit(1)
        # file copied, check ssh again.
        if isinstance(host_agent, Namespace):
            host_agent = Host(
                host, start_engine=False, test_connection=False
            )._host_agent
        response = test_ssh(host_agent)
        if response.startswith("OK"):
            env.logger.info(
                f"Public key access is successfully set up for host ``{host}`` with address ``{host_agent.address}``"
            )
            continue
        else:
            env.logger.error(
                f"Failed to connect to {host} after passing public key. Possible problems include permission of .ssh and home directories."
            )


def login_host(cfg, host):
    try:
        h = Host(host, start_engine=False)
    except Exception as e:
        raise ValueError(f"Unrecognized or invalid host {host}: {e}")

    address, port = h._host_agent.address, h._host_agent.port
    try:
        env.logger.info(f"Running ``ssh {address} -p {port}``")
        os.execvp("ssh", ["ssh", address, "-p", str(port)])
    except Exception as e:
        raise RuntimeError(f"Failed to log in to {host}: {e}")


def run_command_on_hosts(cfg, hosts, cmd, verbosity):
    env.verbosity = verbosity
    if not hosts:
        hosts = cfg.get("hosts", [])
    if not hosts:
        env.logger.warning(
            "No remote host or task queue is defined in ~/.sos/hosts.yml."
        )
        return
    for host in hosts:
        # runing command on all hosts
        try:
            env.logger.info(f'Running ``{" ".join(cmd)}`` on ``{host}``')
            h = Host(host, start_engine=False)
            print(h._host_agent.check_output(cmd, under_workdir=True))
        except Exception as e:
            from .utils import get_traceback

            if verbosity and verbosity > 2:
                sys.stderr.write(get_traceback())
            env.logger.error(str(e))


def push_to_hosts(cfg, hosts, items, verbosity):
    env.verbosity = verbosity
    if not hosts:
        hosts = cfg.get("hosts", [])
    if not hosts:
        env.logger.warning(
            "No remote host or task queue is defined in ~/.sos/hosts.yml."
        )
        return
    for host in hosts:
        try:
            env.logger.info(f'Pushing ``{" ".join(items)}`` to ``{host}``')

            h = Host(host, start_engine=False)
            #
            sent = h.send_to_host(items)
            #
            env.logger.info(
                "{} item{} sent:\n{}".format(
                    len(sent),
                    " is" if len(sent) <= 1 else "s are",
                    "\n".join(
                        ["{} => {}".format(x, sent[x]) for x in sorted(sent.keys())]
                    ),
                )
            )
        except Exception as e:
            from .utils import get_traceback

            if verbosity and verbosity > 2:
                sys.stderr.write(get_traceback())
            env.logger.error(str(e))
            sys.exit(1)


def pull_from_host(cfg, hosts, items, verbosity):
    env.verbosity = verbosity
    if not hosts:
        hosts = cfg.get("hosts", [])
    if not hosts:
        env.logger.warning(
            "No remote host or task queue is defined in ~/.sos/hosts.yml."
        )
        return
    if len(hosts) > 1:
        raise ValueError("Can only pull from a single remote host.")
    try:
        env.logger.info(f'Pulling ``{" ".join(items)}`` from ``{hosts[0]}``')

        host = Host(hosts[0], start_engine=False)
        #
        received = host.receive_from_host(items)
        #
        print(
            "{} item{} received:\n{}".format(
                len(received),
                " is" if len(received) <= 1 else "s are",
                "\n".join(
                    ["{} <= {}".format(x, received[x]) for x in sorted(received.keys())]
                ),
            )
        )
    except Exception as e:
        from .utils import get_traceback

        if verbosity and verbosity > 2:
            sys.stderr.write(get_traceback())
        env.logger.error(str(e))
        sys.exit(1)
