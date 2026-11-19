"""Diagnose the black Netcup Screen without copying any display pixels.

Only emits process states, framebuffer geometry, window counts and error types.
No OAuth data, X11 cookies, user content or screenshots leave the VPS.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import re
import subprocess


EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
ROOT = Path('/opt/ai-agent-engine')
AUTH = Path('/run/ae-stage614-browser/Xauthority')


def run(args: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        x = subprocess.run(args, stdin=subprocess.DEVNULL, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, ''
    return x.returncode == 0, x.stdout


class WindowAttributes(ctypes.Structure):
    _fields_ = [
        ('x', ctypes.c_int), ('y', ctypes.c_int),
        ('width', ctypes.c_int), ('height', ctypes.c_int),
        ('border_width', ctypes.c_int), ('depth', ctypes.c_int),
        ('visual', ctypes.c_void_p), ('root', ctypes.c_ulong),
        ('klass', ctypes.c_int), ('bit_gravity', ctypes.c_int),
        ('win_gravity', ctypes.c_int), ('backing_store', ctypes.c_int),
        ('backing_planes', ctypes.c_ulong),
        ('backing_pixel', ctypes.c_ulong),
        ('save_under', ctypes.c_int), ('colormap', ctypes.c_ulong),
        ('map_installed', ctypes.c_int), ('map_state', ctypes.c_int),
        ('all_event_masks', ctypes.c_long), ('your_event_mask', ctypes.c_long),
        ('do_not_propagate_mask', ctypes.c_long),
        ('override_redirect', ctypes.c_int), ('screen', ctypes.c_void_p),
    ]


def x11_windows() -> dict[str, object]:
    os.environ['DISPLAY'] = ':14'
    os.environ['XAUTHORITY'] = str(AUTH)
    libname = ctypes.util.find_library('X11')
    if not libname:
        return {'connected': False, 'reason': 'libX11_missing'}
    x = ctypes.CDLL(libname)
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x.XDefaultScreen.restype = ctypes.c_int
    x.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XDisplayWidth.restype = ctypes.c_int
    x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XDisplayHeight.restype = ctypes.c_int
    x.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XRootWindow.restype = ctypes.c_ulong
    x.XQueryTree.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x.XQueryTree.restype = ctypes.c_int
    x.XGetWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(WindowAttributes),
    ]
    x.XGetWindowAttributes.restype = ctypes.c_int
    x.XFree.argtypes = [ctypes.c_void_p]
    x.XFree.restype = ctypes.c_int
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x.XCloseDisplay.restype = ctypes.c_int
    display = x.XOpenDisplay(b':14')
    if not display:
        return {'connected': False, 'reason': 'display_open_failed'}
    try:
        screen = x.XDefaultScreen(display)
        width = x.XDisplayWidth(display, screen)
        height = x.XDisplayHeight(display, screen)
        root = x.XRootWindow(display, screen)
        root_return = ctypes.c_ulong()
        parent_return = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        ok = x.XQueryTree(
            display, root, ctypes.byref(root_return),
            ctypes.byref(parent_return), ctypes.byref(children),
            ctypes.byref(count),
        )
        if not ok:
            return {'connected': True, 'query_tree_ok': False,
                    'screen_width': width, 'screen_height': height}
        viewable = 0
        large_viewable = 0
        for i in range(min(count.value, 500)):
            attrs = WindowAttributes()
            if x.XGetWindowAttributes(display, children[i],
                                      ctypes.byref(attrs)) and attrs.map_state == 2:
                viewable += 1
                if attrs.width >= 400 and attrs.height >= 300:
                    large_viewable += 1
        if children:
            x.XFree(children)
        return {
            'connected': True,
            'screen_width': width, 'screen_height': height,
            'direct_windows': count.value,
            'viewable_direct_windows': viewable,
            'large_viewable_direct_windows': large_viewable,
        }
    finally:
        x.XCloseDisplay(display)


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active production release changed')
    _, vt = run(['/usr/bin/fgconsole'])
    states = {unit: run(['systemctl', 'is-active', '--quiet', unit])[0]
              for unit in ('ae614-xorg.service', 'ae614-fix-openbox.service',
                           'ae614-fix-firefox.service')}
    services = all(run(['systemctl', 'is-active', '--quiet', s])[0]
                   for s in ('ae-controller.service', 'ae-backup.timer',
                             'ae-host-check.timer'))
    result = {
        'status': 'BLACK_SCREEN_READ_ONLY_DIAGNOSTIC',
        'console_vt': vt.strip() if vt.strip().isdigit() else 'unavailable',
        'graphical_units_active': states,
        'x11': x11_windows(),
        'graphics_card_present': Path('/dev/dri/card0').exists(),
        'production_healthy': services,
        'production_changed': False,
        'credentials_accessed': False,
        'model_calls': False,
    }
    print('STAGE6_14_VISUAL ' + json.dumps(result, sort_keys=True),
          flush=True)


if __name__ == '__main__':
    main()
