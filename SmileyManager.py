# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import os
from application.python.types import Singleton

from util import escape_html


SMILEY_STYLE="MSN"


class SmileyManager(object):
    __metaclass__ = Singleton
    
    def __init__(self):
        self.smiley_directory = None
        self.icon = None
        self.smileys = {}
        self.smileys_html = {}
        self.smiley_keys = []
        self.theme = None
        

    def load_theme(self, smiley_theme_directory, name="default"):
        self.smiley_directory = smiley_theme_directory
        self.theme = name
        self.icon = None

        in_header = True
        found = False

        f = open(os.path.join(self.smiley_directory, self.theme, "theme"), "r")

        for line in f:
            line = line.strip()
            if not line or line[0] == "#":
                continue

            if in_header:
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k == "Icon":
                        self.icon = v
                    continue

                if line[0] == "[":
                    in_header = False
                else:
                    continue
            if line == "[%s]"%SMILEY_STYLE:
                if found:
                    break
                found = True
                continue
            elif line.startswith("["):
                if found:
                    break

            if found:
                line = line.replace("\\\\", "\\")

                toks = [s.strip() for s in line.split()]
                if len(toks) >= 2:
                    file = toks[0]
                    for text in toks[1:]:
                        self.smileys[text] = file
                    self.smiley_keys.append(toks[1])

        f.close()
    
        self.smileys_html = {}
        for k, v in self.smileys.iteritems():
            # pre-escape the smiley so that it matches escaped text later
            ek = escape_html(k)
            self.smileys_html[ek] = "<img src='file:%s' class='smiley' />"%(self.get_smiley(k))

    def get_smiley(self, text):
        if self.smileys.has_key(text):
            return os.path.join(self.smiley_directory, self.theme, self.smileys[text])
        return None

    
    def subst_smileys_html(self, text):
        items = self.smileys_html.items()
        items.sort(lambda a,b:cmp(a[0],b[0]))
        items.reverse() # reverse list so that longer ones are substituted 1st
        for k, v in items:
            text = text.replace(k, v)
        return text


    def get_smiley_list(self):
        l = []
        for text in self.smiley_keys:
            l.append((text, self.get_smiley(text)))
        return l


