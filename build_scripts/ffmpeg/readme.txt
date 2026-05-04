PJMEDIA can make use of the following FFMPEG development components:

libavutil
libavformat
libavcodec
libavdevice
libswscale

This document described how to build a strip-down version of ffmpeg with
only the needed components.

sudo mkdir -p /opt/local/ports/multimedia/ffmpeg

#/opt/local/var/macports/sources/rsync.macports.org/macports/release/tarballs/ports/multimedia/ffmpeg/Portfile

# The original Portfile
#sudo cp $(port file ffmpeg) /opt/local/ports/multimedia/ffmpeg

cd /opt/local/ports/multimedia/ffmpeg
Edit Portfile if different then this changed version

# Locally changed Portfile
sudo cp Portfile /opt/local/ports/multimedia/ffmpeg

Add file:///opt/local/ports at the beginning of /opt/local/etc/macports/sources.conf

sudo portindex /opt/local/ports
sudo port sync

sudo port install ffmpeg +universal

Check build command

To check if the right version has been used:

ffmpeg -buildconf

To restore original port file

sudo rm -rf /opt/local/ports/multimedia/ffmpeg

or edit /opt/local/etc/macports/sources.conf
sudo port sync

