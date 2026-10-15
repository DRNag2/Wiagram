# wiagram turns text wiring files into fridge diagrams

from .model import Design, ParseError
from .parser import load
from .render import render

__all__ = ["Design", "ParseError", "load", "render"]
__version__ = "0.1.0"
