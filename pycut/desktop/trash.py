"""Move an exact local path through the macOS Trash API; never permanently unlink it."""
from __future__ import annotations

import ctypes as c
import os
from pathlib import Path
import sys


def move_to_trash(path: Path) -> str:
    """Return the OS-chosen Trash path, or raise while leaving the source in place."""
    if sys.platform != "darwin":
        raise OSError("当前系统尚未支持移到回收站；文件和记录已保留")
    # ctypes keeps this macOS-only feature independent of the CLI's dependencies.
    c.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    objc = c.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes, objc.objc_getClass.restype = [c.c_char_p], c.c_void_p
    objc.sel_registerName.argtypes, objc.sel_registerName.restype = [c.c_char_p], c.c_void_p
    def message(receiver, selector, result=c.c_void_p, arguments=(), values=()):
        call = c.CFUNCTYPE(result, c.c_void_p, c.c_void_p, *arguments)(("objc_msgSend", objc))
        return call(receiver, objc.sel_registerName(selector), *values)
    pool = message(message(objc.objc_getClass(b"NSAutoreleasePool"), b"alloc"), b"init")
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [c.c_void_p, c.c_char_p, c.c_long, c.c_bool]
    cf.CFURLCreateFromFileSystemRepresentation.restype = c.c_void_p
    cf.CFRelease.argtypes = [c.c_void_p]
    cf.CFRelease.restype = None
    raw = os.fsencode(path)
    url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), path.is_dir())
    try:
        if not url:
            raise OSError("无法读取文件路径")
        manager = message(objc.objc_getClass(b"NSFileManager"), b"defaultManager")
        destination, error = c.c_void_p(), c.c_void_p()
        ok = message(manager, b"trashItemAtURL:resultingItemURL:error:", c.c_bool,
                     (c.c_void_p, c.POINTER(c.c_void_p), c.POINTER(c.c_void_p)),
                     (url, c.byref(destination), c.byref(error)))
        if not ok:
            description = message(error, b"localizedDescription") if error else None
            detail = message(description, b"UTF8String", c.c_char_p) if description else None
            raise OSError(detail.decode("utf-8") if detail else "系统未能将文件移到废纸篓")
        location = message(message(destination, b"path"), b"UTF8String", c.c_char_p)
        return os.fsdecode(location) if location else ""
    finally:
        if url:
            cf.CFRelease(url)
        message(pool, b"drain", None)
