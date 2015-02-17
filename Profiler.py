# Copyright (C) 2015 AG Projects. See LICENSE for details.
#

import cProfile
import os


do_profile = 'BLINK_DO_PROFILE' in os.environ
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

