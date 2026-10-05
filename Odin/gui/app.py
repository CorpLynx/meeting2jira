"""Top-level Odin Tkinter application"""

from __future__ import annotations


import ctypes
import logging
import queue
import sys
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from .. import __version__
from . import theme
from .backend import AppBackend
from .views import VIEW_FACTORIES
from .widgets import NavButton, ScrollableFrame

log = logging.getLogger(__name__)

ASSET_DIR = PATH(__file__)