#!/usr/bin/env python3
import os
import subprocess
from pbxproj import XcodeProject, PBXCopyFilesBuildPhase
from pbxproj.pbxextensions.ProjectFiles import FileOptions
from pbxproj.pbxsections import PBXBuildFile


project = XcodeProject.load('../Blink.xcodeproj/project.pbxproj')

sections = project.objects.get_sections()
targets = project.objects.get_targets()
valid_targets = ['Blink']

core_deps = []
cmd = "otool -L Resources/lib/sipsimple/core/_core.cpython-39-darwin.so"
p = subprocess.Popen(cmd.split(" "), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
out, err = p.communicate()
libs = out.decode().split('\n')
for l in libs:
    f = l.split(" ")[0].strip()
    if 'executable_path' not in f:
        continue
    core_deps.append(f.split("/")[-1])

file_options = FileOptions(weak=True)

xcode_libs = []
for target in targets:
    if target.name not in valid_targets:
        continue
    print('Checking Xcode target %s' % target.name)
    build_phases = project.objects.get_buildphases_on_target(target_name=target.name)
    for b in build_phases:
        f = b[1]
        if isinstance(f, PBXCopyFilesBuildPhase):
            if f.name != 'Copy Frameworks':
                continue
            print("%s: %d files will be copied" % (f.name, len(f.files)))
            for file in f.files:
                comments = file._get_comment().split(' ')
                filename = comments[0]
                xcode_libs.append(filename)
                #print(f'Target {target.name} Phase {f.name} Filename: {filename}')
                if not os.path.exists('Frameworks/%s' % filename) and filename in core_deps:
                    print(f'{filename} is missing from Frameworks/ folder!')
               

new_deps = []
i = 1
for dep in core_deps:
    if dep not in xcode_libs:
        if os.path.exists('Frameworks/%s' % filename):
            print(f"{i} You must add {dep} to the 'Copy Frameworks' phase")
        else:
            print(f'{dep} core dependency is missing from Frameworks/{dep}')
        
        i = i + 1
        
