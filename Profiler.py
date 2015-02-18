# Copyright (C) 2015 AG Projects. See LICENSE for details.
#

import cProfile
import os

from Foundation import NSUserDefaults


do_profile = NSUserDefaults.standardUserDefaults().boolForKey_("EnableProfiler")
pr = None


def start():
    global do_profile, pr
    if not do_profile:
        return
    assert pr is None
    pr = cProfile.Profile()
    pr.enable()


def stop(filename=None):
    global do_profile, pr
    if not do_profile:
        return
    assert pr is not None
    pr.disable()
    if filename:
        pr.dump_stats(filename)

