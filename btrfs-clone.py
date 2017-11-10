#! /usr/bin/env python

# btrfs-clone: clones a btrfs file system to another one
# Copyright (C) 2017 Martin Wilck
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

# This program clones an existing BTRFS file system to a new one,
# cloning each subvolume in order.
#
# Idea thanks to Thomas Luzat
# (https://superuser.com/questions/607363/how-to-copy-a-btrfs-filesystem)
#
# Usage:
#      btrfs-clone <mount-point-of-existing-FS> <mount-point-of-new-FS>
#
# Example for real-world use:
#
# mkfs.btrfs /dev/sdb1
# mkdir /mnt/new
# mount /dev/sdb1 /mnt/new
# btrfs-clone / /mnt/new
#
# The new file system should be large enough to receive all data
# from the old one. The tool does not check this.
#
# The new filesystem should ideally be newly created, and have a distinct
# UUID from the one to be cloned. The --force option allows to attempt
# cloning even if this is not the case.
#
# The two file systems don't need to be mounted by the toplevel subvolume, the
# program will remount the top subvolumes on temporary mount points.
#
# Error handling is pretty basic, the program will only refuse to overwrite
# a file system with the same UUID. For other problems, this program relies
# on the btrfs tools to fail, and will abort if that happens. The tool will
# not attempt to continue after cloning a certain subvolume failed.
#
# During the cloning, all subvolumes of the origin FS are set to read-only.
#
# Cloning by strategies: "parent" vs. "snapshot"
# ==============================================
#
# Try to clone using the closed "neighbour" snapshot rather than
# the actual parent
#
# Consider the following typical topology, decreasing chronological order,
# where the current fs tree has been snapshotted several times in the past:
#
#    current - snap4 - snap3 -snap2 - snap1
#
# With "parent" topology, we'd clone "current" and after that the snapshots,
# using "current" as "parent" ("-p" option to btrfs-send) for every snapshot.
# But obviously the similarity between snap1 and snap2 will be much higher then
# between snap1 and current. This will cause a waste of disk space, as shared
# extents can't be used efficiently.
#
# "snapshot" strategy uses the "neighbour snapshot" as parent.
# We clone "current" first, then snap4 with -p current, snap3 with -p snap4,
# etc. This ensures that differences are as small as possible.
#
# This has the side effect that the parent-child relationships (expressed by
# parent_uuid) are different in the cloned file system compared to the original
# (snap3 will appear to be a snapshot of snap4, whereas it was a snapshot of
# current in the original FS). Also, file systems will not be cloned in theq
# order of their creation, thus when we clone a subvolume, we can't be sure that
# its parent in the filesystem tree (btrfs "parent_id", don't confuse with
# "parent_uuid") has already been transferred. Therefore we clone into a flat
# directory first (option "snapshot_base" or random name). When all subvols are
# cloned, they can be moved into their desired fs tree position.
#
# The --toplevel option
# =====================
#
# The toplevel "subvolume" of a BTRFS file system can't be cloned with
# send/receive. It's only possible to create a snapshot of the toplevel
# FS and clone that. Obviously, the cloned snapshot in the new FS will be
# distinct from the toplevel of the new FS. By default, this tool moves the
# content of the cloned snapshot to the toplevel of the new FS and deletes
# the snapshot. If this is not desired, the --toplevel option can be used.
# It causes the tool to keep the cloned snapshot volume and create all
# subvolumes relative to it.

import sys
import re
import os
import subprocess
import atexit
import tempfile
import gzip
from uuid import uuid4
from argparse import ArgumentParser
from stat import ST_DEV

opts = None
VERBOSE = []

def randstr():
    return str(uuid4())[-12:]

def check_call(*args, **kwargs):
    if opts.verbose:
        print (" ".join(args[0]))
    if not opts.dry_run:
        subprocess.check_call(*args, **kwargs)

def prop_get_ro(path, yesno):
    info = subprocess.check_output([opts.btrfs, "property", "get", "-ts",
                                    path, "ro"])
    info = info.rstrip()
    return info == "ro=true"

def prop_set_ro(path, yesno):
    check_call([opts.btrfs, "property", "set", "-ts",
                path, "ro", "true" if yesno else "false"])

class Subvol:
    class NoSubvol(ValueError):
        pass
    class BadId(RuntimeError):
        pass
    class MissingAttr(RuntimeError):
        pass

    def __init__(self, mnt, line):
        args = line.split()
        if len(args) != 4:
            raise self.NoSubvol(line)
        self.mnt = mnt
        try:
            self.id = int(args[0])
        except ValueError:
            raise self.NoSubvol(line)
        self.gen = int(args[1])
        self.toplevel = int(args[2])
        self.path = args[3]
        self.check_show()

    def check_show(self):
        info = subprocess.check_output([opts.btrfs, "subvolume", "show",
                                        "%s/%s" % (self.mnt, self.path)])
        for line in info.split("\n"):
            try:
                k, v = line.split(":", 1)
            except ValueError:
                continue
            k = k.strip()
            v = v.strip()
            if k == "UUID":
                self.uuid = v
            elif k == "Parent UUID":
                self.parent_uuid = v
                if self.parent_uuid == "-":
                    self.parent_uuid = None
            elif k == "Subvolume ID":
                if self.id != int(v):
                    raise self.BadId(v)
            elif k == "Parent ID":
                self.parent_id = int(v)
            elif k == "Gen at creation":
                self.ogen = int(v)
            elif k == "Flags":
                self.ro = (v.find("readonly") != -1)

        for attr in ("parent_id", "parent_uuid", "ro", "ogen", "uuid"):
            if not hasattr(self, attr):
                raise self.MissingAttr("%s: no %s" % (self, attr))

    def __str__(self):
        return (("subvol %d at \"%s\"") % (self.id, self.path))

    def longstr(self):
        return (("subvol %d gen %d->%d %s UUID=%s ro:%s" +
                 "\n\tParent: %d %s") %
                (self.id, self.ogen, self.gen, self.path, self.uuid, self.ro,
                 self.parent_id, self.parent_uuid))

    def get_mnt(self, mnt = None):
        if mnt is None:
            return self.mnt
        return mnt

    def get_path(self, mnt = None):
        return "%s/%s" % (self.get_mnt(mnt), self.path)

    def get_ro(self, mnt = None):
        return prop_get_ro(self.get_path(mnt))

    def ro_str(self, mnt = None, prefix=""):
        return ("%s%s (%s): %s" % (prefix, self.path, self.ro,
                                   self.get_ro(mnt)))

    def set_ro(self, yesno, mnt = None):
        # Never change a subvol that was already ro
        if self.ro:
            return
        return prop_set_ro(self.get_path(mnt), yesno)

def get_subvols(mnt):
    vols = subprocess.check_output([opts.btrfs, "subvolume", "list",
                                    "-t", "--sort=ogen",
                                    mnt])
    svs = []
    for line in vols.split("\n"):
        try:
            sv = Subvol(mnt, line)
        except Subvol.NoSubvol:
            pass
        except:
            raise
        else:
            svs.append(sv)
    return svs

def umount_root_subvol(dir):
    try:
        subprocess.check_call(["umount", "-l", dir])
        os.rmdir(dir)
    except:
        pass

def mount_root_subvol(mnt):
    td = tempfile.mkdtemp()
    info = subprocess.check_output([opts.btrfs, "filesystem", "show", mnt])
    line = info.split("\n")[0]
    uuid = re.search(r"uuid: (?P<uuid>[-a-f0-9]*)", line).group("uuid")
    subprocess.check_call(["mount", "-o", "subvolid=5", "UUID=%s" % uuid, td])
    atexit.register(umount_root_subvol, td)
    return (uuid, td)

def set_all_ro(yesno, subvols, mnt = None):
    if yesno:
        l = subvols
    else:
        l = reversed(subvols)

    for sv in l:
        try:
            sv.set_ro(yesno, mnt = mnt)
        except subprocess.CalledProcessError:
            if not yesno:
                print ("Error setting ro=%s for %s: %s") % (
                    yesno, sv.path, sys.exc_info()[1])
                continue
            else:
                raise

def do_send_recv(old, new, send_flags=[]):
    send_cmd = ([opts.btrfs, "send"] + VERBOSE + send_flags + [old])
    recv_cmd = ([opts.btrfs, "receive"] + VERBOSE + [new])

    if opts.verbose > 1:
        name = new.replace("/", "-")
        recv_name = "btrfs-recv-%s.log.gz" % name
        send_name = "btrfs-send-%s.log.gz" % name
        recv_log = gzip.open(recv_name, "wb")
        send_log = gzip.open(send_name, "wb")
    else:
        recv_log = subprocess.PIPE
        send_log = subprocess.PIPE

    if opts.verbose:
        print ("%s |\n\t %s" % (" ".join(send_cmd), " ".join(recv_cmd)))
    if opts.dry_run:
        return

    try:
        send = subprocess.Popen(send_cmd, stdout=subprocess.PIPE,
                                stderr=send_log)
        recv = subprocess.Popen(recv_cmd, stdin=send.stdout,
                                stderr=recv_log)
        send.stdout.close()
        recv.communicate()
        send.wait()
    finally:
        if opts.verbose > 1:
            recv_log.close()
            send_log.close()

    if recv.returncode != 0 or send.returncode != 0:
        if opts.verbose > 1:
            print ("please check %s and %s" % (send_name, recv_name))
        else:
            if send.returncode != 0:
                print ("Error in send:\n%s" % send.stderr.read())
            if recv.returncode != 0:
                print ("Error in recv:\n%s" % recv.stderr.read())
        raise RuntimeError("Error in send/recv for %s -> %s" % (old, new))

def send_root(old, new):
    name = randstr()
    old_snap = "%s/%s" % (old, name)
    new_snap = "%s/%s" % (new, name)
    subprocess.check_call([opts.btrfs, "subvolume", "snapshot", "-r", old, old_snap])
    atexit.register(subprocess.check_call,
                    [opts.btrfs, "subvolume", "delete", old_snap])
    do_send_recv(old_snap, new)
    check_call([opts.btrfs, "property", "set", new_snap, "ro", "false"])

    dir = old_snap if opts.dry_run else new_snap
    dev = os.lstat(dir)[ST_DEV]
    if opts.toplevel:
        for el in os.listdir(dir):
            path = "%s/%s" %(dir, el)
            dev1 = os.lstat(path)[ST_DEV]
            if dev != dev1:
                continue
            # Can' use os.rename here (cross device link)
            check_call(["mv", "-f", "-t", new] +
                       (["-v"] if opts.verbose else []) + [path])
        check_call([opts.btrfs, "subvolume", "delete", new_snap])
        ret = new
    else:
        ret = new_snap
        print ("top level subvol in clone is: %s" % name)
    return ret

def send_subvol_parent(subvol, get_parents, old, new):
    ancestors = [[ "-c", x.get_path(old) ] for x in get_parents(subvol)]
    c_flags = [x for anc in ancestors for x in anc]
    if ancestors:
        p_flags = [ "-p", ancestors[0][1] ]
    else:
        p_flags = []
    do_send_recv(subvol.get_path(old), os.path.dirname(subvol.get_path(new)),
                 send_flags = p_flags + c_flags)


def send_subvols_parent(old_mnt, new_mnt, subvols):
    get_parents = parents_getter({ x.uuid: x for x in subvols })
    new_subvols = []

    for sv in subvols[:2]:
        send_subvol_parent(sv, get_parents, old_mnt, new_mnt)
        sv.set_ro(False, new_mnt)
        #if not opts.dry_run:
        #    print (sv.ro_str(new_mnt))
        new_subvols.append(sv)

def svdir_getter(base):
    def _getter(sv):
        return "%s/%d" % (base, sv.id)
    return _getter

def send_subvol_snap(sv, subvols, old, dir_fn, parent=None):

    dir = dir_fn(sv)
    if not opts.dry_run and not os.path.isdir(dir):
        os.mkdir(dir)
    path = sv.get_path(old)

    if parent is not None:
        flags = [ "-p", parent.get_path(old), "-c", parent.get_path(old)]
    else:
        flags = []

    newpath = "%s/%s" % (dir, os.path.basename(path))
    if os.path.isdir(newpath):
        print ("%s exists, not sending" % newpath)
    else:
        do_send_recv(path, dir, send_flags = flags)
        if not sv.ro and not opts.dry_run:
            prop_set_ro(newpath, False)

    snaps = [x for x in subvols if x.parent_uuid == sv.uuid]
    snaps.sort(reverse = True, key = lambda x: (x.ogen, x.id))

    prev = sv
    for snap in snaps:
        send_subvol_snap(snap, subvols, old, dir_fn, parent=prev)
        prev = snap

def move_to_tree_pos(sv, new, dir_fn, done):
    goal = sv.get_path(new)
    last = os.path.basename(goal)
    dir = dir_fn(sv)
    cur = "%s/%s" % (dir, last)

    if opts.dry_run:
        check_call(["mv", "-f", cur, os.path.dirname(goal)])
        return

    if not os.path.isdir(cur):
        if os.path.isdir(goal):
            print ("ah, %s already moved" % goal)
            return True
        else:
            print ("ERROR: %s was not created" % cur)
            return False
    elif sv.parent_id == 5 or sv.parent_id in done:
        if sv.ro:
            prop_set_ro(cur, False)
        try:
            check_call(["mv", "-f", cur, os.path.dirname(goal)])
        finally:
            if sv.ro and os.path.isdir(cur):
                try:
                    prop_set_ro(cur, True)
                except:
                    pass
        try:
            os.rmdir(dir)
        except OSError:
            print ("Failed to remove %s (this is non-fatal)" % dir)
        done.add(sv.id)
        return True
    else:
        print ("Hmm, parent %d of %d not found" % (sv.parent_id, sv.id))
        return False


def send_subvols_snap(old, new, subvols):

    svbase = "%s/%s" % (new, opts.snap_base if opts.snap_base else randstr())
    if not opts.dry_run and not os.path.isdir(svbase):
        os.mkdir(svbase)
    dir_fn = svdir_getter(svbase)

    for sv in (x for x in subvols if x.parent_uuid is None):
        send_subvol_snap(sv, subvols, old, dir_fn)

    subvols.sort(key = lambda x: (x.parent_id, x.id))
    done = set()
    for sv in subvols:
        move_to_tree_pos(sv, new, dir_fn, done)

    if not opts.dry_run:
        try:
            os.rmdir(svbase)
        except OSError:
            print ("Failed to remove %s (this is non-fatal)" % svbase)

def send_subvols(old_mnt, new_mnt):
    subvols = get_subvols(old_mnt)
    atexit.register(set_all_ro, False, subvols, old_mnt)
    set_all_ro(True, subvols, old_mnt)

    if opts.strategy == "parent":
        send_subvols_parent(old_mnt, new_mnt, subvols)
    elif opts.strategy == "snapshot":
        send_subvols_snap(old_mnt, new_mnt, subvols)

def parents_getter(lookup):
    def _getter(x):
        p = []
        while x.parent_uuid is not None:
            try:
                x = lookup[x.parent_uuid]
            except KeyError:
                break
            else:
                p.append(x)
        return p
    return _getter

def make_args():
    ps = ArgumentParser()
    ps.add_argument("-v", "--verbose", action='count', default=0)
    ps.add_argument("-B", "--btrfs", default="btrfs")
    ps.add_argument("-n", "--dry-run", action='store_true')
    ps.add_argument("-s", "--strategy", default="snapshot",
                    choices=["parent", "snapshot"])
    ps.add_argument("--snap-base")
    ps.add_argument("-t", "--toplevel", action='store_false',
                    help="clone toplevel into a subvolume")
    ps.add_argument("old")
    ps.add_argument("new")
    return ps

def parse_args():
    global opts
    global VERBOSE

    ps = make_args()
    opts = ps.parse_args()
    if opts.verbose is not None:
        VERBOSE = ["-v"] * opts.verbose

if __name__ == "__main__":
    parse_args()

    (old_uuid, old_mnt) = mount_root_subvol(opts.old)
    print ("OLD btrfs %s mounted on %s" % (old_uuid, old_mnt))
    (new_uuid, new_mnt) = mount_root_subvol(opts.new)
    if (old_uuid == new_uuid):
        raise RuntimeError("%s and %s are the same file system" %
                           (opts.old, opts.new))
    print ("NEW btrfs %s mounted on %s" % (new_uuid, new_mnt))

    new_mnt = send_root(old_mnt, new_mnt)
    send_subvols(old_mnt, new_mnt)
