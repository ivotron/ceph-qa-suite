
"""
Useful hack: override Filesystem and Mount interfaces to run a CephFSTestCase against a vstart
ceph instance instead of a packaged/installed cluster.  Use this to turn around test cases
quickly during development.

Because many systems require root to work with fuse mounts, run this as root.
"""

from StringIO import StringIO
import time
import json

from teuthology.exceptions import CommandFailedError

from tasks.cephfs.fuse_mount import FuseMount
from tasks.cephfs.filesystem import Filesystem
import subprocess
import logging
from teuthology.contextutil import MaxWhileTries

log = logging.getLogger(__name__)
log.addHandler(logging.StreamHandler())
log.setLevel(logging.DEBUG)

class LocalRemoteProcess(object):
    def __init__(self, args, subproc, check_status):
        self.args = args
        self.subproc = subproc
        self.stdout = StringIO()
        self.stderr = StringIO()
        self.check_status = check_status

    def wait(self):
        out, err = self.subproc.communicate()
        self.stdout.write(out)
        self.stderr.write(err)

        self.exitstatus = self.returncode = self.subproc.returncode

        if self.check_status and self.exitstatus != 0:
            raise CommandFailedError(" ".join(self.args), self.exitstatus)

class LocalRemote(object):
    """
    Amusingly named class to present the teuthology RemoteProcess interface when we are really
    running things locally for vstart

    Run this inside your src/ dir!
    """

    def run(self, args, check_status=True, wait=True, stdout=None):
        # We don't need no stinkin' sudo
        if args[0] == "sudo":
            args = args[1:]

        if stdout is not None:
            log.warn("unused stdout passed with {0}".format(args))

        subproc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc = LocalRemoteProcess(args, subproc, check_status)

        if wait:
            proc.wait()

        return proc

# FIXME: twiddling vstart daemons is likely to be unreliable, we should probably just let vstart
# run RADOS and run the MDS daemons directly from the test runner
class LocalDaemon(object):
    def __init__(self, daemon_type, daemon_id):
        self.daemon_type = daemon_type
        self.daemon_id = daemon_id
        self.controller = LocalRemote()
        self.log = logging.getLogger("daemon.{0}.{1}".format(self.daemon_type, self.daemon_id))

    def running(self):
        return self._get_pid() is not None

    def _get_pid(self):
        """
        Return PID as an integer or None if not found
        """
        ps_txt = self.controller.run(
            args=["ps", "u", "-C", "ceph-{0}".format(self.daemon_type)],
            check_status=False  # ps returns err if nothing running so ignore
        ).stdout.getvalue().strip()
        lines = ps_txt.split("\n")[1:]

        for line in lines:
            if line.find("-i {0} ".format(self.daemon_id)) != -1:
                return int(line.split()[1])

        return None

    def wait(self, timeout):
        waited = 0
        while self._get_pid() is not None:
            if waited > timeout:
                raise MaxWhileTries("Timed out waiting for daemon {0}.{1}".format(self.daemon_type, self.daemon_id))
            time.sleep(1)
            waited += 1

    def stop(self, timeout=300):
        if not self.running():
            self.log.error('tried to stop a non-running daemon')
            return

        self.controller.run(["kill", "%s" % self._get_pid()])
        self.wait(timeout=timeout)

    def restart(self):
        self.controller.run(["./ceph-{0}".format(self.daemon_type), "-i", self.daemon_id])



class LocalFuseMount(FuseMount):
    def __init__(self, client_id, mount_point):
        test_dir = "/tmp/not_there"
        super(LocalFuseMount, self).__init__(None, test_dir, client_id, LocalRemote())
        self.mountpoint = mount_point

    def mount(self):
        self.client_remote.run(
            args=[
                'mkdir',
                '--',
                self.mountpoint,
            ],
        )

        def list_connections():
            self.client_remote.run(
                args=["sudo", "mount", "-t", "fusectl", "/sys/fs/fuse/connections", "/sys/fs/fuse/connections"],
                check_status=False
            )
            p = self.client_remote.run(
                args=["ls", "/sys/fs/fuse/connections"],
                check_status=False
            )
            if p.exitstatus != 0:
                return []

            ls_str = p.stdout.getvalue().strip()
            if ls_str:
                return [int(n) for n in ls_str.split("\n")]
            else:
                return []

        # Before starting ceph-fuse process, note the contents of
        # /sys/fs/fuse/connections
        pre_mount_conns = list_connections()
        log.info("Pre-mount connections: {0}".format(pre_mount_conns))

        self.client_remote.run(args=[
            "./ceph-fuse",
            "--name",
            "client.{0}".format(self.client_id),
            self.mountpoint
        ])

        # Wait for the connection reference to appear in /sys
        waited = 0
        while list_connections() == pre_mount_conns:
            time.sleep(1)
            waited += 1
            if waited > 30:
                raise RuntimeError("Fuse mount failed to populate /sys/ after {0} seconds".format(
                    waited
                ))

        post_mount_conns = list_connections()
        log.info("Post-mount connections: {0}".format(post_mount_conns))

        # Record our fuse connection number so that we can use it when
        # forcing an unmount
        new_conns = list(set(post_mount_conns) - set(pre_mount_conns))
        if len(new_conns) == 0:
            raise RuntimeError("New fuse connection directory not found ({0})".format(new_conns))
        elif len(new_conns) > 1:
            raise RuntimeError("Unexpectedly numerous fuse connections {0}".format(new_conns))
        else:
            self._fuse_conn = new_conns[0]


class LocalCephManager(object):
    def __init__(self):
        self.controller = LocalRemote()

    def find_remote(self, daemon_type, daemon_id):
        """
        daemon_type like 'mds', 'osd'
        daemon_id like 'a', '0'
        """
        return LocalRemote()

    def raw_cluster_cmd(self, *args):
        """
        args like ["osd", "dump"}
        return stdout string
        """
        proc = self.controller.run(["./ceph"] + list(args))
        return proc.stdout.getvalue()

    def raw_cluster_cmd_result(self, *args):
        """
        like raw_cluster_cmd but don't check status, just return rc
        """
        proc = self.controller.run(["./ceph"] + list(args), check_status=False)
        return proc.exitstatus

    def admin_socket(self, daemon_type, daemon_id, command, check_status = True):
        return self.controller.run(
            args = ["./ceph", "daemon", "{0}.{1}".format(daemon_type, daemon_id)] + command, check_status=check_status
        )

    #FIXME: copypasta
    def get_mds_status(self, mds):
        """
        Run cluster commands for the mds in order to get mds information
        """
        out = self.raw_cluster_cmd('mds', 'dump', '--format=json')
        j = json.loads(' '.join(out.splitlines()[1:]))
        # collate; for dup ids, larger gid wins.
        for info in j['info'].itervalues():
            if info['name'] == mds:
                return info
        return None

    #FIXME: copypasta
    def get_mds_status_by_rank(self, rank):
        """
        Run cluster commands for the mds in order to get mds information
        check rank.
        """
        j = self.get_mds_status_all()
        # collate; for dup ids, larger gid wins.
        for info in j['info'].itervalues():
            if info['rank'] == rank:
                return info
        return None


class LocalFilesystem(Filesystem):
    def __init__(self):
        # Deliberately skip calling parent constructor

        self.admin_remote = LocalRemote()

        # FIXME: learn this instead of assuming just a single MDS
        self.mds_ids = ["a"]

        self.mon_manager = LocalCephManager()

        self.mds_daemons = dict([(id_, LocalDaemon("mds", id_)) for id_ in self.mds_ids])

        self.client_remote = LocalRemote()

        self._ctx = None

    def get_pgs_per_fs_pool(self):
        # FIXME: assuming there are 3 OSDs
        return 3 * int(self.get_config('mon_pg_warn_min_per_osd'))

    def get_config(self, key, service_type=None):
        if service_type is None:
            service_type = 'mon'

        # FIXME hardcoded vstart service IDs
        service_id = {
            'mon': 'a',
            'mds': 'a',
            'osd': '0'
        }[service_type]

        return self.json_asok(['config', 'get', key], service_type, service_id)[key]

    def set_ceph_conf(self, subsys, key, value):
        # FIXME: unimplemented
        pass

    def clear_ceph_conf(self, subsys, key):
        # FIXME: unimplemented
        pass

    def clear_firewall(self):
        # FIXME: unimplemented
        pass

def exec_test():
    from unittest import suite, loader, case
    import unittest

    mount = LocalFuseMount("a", "/tmp/mnt.a")
    filesystem = LocalFilesystem()

    from tasks.cephfs_test_runner import DecoratingLoader

    class LogStream(object):
        def __init__(self):
            self.buffer = ""

        def write(self, data):
            self.buffer += data
            if "\n" in self.buffer:
                lines = self.buffer.split("\n")
                for line in lines[:-1]:
                    log.info(line)
                self.buffer = lines[-1]

        def flush(self):
            pass

    decorating_loader = DecoratingLoader({
        "ctx": None,
        "mounts": [mount],
        "fs": filesystem
    })

    # TODO let caller say what modules they want
    modules = ["tasks.cephfs.test_forward_scrub"]
    log.info("Executing modules: {0}".format(modules))
    module_suites = []
    for mod_name in modules:
        # Test names like cephfs.test_auto_repair
        module_suites.append(decorating_loader.loadTestsFromName(mod_name))
    overall_suite = suite.TestSuite(module_suites)

    result_class = unittest.TextTestResult
    fail_on_skip = False
    class LoggingResult(result_class):
        def startTest(self, test):
            log.info("Starting test: {0}".format(self.getDescription(test)))
            return super(LoggingResult, self).startTest(test)

        def addSkip(self, test, reason):
            if fail_on_skip:
                # Don't just call addFailure because that requires a traceback
                self.failures.append((test, reason))
            else:
                super(LoggingResult, self).addSkip(test, reason)

    # Execute!
    result = unittest.TextTestRunner(
        stream=LogStream(),
        resultclass=LoggingResult,
        verbosity=2,
        failfast=True).run(overall_suite)

    if not result.wasSuccessful():
        result.printErrors()  # duplicate output at end for convenience

        bad_tests = []
        for test, error in result.errors:
            bad_tests.append(str(test))
        for test, failure in result.failures:
            bad_tests.append(str(test))

        raise RuntimeError("Test failure: {0}".format(", ".join(bad_tests)))

    print result

if __name__ == "__main__":
    exec_test()