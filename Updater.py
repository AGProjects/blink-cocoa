
from Foundation import NSBundle, NSObject
import objc


class Updater(NSObject):
    sp = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        NSBundle.loadNibNamed_owner_("Updater", self)
        return self

