#!/bin/sh
rsync -avz --progress ../../Distribution/Blink.dmg cdr-adm:/var/www/prj/dnshosting/blink/Releases/MacOSX/
rsync -avz ../../ReleaseNotes/BlinkAppcast.xml cdr-adm:/var/www/prj/dnshosting/blink/
rsync -avz ../../ReleaseNotes/changelog-beta.html cdr-adm:/var/www/prj/dnshosting/blink/ReleaseNotes/
