"""Manager component layer.

Holds business workflow parts shared by several Managers. Unlike shared/ (pure
helpers) it may contain decision logic, but it is not a Manager itself, so it
must not import Routers or Managers (dependency direction: Manager -> Component -> Service).
"""
