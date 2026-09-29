"""Open local media without following links in a writable music tree."""

import os
import stat
from pathlib import Path
from typing import BinaryIO


def open_regular(path: Path) -> BinaryIO:
    """Pin every directory before opening the next component, refusing symlinks.

    A resolve/check followed by open would let a writer swap a directory or file
    between the check and the read. Directory descriptors keep that walk stable.
    """
    path = path.absolute()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("media must be a regular file")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(directory)
