"""Local OS account identity, independent of inherited shell variables."""

import os
import pwd


def current_username() -> str:
    return pwd.getpwuid(os.getuid()).pw_name
