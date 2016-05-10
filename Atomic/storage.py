import errno
import shutil
import selinux
import requests
import os, sys

from . import util
from .Export import export_docker
from .Import import import_docker
from .util import NoDockerDaemon

try:
    from subprocess import DEVNULL  # pylint: disable=no-name-in-module
except ImportError:
    DEVNULL = open(os.devnull, 'wb')

try:
    from . import Atomic
except ImportError:
    from atomic import Atomic

def list_pvs(vgroup):
    res = [ ]
    for l in util.check_output([ "pvs", "--noheadings", "-o",  "vg_name,pv_name" ]).splitlines():
        fields = l.split()
        if len(fields) == 2 and fields[0] == vgroup:
            res.append(fields[1])
    return res

def query_pvs(pv, fields):
    return util.check_output([ "pvs", "--noheadings", "-o",  fields, "--unit", "b", pv ]).split()

def list_parents(dev):
    return util.check_output([ "lsblk", "-snlp", "-o", "NAME", dev ]).splitlines()[1:]

def list_children(dev):
    return util.check_output([ "lsblk", "-nlp", "-o", "NAME", dev ]).splitlines()[1:]

def get_dss_vgroup():
    vgroup = util.sh_get_var_in_file("/etc/sysconfig/docker-storage-setup", "VG", "")
    if vgroup == "":
        root_dev = None
        for l in open("/proc/mounts", "r").readlines():
            fields = l.split()
            if fields[1] == "/" and fields[0].startswith("/dev"):
                vgroup = util.check_output([ "lvs", "--noheadings", "-o",  "vg_name", fields[0]]).strip()
    return vgroup

class Storage(Atomic):
    def reset(self):
        root = "/var/lib/docker"
        try:
            self.d.info()
            raise ValueError("Docker daemon must be stop before resetting storage")
        except requests.exceptions.ConnectionError as e:
            pass

        util.check_call(["docker-storage-setup", "--reset"], stdout=DEVNULL)
        util.call(["umount", root + "/devicemapper"], stderr=DEVNULL)
        util.call(["umount", root + "/overlay"], stderr=DEVNULL)
        shutil.rmtree(root)
        os.mkdir(root)
        try:
            selinux.restorecon(root.encode("utf-8"))
        except:
            selinux.restorecon(root)

    def reduce(self):
        vgroup = get_dss_vgroup()
        dss_conf = "/etc/sysconfig/docker-storage-setup"
        for pv in list_pvs(vgroup):
            if query_pvs(pv, "pv_used")[0][:-1] == '0':
                util.check_call([ "vgreduce", vgroup, pv ])
                util.check_call([ "wipefs", "-a", pv ])
                parents = list_parents(pv)
                util.sh_modify_var_in_file(dss_conf, "DEVS",
                                           lambda old: util.sh_set_del(old, parents))
                if len(parents) == 1:
                    children = list_children(parents[0])
                    if len(children) == 1 and children[0] == pv:
                        util.check_call([ "wipefs", "-a", parents[0] ])

    def add(self):
        dss_conf = "/etc/sysconfig/docker-storage-setup"
        dss_conf_bak = "/etc/sysconfig/docker-storage-setup" + ".bkp"

        try:
            shutil.copyfile(dss_conf, dss_conf_bak)
            util.sh_modify_var_in_file(dss_conf, "DEVS",
                                       lambda old: util.sh_set_add(old, self.args.device))
            if util.call(["docker-storage-setup"]) != 0:
                os.rename(dss_conf_bak, dss_conf)
                util.call(["docker-storage-setup"])
                raise ValueError("Not all devices could be added")
        finally:
            if os.path.exists(dss_conf_bak):
                os.remove(dss_conf_bak)

    def Export(self):
        try:
            export_docker(self.args.graph, self.args.export_location, self.force)
        except requests.exceptions.ConnectionError:
            raise NoDockerDaemon()

    def Import(self):
        self.ping()
        try:
            import_docker(self.args.graph, self.args.import_location)
        except requests.exceptions.ConnectionError:
            raise NoDockerDaemon()
