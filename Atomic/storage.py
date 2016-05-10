import errno
import shutil
import selinux
import requests
import os, sys, re

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

def modify_sh_var_in_text(text, var, modifier, default=""):
    pattern = '^[ \t]*%s[ \t]*=[ \t]*"(.*)"[ \t]*$' % re.escape(var)
    found = [ False ]
    def sub(match):
        found[0] = True
        return var + '="' + modifier(match.group(1)) + '"'
    new_text = re.sub(pattern, sub, text, flags=re.MULTILINE)
    if found[0]:
        return new_text
    else:
        return text + '\n' + var + '="' + modifier(default) + '"\n'

def modify_sh_var_in_file(path, var, modifier, default=""):
    if os.path.exists(path):
        with open(path, "r") as f:
            text = f.read()
    else:
        text = ""
    with open(path, "w") as f:
        f.write(modify_sh_var_in_text(text, var, modifier, default))

def sh_set_add(a, b):
    return " ".join(list(set(a.split()) | set(b)))

def sh_set_del(a, b):
    return " ".join(list(set(a.split()) - set(b)))

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
        shutil.rmtree(root)
        os.mkdir(root)
        try:
            selinux.restorecon(root.encode("utf-8"))
        except:
            selinux.restorecon(root)

    def reduce(self):
        vgroup = util.check_output(["docker-storage-setup", "--show-vgroup"]).strip()
        dss_conf = "/etc/sysconfig/docker-storage-setup"
        for pv in list_pvs(vgroup):
            if query_pvs(pv, "pv_used")[0][:-1] == '0':
                util.check_call([ "vgreduce", vgroup, pv ])
                util.check_call([ "wipefs", "-a", pv ])
                parents = list_parents(pv)
                modify_sh_var_in_file(dss_conf, "DEVS", lambda old: sh_set_del(old, parents))
                if len(parents) == 1:
                    children = list_children(parents[0])
                    if len(children) == 1 and children[0] == pv:
                        util.check_call([ "wipefs", "-a", parents[0] ])

    def add(self):
        dss_conf = "/etc/sysconfig/docker-storage-setup"
        dss_conf_bak = "/etc/sysconfig/docker-storage-setup" + ".bkp"

        if os.path.exists(dss_conf):
            shutil.copyfile(dss_conf, dss_conf_bak)
        elif os.path.exists(dss_conf_bak):
            os.remove(dss_conf_bak)
        modify_sh_var_in_file(dss_conf, "DEVS", lambda old: sh_set_add(old, self.args.device))
        if util.call(["docker-storage-setup"]) != 0:
            if os.path.exists(dss_conf_bak):
                os.rename(dss_conf_bak, dss_conf)
            else:
                os.remove(dss_conf)
            util.call(["docker-storage-setup"])
            raise ValueError("Not all devices could be added")
        else:
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
