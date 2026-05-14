"""TMW MCP: a Python client for The Mana World, exposed as an MCP server.

See README.md for usage. The most useful entry points are:

* ``tmw_mcp.mcp_shim:main`` - stdio MCP entry point that owns a self-restartable
  daemon subprocess. Console script ``tmw-mcp``.
* ``tmw_mcp.mcp_server:main`` - the daemon itself (also usable standalone).
  Console script ``tmw-mcp-server``.
* ``tmw_mcp.main:main`` - interactive CLI for humans. Console script
  ``tmw-mcp-cli``.
* ``tmw_mcp.register:main`` - account registration. Console script
  ``tmw-mcp-register``.
"""

__version__ = "0.1.0"
