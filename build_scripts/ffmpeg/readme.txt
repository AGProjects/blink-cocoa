sudo mkdir -p /opt/local/ports/multimedia/ffmpeg
cd /opt/local/ports/multimedia/ffmpeg

#/opt/local/var/macports/sources/rsync.macports.org/macports/release/tarballs/ports/multimedia/ffmpeg/Portfile

sudo cp $(port file ffmpeg) .

Edit Portfile

Add:

file:///opt/local/ports

at the beginning of /opt/local/etc/macports/sources.conf

sudo portindex /opt/local/ports
sudo port sync

sudo port install ffmpeg +universal
ffmpeg -buildconf

To restore original port file

sudo rm -rf /opt/local/ports/multimedia/ffmpeg

or edit /opt/local/etc/macports/sources.conf

sudo port sync

